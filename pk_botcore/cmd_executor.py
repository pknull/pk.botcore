"""Dynamic command registry for Claude [CMD:*] directives."""

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


def claude_command(name: str):
    """
    Decorator to mark a method as invocable by Claude via [CMD:name:args].

    Usage:
        @claude_command("skill")
        async def cmd_skill(self, ctx, char_name: str, skill_name: str) -> CmdResult:
            ...

    Args:
        name: The command name used in [CMD:name:args] directives
    """
    def decorator(func: Callable[..., Awaitable[CmdResult]]):
        func._claude_command = name
        return func
    return decorator


class CommandRegistry:
    """
    Registry of commands available for Claude to invoke.

    Scans loaded cogs for methods decorated with @claude_command and
    allows Claude to invoke them via [CMD:action:args] directives.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._commands: dict[str, Callable[..., Awaitable[CmdResult]]] = {}

    def discover_commands(self) -> None:
        """
        Scan loaded cogs for @claude_command decorated methods.

        Call this after all cogs are loaded to populate the registry.
        """
        self._commands.clear()

        for cog_name, cog in self.bot.cogs.items():
            for name, method in inspect.getmembers(cog, predicate=inspect.ismethod):
                if hasattr(method, '_claude_command'):
                    cmd_name = method._claude_command
                    self._commands[cmd_name] = method
                    logger.info("Registered command: %s from %s.%s", cmd_name, cog_name, name)

        logger.info("Command discovery complete: %d commands registered", len(self._commands))

    def list_commands(self) -> list[str]:
        """Return list of available command names."""
        return list(self._commands.keys())

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
        response_text: Claude's full response text

    Returns:
        List of command strings (without brackets)
    """
    return CMD_PATTERN.findall(response_text)


def clean_response(response_text: str) -> str:
    """
    Remove [CMD:*] directives from response text.

    Args:
        response_text: Claude's full response text

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
    Process Claude's response, extracting and executing [CMD:*] directives.

    Args:
        response_text: Claude's full response text
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
