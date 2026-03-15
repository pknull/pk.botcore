"""Dynamic command registry for LLM [CMD:*] directives."""

import inspect
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Awaitable

import discord
from discord.ext import commands

logger = logging.getLogger('pk_botcore.cmd_executor')

# Pattern for [CMD:action:arg1:arg2:...] directives
CMD_PATTERN = re.compile(r'\[CMD:([^\]]+)\]')


@dataclass
class CmdResult:
    """Result from executing a command directive."""
    success: bool
    embed: discord.Embed | None = None
    message: str | None = None
    error: str | None = None


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

    async def execute(self, cmd_string: str, ctx: Any) -> CmdResult:
        """
        Execute a [CMD:action:args] directive.

        Args:
            cmd_string: The command string (without brackets), e.g. "skill:Jake:Library Use"
            ctx: Context object (message, channel, author, etc.)

        Returns:
            CmdResult with success status and optional embed/message/error
        """
        parts = cmd_string.split(':')
        action = parts[0].lower()
        args = parts[1:] if len(parts) > 1 else []

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
    return CMD_PATTERN.findall(response_text)


def clean_response(response_text: str) -> str:
    """
    Remove [CMD:*] directives from response text.

    Args:
        response_text: LLM's full response text

    Returns:
        Cleaned text with commands removed
    """
    cleaned = CMD_PATTERN.sub("", response_text).strip()
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

    for cmd_match in CMD_PATTERN.findall(response_text):
        result = await registry.execute(cmd_match, ctx)
        results.append(result)
        logger.debug("Executed [CMD:%s] -> success=%s", cmd_match, result.success)

    # Remove command directives from response
    cleaned_text = CMD_PATTERN.sub("", response_text).strip()

    # Clean up excessive newlines
    while "\n\n\n" in cleaned_text:
        cleaned_text = cleaned_text.replace("\n\n\n", "\n\n")

    return cleaned_text, results
