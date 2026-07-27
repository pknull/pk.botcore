"""Shared Discord bot application boilerplate.

Provides common setup for logging, extension loading, event handling,
command syncing, and bot startup used by all PK bots.
"""

import asyncio
import logging
import os
import sys
import traceback

import discord
from discord import app_commands
from discord.ext import commands


# Remove nested-agent guard so Claude Agent SDK subprocesses can spawn.
# Bots may be launched from a Claude Code session (e.g. via tmux/asha:spawn),
# inheriting CLAUDECODE=1 which causes the CLI to refuse execution.
os.environ.pop('CLAUDECODE', None)
os.environ.pop('CLAUDE_CODE_ENTRYPOINT', None)


def setup_logging():
    """Configure the standard PK bot logging layout."""
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )

    debug_logger = logging.getLogger('debug')
    debug_logger.setLevel(logging.DEBUG)
    debug_logger.propagate = False

    root = logging.getLogger()
    root.setLevel(logging.WARNING)

    return debug_logger


def get_token():
    """Read and validate DISCORD_BOT_TOKEN from environment."""
    debug_logger = logging.getLogger('debug')
    token = os.environ.get('DISCORD_BOT_TOKEN')
    if not token:
        debug_logger.error("DISCORD_BOT_TOKEN not found in environment variables")
        debug_logger.error("Set DISCORD_BOT_TOKEN in .env file or environment")
        sys.exit(1)
    debug_logger.info("DISCORD_BOT_TOKEN configured")
    return token


def create_bot(*, intents, description, allowed_mentions=None):
    """Create a commands.Bot with standard PK settings."""
    if allowed_mentions is None:
        allowed_mentions = discord.AllowedMentions.none()
    return commands.Bot(
        intents=intents,
        command_prefix='!',
        description=description,
        help_command=None,
        allowed_mentions=allowed_mentions,
    )


async def load_extensions(bot, cogs):
    """Load cog extensions, exiting on failure."""
    debug_logger = logging.getLogger('debug')
    for cog in cogs:
        debug_logger.info('Loading extension %s', cog)
        try:
            await bot.load_extension("cogs." + cog)
            debug_logger.info('Loaded extension %s', cog)
        except Exception as e:
            exc = '{}: {}'.format(type(e).__name__, e)
            debug_logger.error('Failed to load extension %s\n%s', cog, exc)
            debug_logger.error('Traceback: %s', traceback.format_exc())
            sys.exit(1)


async def sync_commands(bot):
    """Copy global commands to guilds and clear remote global duplicates once.

    The registered global command objects are restored to the local tree after
    the empty global sync. This makes reconnect-driven ``on_ready`` calls
    idempotent without destroying the definitions needed by later guild syncs.
    """
    root = logging.getLogger()
    lock = getattr(bot, "_pk_botcore_sync_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(bot, "_pk_botcore_sync_lock", lock)

    async with lock:
        if getattr(bot, "_pk_botcore_commands_synced", False):
            root.debug("Slash commands already synchronized; skipping reconnect sync")
            return

        global_commands = list(bot.tree.get_commands(guild=None))
        try:
            for guild in bot.guilds:
                bot.tree.copy_global_to(guild=guild)

            bot.tree.clear_commands(guild=None)
            try:
                await bot.tree.sync()
                root.warning('Cleared global commands')
            finally:
                for command in global_commands:
                    bot.tree.add_command(command, override=True)

            all_guilds_synced = True
            for guild in bot.guilds:
                try:
                    guild_synced = await bot.tree.sync(guild=guild)
                    root.warning('Synced %d commands to guild: %s', len(guild_synced), guild.name)
                except Exception as e:
                    all_guilds_synced = False
                    root.warning('Failed to sync to guild %s: %s', guild.name, e)

            if all_guilds_synced:
                setattr(bot, "_pk_botcore_commands_synced", True)
            else:
                root.warning("Slash command sync incomplete; reconnect will retry")
        except Exception as e:
            root.error('Failed to sync slash commands: %s', e)


def register_common_events(bot):
    """Attach shared event handlers to the bot."""
    debug_logger = logging.getLogger('debug')

    @bot.event
    async def on_message(message):
        if message.author == bot.user:
            return
        try:
            await bot.process_commands(message)
        except discord.NotFound:
            debug_logger.error("Message not found when processing commands")
        except Exception as e:
            debug_logger.error("Error in on_message: %s", str(e))

    @bot.event
    async def on_command_error(ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.NotOwner):
            debug_logger.warning(
                "Non-owner command attempt: %s from %s (%s)",
                ctx.command, ctx.author.name, ctx.author.id
            )
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f'Missing argument: {error.param.name}')
            return
        if isinstance(error, commands.CheckFailure):
            return
        debug_logger.error('Command error: %s', str(error))
        await ctx.send('Something went wrong. Please try again.')

    @bot.tree.error
    async def on_app_command_error(interaction, error):
        debug_logger.error(
            "Slash command error in /%s: %s",
            interaction.command.name if interaction.command else "unknown",
            str(error)
        )
        original = error.original if isinstance(error, app_commands.CommandInvokeError) else error
        if isinstance(original, TypeError):
            msg = f"Command error: {original}"
        else:
            msg = "Something went wrong. Please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException as e:
            debug_logger.error("Failed to send error response: %s", e)


def register_killbot(bot, *, on_shutdown=None):
    """Register the killbot prefix and slash commands.

    Args:
        bot: The commands.Bot instance
        on_shutdown: Optional async callable() run before bot.close()
    """
    debug_logger = logging.getLogger('debug')

    async def _shutdown():
        if on_shutdown:
            try:
                await on_shutdown()
            except Exception as e:
                debug_logger.error("Error in shutdown hook: %s", str(e))
        await bot.close()

    @bot.command()
    @commands.is_owner()
    async def killbot(ctx):
        """Gracefully shut down the bot. Owner only, DM only."""
        if ctx.guild is not None:
            debug_logger.warning(
                "killbot rejected from guild %s by %s",
                ctx.guild.name, ctx.author.name
            )
            return
        debug_logger.info("Shutdown requested by %s", ctx.author.name)
        try:
            await ctx.send("Shutting down.")
        except discord.HTTPException as e:
            debug_logger.error("Failed to send shutdown message: %s", str(e))
        await _shutdown()

    @bot.tree.command(name="killbot", description="Shut down the bot (owner only)")
    async def slash_killbot(interaction):
        """Gracefully shut down the bot. Owner only."""
        app_info = await bot.application_info()
        if interaction.user.id != app_info.owner.id:
            await interaction.response.send_message("Only the owner can shut down the bot.", ephemeral=True)
            return
        debug_logger.info("Shutdown requested by %s via slash command", interaction.user.name)
        await interaction.response.send_message("Shutting down.")
        await _shutdown()


async def run_bot(bot, token, cogs, *, on_ready_extra=None):
    """Standard bot startup: load extensions, start, sync on ready.

    Args:
        bot: The commands.Bot instance
        token: Discord bot token
        cogs: List of cog module names to load
        on_ready_extra: Optional async callable(bot) run after sync_commands
    """
    debug_logger = logging.getLogger('debug')
    root = logging.getLogger()

    @bot.event
    async def on_ready():
        root.warning('Logged in as %s (id: %s)', bot.user.name, bot.user.id)
        await sync_commands(bot)
        if on_ready_extra:
            await on_ready_extra(bot)

    try:
        async with bot:
            await load_extensions(bot, cogs)
            await bot.start(token)
    except Exception as e:
        debug_logger.error("Error in main: %s", str(e))
        debug_logger.error("Traceback: %s", traceback.format_exc())
        raise
