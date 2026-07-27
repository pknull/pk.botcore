"""Dynamic command registry for LLM [CMD:*] directives."""

import inspect
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Awaitable

import discord
from discord.ext import commands

logger = logging.getLogger('pk_botcore.cmd_executor')

# Pattern for [CMD:action:arg1:arg2:...] directives
CMD_PATTERN = re.compile(r'\[CMD:([^\]]+)\]')
CMDJSON_PREFIX = "[CMDJSON:"


@dataclass
class CmdResult:
    """Result from executing a command directive."""
    success: bool
    embed: discord.Embed | None = None
    message: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class CommandDirective:
    """Parsed legacy or structured command directive."""

    action: str
    args: tuple[str, ...]
    raw: str
    start: int = -1
    end: int = -1


def llm_command(name: str, *, description: str = "", args: str = ""):
    """
    Decorator to mark a method as invocable by LLM via [CMD:name:args].

    Usage:
        @llm_command("skill", description="Roll a skill check", args="discord_id, skill_name, difficulty?, modifier?")
        async def cmd_skill(self, ctx, discord_id: str, skill_name: str, ...) -> CmdResult:
            ...

    Args:
        name: The command name used in [CMD:name:args] directives
        description: Human-readable description of what the command does
        args: Argument format string (use ? suffix for optional args)
    """
    def decorator(func: Callable[..., Awaitable[CmdResult]]):
        func._llm_command = name
        func._llm_command_desc = description
        func._llm_command_args = args
        return func
    return decorator


class CommandRegistry:
    """
    Registry of commands available for LLM to invoke.

    Scans loaded cogs for methods decorated with @llm_command and
    allows LLM to invoke them via [CMD:action:args] directives.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._commands: dict[str, Callable[..., Awaitable[CmdResult]]] = {}

    def discover_commands(self) -> None:
        """
        Scan loaded cogs for @llm_command decorated methods.

        Call this after all cogs are loaded to populate the registry.
        """
        self._commands.clear()

        for cog_name, cog in self.bot.cogs.items():
            for name, method in inspect.getmembers(cog, predicate=inspect.ismethod):
                if hasattr(method, '_llm_command'):
                    cmd_name = method._llm_command
                    self._commands[cmd_name] = method
                    logger.info("Registered command: %s from %s.%s", cmd_name, cog_name, name)

        logger.info("Command discovery complete: %d commands registered", len(self._commands))

    def list_commands(self) -> list[str]:
        """Return list of available command names."""
        return list(self._commands.keys())

    def get_command_docs(self) -> str:
        """
        Generate documentation for all registered commands.

        Returns markdown-formatted documentation suitable for injection
        into the LLM system prompt.
        """
        if not self._commands:
            return ""

        lines = [
            "## Executable Commands",
            "",
            "You can execute commands by including directives in your response.",
            "Format: `[CMD:action:arg1:arg2:...]`",
            (
                "For arguments containing colons or ISO timestamps, use structured JSON: "
                '`[CMDJSON:{"action":"action","args":["2026-07-27T12:30:00-07:00"]}]`'
            ),
            "",
            "| Command | Args | Description |",
            "|---------|------|-------------|",
        ]

        for name, method in sorted(self._commands.items()):
            desc = getattr(method, '_llm_command_desc', '') or 'No description'
            args = getattr(method, '_llm_command_args', '') or 'none'
            lines.append(f"| `{name}` | {args} | {desc} |")

        lines.append("")
        lines.append("Commands are executed automatically and stripped from the displayed response.")

        return "\n".join(lines)

    async def execute(self, cmd_string: str | CommandDirective, ctx: Any) -> CmdResult:
        """
        Execute a [CMD:action:args] directive.

        Args:
            cmd_string: The command string (without brackets), e.g. "skill:Jake:Library Use"
            ctx: Context object (message, channel, author, etc.)

        Returns:
            CmdResult with success status and optional embed/message/error
        """
        try:
            directive = (
                cmd_string
                if isinstance(cmd_string, CommandDirective)
                else _parse_command_string(cmd_string)
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return CmdResult(success=False, error=f"Invalid command directive: {exc}")

        action = directive.action.lower()
        args = list(directive.args)

        if action not in self._commands:
            available = ', '.join(self._commands.keys()) if self._commands else 'none'
            return CmdResult(
                success=False,
                error=f"Unknown command: {action}. Available: {available}"
            )

        try:
            handler = self._commands[action]
            result = await handler(ctx, *args)
            return result
        except TypeError as e:
            # Wrong number of arguments
            logger.error("Command %s argument error: %s", action, e)
            return CmdResult(
                success=False,
                error=f"Invalid arguments for {action}: {e}"
            )
        except Exception as e:
            logger.exception("Command %s execution error", action)
            return CmdResult(
                success=False,
                error=f"Error executing {action}: {e}"
            )


def extract_commands(response_text: str) -> list[str]:
    """
    Extract [CMD:*] directives from response text.

    Args:
        response_text: LLM's full response text

    Returns:
        List of command strings (without brackets)
    """
    return [directive.raw for directive in extract_command_directives(response_text)]


def _directive_from_json(
    payload: object,
    *,
    raw: str,
    start: int = -1,
    end: int = -1,
) -> CommandDirective:
    if not isinstance(payload, dict):
        raise ValueError("structured command must be a JSON object")
    action = payload.get("action")
    args = payload.get("args", [])
    if not isinstance(action, str) or not action.strip():
        raise ValueError("structured command action must be a non-empty string")
    if not isinstance(args, list):
        raise ValueError("structured command args must be a list")
    if not all(isinstance(arg, (str, int, float, bool)) or arg is None for arg in args):
        raise ValueError("structured command args must contain scalar values")
    return CommandDirective(
        action=action,
        args=tuple("" if arg is None else str(arg) for arg in args),
        raw=raw,
        start=start,
        end=end,
    )


def _parse_command_string(cmd_string: str) -> CommandDirective:
    if not isinstance(cmd_string, str):
        raise TypeError("command must be a string")
    stripped = cmd_string.strip()
    if stripped.startswith("{"):
        return _directive_from_json(json.loads(stripped), raw=cmd_string)
    parts = cmd_string.split(':')
    return CommandDirective(
        action=parts[0],
        args=tuple(parts[1:]),
        raw=cmd_string,
    )


def extract_command_directives(response_text: str) -> list[CommandDirective]:
    """Parse legacy ``CMD`` and colon-safe structured ``CMDJSON`` directives."""
    directives: list[CommandDirective] = []
    structured_regions: list[tuple[int, int]] = []

    decoder = json.JSONDecoder()
    search_from = 0
    while True:
        start = response_text.find(CMDJSON_PREFIX, search_from)
        if start < 0:
            break
        payload_start = start + len(CMDJSON_PREFIX)
        try:
            payload, consumed = decoder.raw_decode(response_text[payload_start:])
            end = payload_start + consumed
            if end >= len(response_text) or response_text[end] != "]":
                raise ValueError("structured command is missing closing bracket")
            raw = response_text[payload_start:end]
            directives.append(
                _directive_from_json(
                    payload,
                    raw=raw,
                    start=start,
                    end=end + 1,
                )
            )
            structured_regions.append((start, end + 1))
            search_from = end + 1
        except (json.JSONDecodeError, ValueError):
            # Invalid directives remain visible rather than executing partially.
            candidate_end = _find_structured_candidate_end(response_text, payload_start)
            structured_regions.append((start, candidate_end))
            search_from = candidate_end

    for match in CMD_PATTERN.finditer(response_text):
        if any(start <= match.start() < end for start, end in structured_regions):
            continue
        parsed = _parse_command_string(match.group(1))
        directives.append(
            CommandDirective(
                action=parsed.action,
                args=parsed.args,
                raw=match.group(1),
                start=match.start(),
                end=match.end(),
            )
        )

    directives.sort(key=lambda directive: directive.start)
    return directives


def _find_structured_candidate_end(response_text: str, payload_start: int) -> int:
    """Find the outer bracket of malformed CMDJSON without scanning inside it."""
    depth = 0
    in_string = False
    escaped = False

    for index in range(payload_start, len(response_text)):
        char = response_text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            if char == "]" and depth == 0:
                return index + 1
            depth = max(0, depth - 1)

    return len(response_text)


def _remove_directives(response_text: str, directives: list[CommandDirective]) -> str:
    cleaned = response_text
    for directive in reversed(directives):
        if directive.start >= 0:
            cleaned = cleaned[:directive.start] + cleaned[directive.end:]
    return cleaned


def clean_response(response_text: str) -> str:
    """
    Remove [CMD:*] directives from response text.

    Args:
        response_text: LLM's full response text

    Returns:
        Cleaned text with commands removed
    """
    cleaned = _remove_directives(response_text, extract_command_directives(response_text)).strip()
    while "\n\n\n" in cleaned:
        cleaned = cleaned.replace("\n\n\n", "\n\n")
    return cleaned


async def process_response_async(
    response_text: str,
    registry: CommandRegistry,
    ctx: Any
) -> tuple[str, list[CmdResult]]:
    """
    Process LLM response, extracting and executing [CMD:*] directives.

    Args:
        response_text: LLM's full response text
        registry: The CommandRegistry to execute commands with
        ctx: Context object for command execution

    Returns:
        Tuple of (cleaned_text, list_of_results)
    """
    results = []

    directives = extract_command_directives(response_text)
    for directive in directives:
        result = await registry.execute(directive, ctx)
        results.append(result)
        logger.debug("Executed command %s -> success=%s", directive.action, result.success)

    # Remove command directives from response
    cleaned_text = _remove_directives(response_text, directives).strip()

    # Clean up excessive newlines
    while "\n\n\n" in cleaned_text:
        cleaned_text = cleaned_text.replace("\n\n\n", "\n\n")

    return cleaned_text, results
