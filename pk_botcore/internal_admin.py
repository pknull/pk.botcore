"""Shared internal admin cog for Discord bots.

This cog exposes only internal LLM command handlers. It does not register
public slash commands. Bots can opt in by loading a thin local wrapper cog.
"""

from __future__ import annotations

import re
from io import BytesIO
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import discord
from discord.ext import commands

from .cmd_executor import CmdResult, llm_command

USER_REF_RE = re.compile(r"<@!?(?P<user_id>\d+)>")
ROLE_REF_RE = re.compile(r"<@&(?P<role_id>\d+)>")
CHANNEL_REF_RE = re.compile(r"<#(?P<channel_id>\d+)>")
EMOJI_REF_RE = re.compile(r"<a?:[A-Za-z0-9_]+:(?P<emoji_id>\d+)>")
DURATION_PART_RE = re.compile(r"(?P<value>\d+)(?P<unit>[smhdw])", re.IGNORECASE)
WEBHOOK_REF_RE = re.compile(r"https://discord(?:app)?\.com/api/webhooks/(?P<webhook_id>\d+)/")
REVIEW_LIST_PAGE_SIZE = 25

REQUESTER_ADMIN_CAPABILITY_MAP = [
    ("kick_members", "kick members and review recent joins"),
    ("ban_members", "ban, unban, softban, and massban members"),
    ("moderate_members", "timeout members, remove timeouts, and review active timeouts"),
    ("move_members", "disconnect or move members in voice channels"),
    ("mute_members", "server mute or unmute members in voice channels"),
    ("deafen_members", "server deafen or undeafen members in voice channels"),
    ("manage_webhooks", "create, rename, move, delete, and review webhooks"),
    ("manage_guild", "manage automod rules and prune inactive members"),
    ("manage_events", "create, edit, delete, and review scheduled events"),
    ("manage_roles", "add, remove, create, rename, delete, and review roles"),
    ("manage_channels", "create, rename, review, lock, unlock, delete, edit, and post to channels"),
    ("manage_nicknames", "set or clear nicknames"),
    ("manage_emojis_and_stickers", "create, rename, delete, or review emojis"),
]


def get_actor_admin_capabilities(actor) -> list[str]:
    """Describe the Discord admin actions the actor may request through the bot."""
    guild_permissions = getattr(actor, "guild_permissions", None)
    if guild_permissions is None:
        return []
    if getattr(guild_permissions, "administrator", False):
        return ["all server admin commands"]

    capabilities = []
    for permission_name, description in REQUESTER_ADMIN_CAPABILITY_MAP:
        if getattr(guild_permissions, permission_name, False):
            capabilities.append(description)
    return capabilities


class DestructiveActionConfirmView(discord.ui.View):
    """Requester-locked confirmation view for destructive admin actions."""

    def __init__(
        self,
        *,
        requester_id: int,
        prompt_text: str,
        executor: Callable[[], Awaitable[CmdResult]],
        timeout: float = 120.0,
    ):
        super().__init__(timeout=timeout)
        self.requester_id = requester_id
        self.prompt_text = prompt_text
        self.executor = executor
        self.message: discord.Message | None = None

        confirm_button = discord.ui.Button(label="Confirm", style=discord.ButtonStyle.danger)
        cancel_button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        confirm_button.callback = self._confirm  # type: ignore[assignment]
        cancel_button.callback = self._cancel  # type: ignore[assignment]
        self.add_item(confirm_button)
        self.add_item(cancel_button)

    def _lock(self) -> None:
        for child in self.children:
            child.disabled = True

    async def _edit_bound_message(self, *, content: str, view: discord.ui.View | None = None) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(content=content, view=view)
        except discord.HTTPException:
            pass

    async def _confirm(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the requester can confirm this action.", ephemeral=True)
            return

        self._lock()
        await interaction.response.edit_message(content=f"{self.prompt_text}\nStatus: executing...", view=self)
        result = await self.executor()
        final_text = result.message if result.success else f"Denied: {result.error or 'unknown error'}"
        await self._edit_bound_message(content=f"{self.prompt_text}\nStatus: {final_text}", view=None)
        self.stop()

    async def _cancel(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Only the requester can cancel this action.", ephemeral=True)
            return

        self._lock()
        await interaction.response.edit_message(content=f"{self.prompt_text}\nStatus: cancelled.", view=self)
        await self._edit_bound_message(content=f"{self.prompt_text}\nStatus: cancelled.", view=None)
        self.stop()

    async def on_timeout(self) -> None:
        self._lock()
        await self._edit_bound_message(content=f"{self.prompt_text}\nStatus: confirmation timed out.", view=None)


class InternalAdminCog(commands.Cog):
    """Internal-only server admin command surface for LLM-driven bots."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._owner_id: int | None = None

    async def _get_owner_id(self) -> int:
        if self._owner_id is None:
            app_info = await self.bot.application_info()
            self._owner_id = app_info.owner.id
        return self._owner_id

    async def _ctx_is_keeper(self, ctx) -> bool:
        actor = getattr(ctx, "author", None) or getattr(ctx, "user", None)
        if actor is None:
            return False
        return actor.id == await self._get_owner_id()

    def _format_permission_names(self, permissions: list[str]) -> str:
        return ", ".join(permission.replace("_", " ") for permission in permissions)

    def _missing_permissions(self, member, permissions: list[str]) -> list[str]:
        guild_permissions = getattr(member, "guild_permissions", None)
        if guild_permissions is None:
            return permissions
        return [permission for permission in permissions if not getattr(guild_permissions, permission, False)]

    def _actor_has_any_permission(self, actor, permissions: list[str]) -> bool:
        guild_permissions = getattr(actor, "guild_permissions", None)
        if guild_permissions is None:
            return False
        if getattr(guild_permissions, "administrator", False):
            return True
        return all(getattr(guild_permissions, permission, False) for permission in permissions)

    def _role_position(self, role) -> int:
        return int(getattr(role, "position", 0) or 0)

    def _get_bot_member(self, guild):
        bot_member = getattr(guild, "me", None)
        if bot_member is not None:
            return bot_member

        get_member = getattr(guild, "get_member", None)
        if callable(get_member) and getattr(self.bot, "user", None) is not None:
            return get_member(self.bot.user.id)
        return None

    async def _resolve_global_channel(self, reference: str):
        if not reference:
            return None, None, "Missing channel reference."

        reference = reference.strip()
        channel_id = None
        mention_match = CHANNEL_REF_RE.fullmatch(reference)
        if mention_match:
            channel_id = int(mention_match.group("channel_id"))
        elif reference.isdigit():
            channel_id = int(reference)

        if channel_id is None:
            return None, None, "Outside a server context, use a channel mention or numeric channel ID."

        channel = None
        get_channel_or_thread = getattr(self.bot, "get_channel_or_thread", None)
        if callable(get_channel_or_thread):
            channel = get_channel_or_thread(channel_id)
        if channel is None:
            get_channel = getattr(self.bot, "get_channel", None)
            if callable(get_channel):
                channel = get_channel(channel_id)
        if channel is None:
            fetch_channel = getattr(self.bot, "fetch_channel", None)
            if callable(fetch_channel):
                try:
                    channel = await fetch_channel(channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    channel = None
        if channel is None:
            return None, None, f"No channel found for `{reference}`."

        guild = getattr(channel, "guild", None)
        if guild is None:
            return None, None, "That destination is not a server channel."
        return channel, guild, None

    async def _ensure_admin_command(
        self,
        ctx,
        *,
        action: str,
        required_permissions: list[str] | None = None,
        bot_required_permissions: list[str] | None = None,
    ) -> tuple[object | None, object | None, CmdResult | None]:
        actor = getattr(ctx, "author", None) or getattr(ctx, "user", None)
        is_keeper = await self._ctx_is_keeper(ctx)
        if bot_required_permissions is None:
            bot_required_permissions = required_permissions

        guild = getattr(ctx, "guild", None)
        if guild is None:
            return None, None, CmdResult(success=False, error=f"{action.capitalize()} only works in a server.")

        if not is_keeper:
            if actor is None or not required_permissions:
                return None, None, CmdResult(success=False, error=f"Only The Keeper can {action}.")
            if not self._actor_has_any_permission(actor, required_permissions):
                return None, None, CmdResult(
                    success=False,
                    error=(
                        f"You need `{self._format_permission_names(required_permissions)}` "
                        f"or `administrator` to {action} through the bot."
                    ),
                )

        bot_member = self._get_bot_member(guild)
        if bot_member is None:
            return None, None, CmdResult(success=False, error="Can't resolve my server member record.")

        if bot_required_permissions:
            missing = self._missing_permissions(bot_member, bot_required_permissions)
            if missing:
                return guild, bot_member, CmdResult(
                    success=False,
                    error=f"I need `{self._format_permission_names(missing)}` to {action}.",
                )

        return guild, bot_member, None

    async def _resolve_member(self, guild, reference: str):
        if not reference:
            return None, "Missing member reference."

        reference = reference.strip()
        user_id = None
        mention_match = USER_REF_RE.fullmatch(reference)
        if mention_match:
            user_id = int(mention_match.group("user_id"))
        elif reference.isdigit():
            user_id = int(reference)

        if user_id is not None:
            get_member = getattr(guild, "get_member", None)
            if callable(get_member):
                member = get_member(user_id)
                if member is not None:
                    return member, None

            fetch_member = getattr(guild, "fetch_member", None)
            if callable(fetch_member):
                try:
                    member = await fetch_member(user_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    member = None
                if member is not None:
                    return member, None
            return None, f"No server member found for `{reference}`."

        members = list(getattr(guild, "members", []) or [])
        exact_matches = [
            member for member in members
            if reference in {getattr(member, "display_name", None), getattr(member, "name", None), str(member)}
        ]
        if len(exact_matches) == 1:
            return exact_matches[0], None
        if len(exact_matches) > 1:
            return None, f"`{reference}` matches multiple members. Use a mention or user ID."

        lowered = reference.casefold()
        fuzzy_matches = [
            member for member in members
            if lowered in {
                str(getattr(member, "display_name", "")).casefold(),
                str(getattr(member, "name", "")).casefold(),
                str(member).casefold(),
            }
        ]
        if len(fuzzy_matches) == 1:
            return fuzzy_matches[0], None
        if len(fuzzy_matches) > 1:
            return None, f"`{reference}` matches multiple members. Use a mention or user ID."

        return None, f"No server member found for `{reference}`."

    def _resolve_role(self, guild, reference: str):
        if not reference:
            return None, "Missing role reference."

        reference = reference.strip()
        role_id = None
        mention_match = ROLE_REF_RE.fullmatch(reference)
        if mention_match:
            role_id = int(mention_match.group("role_id"))
        elif reference.isdigit():
            role_id = int(reference)

        if role_id is not None:
            get_role = getattr(guild, "get_role", None)
            role = get_role(role_id) if callable(get_role) else None
            if role is None:
                return None, f"No role found for `{reference}`."
            return role, None

        roles = list(getattr(guild, "roles", []) or [])
        exact_matches = [role for role in roles if reference == getattr(role, "name", None)]
        if len(exact_matches) == 1:
            return exact_matches[0], None
        if len(exact_matches) > 1:
            return None, f"`{reference}` matches multiple roles. Use a role mention or ID."

        lowered = reference.casefold()
        fuzzy_matches = [role for role in roles if str(getattr(role, "name", "")).casefold() == lowered]
        if len(fuzzy_matches) == 1:
            return fuzzy_matches[0], None
        if len(fuzzy_matches) > 1:
            return None, f"`{reference}` matches multiple roles. Use a role mention or ID."

        return None, f"No role found for `{reference}`."

    def _resolve_emoji(self, guild, reference: str):
        if not reference:
            return None, "Missing emoji reference."

        reference = reference.strip()
        emoji_id = None
        mention_match = EMOJI_REF_RE.fullmatch(reference)
        if mention_match:
            emoji_id = int(mention_match.group("emoji_id"))
        elif reference.isdigit():
            emoji_id = int(reference)

        emojis = list(getattr(guild, "emojis", []) or [])
        if emoji_id is not None:
            for emoji in emojis:
                if getattr(emoji, "id", None) == emoji_id:
                    return emoji, None
            return None, f"No emoji found for `{reference}`."

        exact_matches = [emoji for emoji in emojis if reference == getattr(emoji, "name", None)]
        if len(exact_matches) == 1:
            return exact_matches[0], None
        if len(exact_matches) > 1:
            return None, f"`{reference}` matches multiple emojis. Use the emoji ID."

        lowered = reference.casefold()
        fuzzy_matches = [emoji for emoji in emojis if str(getattr(emoji, "name", "")).casefold() == lowered]
        if len(fuzzy_matches) == 1:
            return fuzzy_matches[0], None
        if len(fuzzy_matches) > 1:
            return None, f"`{reference}` matches multiple emojis. Use the emoji ID."

        return None, f"No emoji found for `{reference}`."

    async def _fetch_detailed_emoji(self, guild, emoji):
        fetch_emoji = getattr(guild, "fetch_emoji", None)
        if not callable(fetch_emoji):
            return emoji
        try:
            return await fetch_emoji(emoji.id)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            return emoji

    def _resolve_channel(self, guild, reference: str):
        if not reference:
            return None, "Missing channel reference."

        reference = reference.strip()
        channel_id = None
        mention_match = CHANNEL_REF_RE.fullmatch(reference)
        if mention_match:
            channel_id = int(mention_match.group("channel_id"))
        elif reference.isdigit():
            channel_id = int(reference)

        channels = list(getattr(guild, "channels", []) or [])
        if channel_id is not None:
            get_channel = getattr(guild, "get_channel", None)
            channel = get_channel(channel_id) if callable(get_channel) else None
            if channel is None:
                for existing in channels:
                    if getattr(existing, "id", None) == channel_id:
                        channel = existing
                        break
            if channel is None:
                return None, f"No channel found for `{reference}`."
            return channel, None

        exact_matches = [channel for channel in channels if reference == getattr(channel, "name", None)]
        if len(exact_matches) == 1:
            return exact_matches[0], None
        if len(exact_matches) > 1:
            return None, f"`{reference}` matches multiple channels. Use a channel mention or ID."

        lowered = reference.casefold()
        fuzzy_matches = [channel for channel in channels if str(getattr(channel, "name", "")).casefold() == lowered]
        if len(fuzzy_matches) == 1:
            return fuzzy_matches[0], None
        if len(fuzzy_matches) > 1:
            return None, f"`{reference}` matches multiple channels. Use a channel mention or ID."

        return None, f"No channel found for `{reference}`."

    async def _resolve_ban_entry(self, guild, reference: str):
        if not reference:
            return None, "Missing banned user reference."

        reference = reference.strip()
        user_id = None
        mention_match = USER_REF_RE.fullmatch(reference)
        if mention_match:
            user_id = int(mention_match.group("user_id"))
        elif reference.isdigit():
            user_id = int(reference)

        bans = getattr(guild, "bans", None)
        if not callable(bans):
            return None, "This guild doesn't expose ban records through the current API surface."

        async for entry in bans(limit=None):
            user = getattr(entry, "user", None)
            if user is None:
                continue
            if user_id is not None and getattr(user, "id", None) == user_id:
                return entry, None
            if user_id is None:
                candidates = {getattr(user, "name", None), str(user)}
                if reference in candidates:
                    return entry, None
                lowered = reference.casefold()
                if any(str(candidate).casefold() == lowered for candidate in candidates if candidate):
                    return entry, None

        return None, f"No ban entry found for `{reference}`."

    async def _resolve_ban_target(self, guild, reference: str):
        member, member_error = await self._resolve_member(guild, reference)
        if member is not None:
            return member, None

        reference = (reference or "").strip()
        user_id = None
        mention_match = USER_REF_RE.fullmatch(reference)
        if mention_match:
            user_id = int(mention_match.group("user_id"))
        elif reference.isdigit():
            user_id = int(reference)

        if user_id is not None:
            return discord.Object(id=user_id), None

        return None, member_error or f"No bannable target found for `{reference}`."

    async def _resolve_webhook(self, guild, reference: str):
        if not reference:
            return None, "Missing webhook reference."
        reference = reference.strip()
        webhook_id = None
        match = WEBHOOK_REF_RE.search(reference)
        if match:
            webhook_id = int(match.group("webhook_id"))
        elif reference.isdigit():
            webhook_id = int(reference)

        try:
            webhooks = await guild.webhooks()
        except (discord.Forbidden, discord.HTTPException) as exc:
            return None, f"Couldn't fetch webhooks: {exc}"

        if webhook_id is not None:
            for webhook in webhooks:
                if getattr(webhook, "id", None) == webhook_id:
                    return webhook, None
            return None, f"No webhook found for `{reference}`."

        exact_matches = [webhook for webhook in webhooks if reference == getattr(webhook, "name", None)]
        if len(exact_matches) == 1:
            return exact_matches[0], None
        if len(exact_matches) > 1:
            return None, f"`{reference}` matches multiple webhooks. Use an ID."

        lowered = reference.casefold()
        fuzzy_matches = [webhook for webhook in webhooks if str(getattr(webhook, "name", "")).casefold() == lowered]
        if len(fuzzy_matches) == 1:
            return fuzzy_matches[0], None
        if len(fuzzy_matches) > 1:
            return None, f"`{reference}` matches multiple webhooks. Use an ID."
        return None, f"No webhook found for `{reference}`."

    async def _resolve_sticker(self, guild, reference: str):
        if not reference:
            return None, "Missing sticker reference."
        reference = reference.strip()
        sticker_id = int(reference) if reference.isdigit() else None
        try:
            stickers = await guild.fetch_stickers()
        except (discord.Forbidden, discord.HTTPException) as exc:
            return None, f"Couldn't fetch stickers: {exc}"

        if sticker_id is not None:
            for sticker in stickers:
                if getattr(sticker, "id", None) == sticker_id:
                    return sticker, None
            return None, f"No sticker found for `{reference}`."

        exact_matches = [sticker for sticker in stickers if reference == getattr(sticker, "name", None)]
        if len(exact_matches) == 1:
            return exact_matches[0], None
        if len(exact_matches) > 1:
            return None, f"`{reference}` matches multiple stickers. Use an ID."

        lowered = reference.casefold()
        fuzzy_matches = [sticker for sticker in stickers if str(getattr(sticker, "name", "")).casefold() == lowered]
        if len(fuzzy_matches) == 1:
            return fuzzy_matches[0], None
        if len(fuzzy_matches) > 1:
            return None, f"`{reference}` matches multiple stickers. Use an ID."
        return None, f"No sticker found for `{reference}`."

    async def _resolve_scheduled_event(self, guild, reference: str):
        if not reference:
            return None, "Missing scheduled event reference."
        reference = reference.strip()
        event_id = int(reference) if reference.isdigit() else None
        try:
            events = await guild.fetch_scheduled_events(with_counts=True)
        except (discord.Forbidden, discord.HTTPException) as exc:
            return None, f"Couldn't fetch scheduled events: {exc}"

        if event_id is not None:
            for event in events:
                if getattr(event, "id", None) == event_id:
                    return event, None
            return None, f"No scheduled event found for `{reference}`."

        exact_matches = [event for event in events if reference == getattr(event, "name", None)]
        if len(exact_matches) == 1:
            return exact_matches[0], None
        if len(exact_matches) > 1:
            return None, f"`{reference}` matches multiple scheduled events. Use an ID."

        lowered = reference.casefold()
        fuzzy_matches = [event for event in events if str(getattr(event, "name", "")).casefold() == lowered]
        if len(fuzzy_matches) == 1:
            return fuzzy_matches[0], None
        if len(fuzzy_matches) > 1:
            return None, f"`{reference}` matches multiple scheduled events. Use an ID."
        return None, f"No scheduled event found for `{reference}`."

    async def _resolve_automod_rule(self, guild, reference: str):
        if not reference:
            return None, "Missing automod rule reference."
        reference = reference.strip()
        rule_id = int(reference) if reference.isdigit() else None
        try:
            rules = await guild.fetch_automod_rules()
        except (discord.Forbidden, discord.HTTPException) as exc:
            return None, f"Couldn't fetch automod rules: {exc}"

        if rule_id is not None:
            for rule in rules:
                if getattr(rule, "id", None) == rule_id:
                    return rule, None
            return None, f"No automod rule found for `{reference}`."

        exact_matches = [rule for rule in rules if reference == getattr(rule, "name", None)]
        if len(exact_matches) == 1:
            return exact_matches[0], None
        if len(exact_matches) > 1:
            return None, f"`{reference}` matches multiple automod rules. Use an ID."

        lowered = reference.casefold()
        fuzzy_matches = [rule for rule in rules if str(getattr(rule, "name", "")).casefold() == lowered]
        if len(fuzzy_matches) == 1:
            return fuzzy_matches[0], None
        if len(fuzzy_matches) > 1:
            return None, f"`{reference}` matches multiple automod rules. Use an ID."
        return None, f"No automod rule found for `{reference}`."

    def _can_bot_manage_member(self, guild, bot_member, target) -> str | None:
        target_id = getattr(target, "id", None)
        if target_id is not None and target_id == getattr(guild, "owner_id", None):
            return "Can't act on the server owner."
        if getattr(self.bot, "user", None) is not None and target_id == self.bot.user.id:
            return "Not kicking or banning myself."

        bot_top_role = getattr(bot_member, "top_role", None)
        target_top_role = getattr(target, "top_role", None)
        if bot_top_role is not None and target_top_role is not None:
            if self._role_position(target_top_role) >= self._role_position(bot_top_role):
                return "Target is above or equal to my top role."
        return None

    def _can_bot_manage_role(self, guild, bot_member, role) -> str | None:
        if role is None:
            return "Role not found."
        if getattr(role, "managed", False):
            return "Can't manage integration-managed roles."
        if getattr(role, "id", None) == getattr(guild, "id", None):
            return "Can't modify the @everyone role."

        bot_top_role = getattr(bot_member, "top_role", None)
        if bot_top_role is not None and self._role_position(role) >= self._role_position(bot_top_role):
            return "That role is above or equal to my top role."
        return None

    def _build_audit_reason(self, ctx, action: str, reason: str | None = None) -> str:
        actor = getattr(ctx, "author", None) or getattr(ctx, "user", None)
        actor_name = getattr(actor, "display_name", None) or getattr(actor, "name", None) or "The Keeper"
        actor_id = getattr(actor, "id", "unknown")
        suffix = f" Requested by {actor_name} ({actor_id}) via bot."
        if reason:
            return f"{action}: {reason}.{suffix}"
        return f"{action}.{suffix}"

    async def _send_context_message(self, ctx, *, content: str, view: discord.ui.View | None = None):
        send = getattr(ctx, "send", None)
        if callable(send):
            return await send(content=content, view=view)

        channel = getattr(ctx, "channel", None)
        channel_send = getattr(channel, "send", None)
        if callable(channel_send):
            return await channel_send(content=content, view=view)
        return None

    async def _request_destructive_confirmation(
        self,
        ctx,
        *,
        summary: str,
        executor: Callable[[], Awaitable[CmdResult]],
    ) -> CmdResult:
        actor = getattr(ctx, "author", None) or getattr(ctx, "user", None)
        if actor is None:
            return CmdResult(success=False, error="Can't determine who should confirm this action.")

        actor_label = getattr(actor, "mention", None) or getattr(actor, "display_name", None) or getattr(actor, "name", None) or "requester"
        prompt_text = f"{actor_label}: confirm `{summary}`. Buttons expire in 2 minutes."
        view = DestructiveActionConfirmView(requester_id=actor.id, prompt_text=prompt_text, executor=executor)
        message = await self._send_context_message(ctx, content=prompt_text, view=view)
        if message is not None:
            view.message = message
        return CmdResult(
            success=True,
            message=f"[[INTERNAL]] Confirmation requested for `{summary}`. No changes made yet.",
        )

    def _format_datetime(self, value) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return str(value)

    def _build_listing_message(self, header: str, detail_lines: list[str], *, hard_limit: int = 1900) -> str:
        message = "\n".join([header, *detail_lines])
        if len(message) <= hard_limit:
            return message

        kept_lines = [header]
        current_length = len(header) + 1
        remaining = len(detail_lines)
        for line in detail_lines:
            projected = current_length + len(line) + 1
            if projected > hard_limit - 50:
                break
            kept_lines.append(line)
            current_length = projected
            remaining -= 1

        if remaining > 0:
            kept_lines.append(f"- ...and {remaining} more.")
        return "\n".join(kept_lines)

    def _parse_toggle_value(self, raw_value: str, *, label: str = "value") -> tuple[bool | None, str | None]:
        normalized = str(raw_value or "").strip().casefold()
        if normalized in {"on", "true", "yes", "enable", "enabled", "1"}:
            return True, None
        if normalized in {"off", "false", "no", "disable", "disabled", "0"}:
            return False, None
        return None, f"{label.capitalize()} must be `on` or `off`."

    def _parse_role_colour(self, raw_value: str) -> tuple[int | None, str | None]:
        normalized = str(raw_value or "").strip().lower()
        if normalized in {"clear", "none", "default", "remove"}:
            return 0, None
        if normalized.startswith("#"):
            normalized = normalized[1:]
        if normalized.startswith("0x"):
            normalized = normalized[2:]
        if len(normalized) != 6 or any(ch not in "0123456789abcdef" for ch in normalized):
            return None, "Role color must be a 6-digit hex value like `#FF8800`, or `clear`."
        return int(normalized, 16), None

    def _parse_iso_datetime(self, raw_value: str) -> tuple[datetime | None, str | None]:
        normalized = str(raw_value or "").strip()
        if not normalized:
            return None, "Missing datetime."
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(normalized)
        except ValueError:
            return None, "Datetime must be ISO-8601 like `2026-04-08T18:30:00+00:00`."
        if value.tzinfo is None:
            return None, "Datetime must include a timezone offset."
        return value, None

    def _parse_keyword_list(self, raw_value: str) -> list[str]:
        return [item.strip() for item in str(raw_value or "").split(",") if item.strip()]

    def _is_clear_value(self, raw_value: str) -> bool:
        return str(raw_value or "").strip().casefold() in {"clear", "none", "remove", "null"}

    def _parse_overwrite_value(self, raw_value: str, *, label: str) -> tuple[bool | None, str | None]:
        normalized = str(raw_value or "").strip().casefold()
        if normalized in {"clear", "none", "remove", "inherit", "default", "unset"}:
            return None, None
        if normalized in {"on", "true", "yes", "allow", "allowed", "grant", "granted", "1"}:
            return True, None
        if normalized in {"off", "false", "no", "deny", "denied", "block", "blocked", "0"}:
            return False, None
        return None, f"{label.capitalize()} must be `allow`, `deny`, or `clear`."

    def _parse_permission_overwrite_options(
        self,
        option_parts: tuple[str, ...],
        *,
        subject: str,
    ) -> tuple[dict[str, bool | None] | None, str | None, str | None]:
        valid_names = {name.casefold() for name in discord.PermissionOverwrite().VALID_NAMES}
        overwrites: dict[str, bool | None] = {}
        reason: str | None = None
        for raw_part in option_parts:
            part = str(raw_part or "").strip()
            if not part:
                continue
            if "=" not in part:
                return None, None, f"{subject} options must use `key=value` segments."
            key, value = part.split("=", 1)
            normalized_key = key.strip().casefold().replace("-", "_")
            if normalized_key == "reason":
                reason = value.strip() or None
                continue
            if normalized_key not in valid_names:
                return None, None, f"Unsupported {subject} permission `{key.strip()}`."
            parsed_value, value_error = self._parse_overwrite_value(value, label=f"permission `{normalized_key}`")
            if value_error:
                return None, None, value_error
            overwrites[normalized_key] = parsed_value
        if not overwrites:
            return None, None, f"{subject.capitalize()} needs at least one permission overwrite like `send_messages=deny`."
        return overwrites, reason, None

    async def _resolve_permission_target(self, guild, reference: str):
        if not reference:
            return None, "Missing permission target."
        reference = reference.strip()

        if ROLE_REF_RE.fullmatch(reference):
            return self._resolve_role(guild, reference)
        if USER_REF_RE.fullmatch(reference):
            return await self._resolve_member(guild, reference)

        if reference.isdigit():
            role, role_error = self._resolve_role(guild, reference)
            if role_error is None:
                return role, None
            return await self._resolve_member(guild, reference)

        role, role_error = self._resolve_role(guild, reference)
        member, member_error = await self._resolve_member(guild, reference)
        if role_error is None and member_error is None:
            return None, f"`{reference}` matches both a role and a member. Use a mention or ID."
        if role_error is None:
            return role, None
        if member_error is None:
            return member, None
        return None, member_error or role_error or f"No role or member found for `{reference}`."

    async def _resolve_automod_action_channel(self, guild, reference: str):
        channel, channel_error = self._resolve_channel(guild, reference)
        if channel_error:
            return None, channel_error
        kind = self._channel_kind(channel)
        if kind not in {"text", "news", "forum"} and not hasattr(channel, "topic"):
            return None, "Automod alert channels must be text-like channels."
        return channel, None

    async def _build_automod_actions(
        self,
        guild,
        options: dict[str, str],
        *,
        allow_custom_message: bool = True,
        allow_timeout: bool = True,
    ) -> tuple[list[discord.AutoModRuleAction] | None, str | None]:
        actions: list[discord.AutoModRuleAction] = []
        custom_message = options.get("custom_message")
        if custom_message and not allow_custom_message:
            return None, "This automod rule type does not support a custom block message."
        actions.append(discord.AutoModRuleAction(custom_message=custom_message) if custom_message else discord.AutoModRuleAction())

        alert_channel_ref = options.get("alert_channel")
        if alert_channel_ref:
            alert_channel, alert_error = await self._resolve_automod_action_channel(guild, alert_channel_ref)
            if alert_error:
                return None, alert_error
            actions.append(
                discord.AutoModRuleAction(
                    type=discord.AutoModRuleActionType.send_alert_message,
                    channel_id=alert_channel.id,
                )
            )

        timeout_text = options.get("timeout")
        if timeout_text:
            if not allow_timeout:
                return None, "This automod rule type does not support timeout actions."
            duration, duration_error = self._parse_timeout_duration(timeout_text)
            if duration_error:
                return None, duration_error
            actions.append(
                discord.AutoModRuleAction(
                    type=discord.AutoModRuleActionType.timeout,
                    duration=duration,
                )
            )

        return actions, None

    def _parse_automod_presets(self, raw_value: str) -> tuple[discord.AutoModPresets | None, str | None]:
        preset_names = self._parse_keyword_list(raw_value)
        if not preset_names:
            return None, "Preset automod rules need at least one preset like `profanity` or `slurs`."
        preset_kwargs = {"profanity": False, "sexual_content": False, "slurs": False}
        for name in preset_names:
            normalized = name.casefold().replace("-", "_")
            if normalized == "all":
                for key in preset_kwargs:
                    preset_kwargs[key] = True
                continue
            if normalized not in preset_kwargs:
                supported = ", ".join(sorted([*preset_kwargs.keys(), "all"]))
                return None, f"Unsupported automod preset `{name}`. Supported: {supported}."
            preset_kwargs[normalized] = True
        return discord.AutoModPresets(**preset_kwargs), None

    def _format_role_line(self, guild, role) -> str:
        details = [f"pos {self._role_position(role)}"]

        if getattr(role, "managed", False):
            details.append("managed")
        if getattr(role, "hoist", False):
            details.append("hoisted")
        if getattr(role, "mentionable", False):
            details.append("mentionable")

        colour = getattr(role, "colour", None) or getattr(role, "color", None)
        colour_value = getattr(colour, "value", colour)
        if isinstance(colour_value, int) and colour_value:
            details.append(f"color #{colour_value:06X}")

        member_count = sum(1 for member in list(getattr(guild, "members", []) or []) if role in getattr(member, "roles", []))
        details.append(f"members {member_count}")

        created_at = self._format_datetime(getattr(role, "created_at", None))
        if created_at:
            details.append(f"created {created_at}")

        return f"- @{role.name} `{role.id}` ({'; '.join(details)})"

    def _channel_kind(self, channel) -> str:
        channel_type = getattr(channel, "type", None)
        if channel_type is not None:
            return str(channel_type).replace("_", " ")

        class_name = channel.__class__.__name__.replace("Channel", "")
        return class_name.casefold() or "channel"

    def _channel_lock_permission(self, channel) -> tuple[str, str] | tuple[None, None]:
        kind = self._channel_kind(channel)
        if hasattr(channel, "topic") or hasattr(channel, "slowmode_delay") or kind in {"text", "news", "forum"}:
            return "send_messages", "messages"
        if hasattr(channel, "user_limit") or kind in {"voice", "stage"}:
            return "connect", "connections"
        return None, None

    def _channel_post_permission(self, channel) -> tuple[str, str] | tuple[None, None]:
        kind = self._channel_kind(channel)
        if "thread" in kind:
            return "send_messages_in_threads", "thread messages"
        if kind == "forum":
            return None, None
        if hasattr(channel, "topic") or hasattr(channel, "slowmode_delay") or kind in {"text", "news"}:
            return "send_messages", "messages"
        return None, None

    def _format_channel_line(self, channel) -> str:
        details = [self._channel_kind(channel)]

        category = getattr(channel, "category", None)
        if category is not None and getattr(category, "name", None):
            details.append(f"category {category.name}")

        topic = getattr(channel, "topic", None)
        if topic:
            clean_topic = " ".join(str(topic).split())
            if len(clean_topic) > 60:
                clean_topic = clean_topic[:57] + "..."
            details.append(f"topic {clean_topic}")

        slowmode_delay = getattr(channel, "slowmode_delay", None)
        if isinstance(slowmode_delay, int) and slowmode_delay > 0:
            details.append(f"slowmode {slowmode_delay}s")

        user_limit = getattr(channel, "user_limit", None)
        if isinstance(user_limit, int) and user_limit > 0:
            details.append(f"user limit {user_limit}")

        if getattr(channel, "nsfw", False):
            details.append("nsfw")

        created_at = self._format_datetime(getattr(channel, "created_at", None))
        if created_at:
            details.append(f"created {created_at}")

        return f"- #{channel.name} `{channel.id}` ({'; '.join(details)})"

    def _format_webhook_line(self, webhook) -> str:
        channel = getattr(webhook, "channel", None)
        channel_name = getattr(channel, "name", None) or "unknown-channel"
        user = getattr(webhook, "user", None)
        creator = getattr(user, "display_name", None) or getattr(user, "name", None) or ("unknown" if user is None else str(user))
        return f"- {getattr(webhook, 'name', 'unnamed')} `{getattr(webhook, 'id', 'unknown')}` (channel #{channel_name}; by {creator})"

    def _format_sticker_line(self, sticker) -> str:
        emoji = getattr(sticker, "emoji", None) or "unknown"
        description = getattr(sticker, "description", None) or "no description"
        return f"- {getattr(sticker, 'name', 'unnamed')} `{getattr(sticker, 'id', 'unknown')}` ({emoji}; {description})"

    def _format_event_line(self, event) -> str:
        start_text = self._format_datetime(getattr(event, "start_time", None)) or "unknown start"
        status = getattr(getattr(event, "status", None), "name", None) or str(getattr(event, "status", "unknown"))
        entity_type = getattr(getattr(event, "entity_type", None), "name", None) or str(getattr(event, "entity_type", "unknown"))
        recurrence = getattr(event, "recurrence_rule", None)
        recurrence_tag = " (recurring)" if recurrence is not None else ""
        return f"- {getattr(event, 'name', 'unnamed')} `{getattr(event, 'id', 'unknown')}` ({status}; {entity_type}; starts {start_text}){recurrence_tag}"

    def _format_automod_rule_line(self, rule) -> str:
        trigger = getattr(getattr(rule, "trigger", None), "type", None)
        trigger_name = getattr(trigger, "name", None) or str(trigger or "unknown")
        enabled = "enabled" if getattr(rule, "enabled", False) else "disabled"
        return f"- {getattr(rule, 'name', 'unnamed')} `{getattr(rule, 'id', 'unknown')}` ({trigger_name}; {enabled})"

    def _is_voice_channel(self, channel) -> bool:
        return self._channel_kind(channel) in {"voice", "stage"} or hasattr(channel, "user_limit")

    def _entity_type_for_voice_channel(self, channel) -> "discord.EntityType":
        """Return the correct EntityType for a voice or stage channel.

        Stage channels require ``EntityType.stage_instance``; all other
        voice-capable channels use ``EntityType.voice``.
        """
        if "stage" in self._channel_kind(channel):
            return discord.EntityType.stage_instance
        return discord.EntityType.voice

    async def _resolve_voice_channel(self, guild, reference: str):
        channel, channel_error = self._resolve_channel(guild, reference)
        if channel_error:
            return None, channel_error
        if not self._is_voice_channel(channel):
            return None, "That channel is not a voice or stage channel."
        return channel, None

    def _emoji_creator_candidates(self, emoji) -> list[str]:
        creator = getattr(emoji, "user", None)
        if creator is None:
            return []
        return [
            str(candidate)
            for candidate in (
                getattr(creator, "display_name", None),
                getattr(creator, "global_name", None),
                getattr(creator, "name", None),
                getattr(creator, "id", None),
                str(creator),
            )
            if candidate not in {None, ""}
        ]

    async def _filter_emojis(self, guild, emojis: list[object], filter_text: str) -> list[object]:
        normalized = str(filter_text or "").strip().casefold()
        if not normalized:
            return emojis

        matched: list[object] = []
        for emoji in emojis:
            emoji_name = str(getattr(emoji, "name", "")).casefold()
            emoji_id = str(getattr(emoji, "id", ""))
            if normalized in emoji_name or normalized in emoji_id:
                matched.append(emoji)
                continue

            detailed_emoji = await self._fetch_detailed_emoji(guild, emoji)
            creator_candidates = self._emoji_creator_candidates(detailed_emoji)
            if any(normalized in candidate.casefold() for candidate in creator_candidates):
                matched.append(detailed_emoji)

        return matched

    def _paginate_items(self, items: list[object], page: int, *, page_size: int) -> tuple[list[object], int]:
        total_pages = max(1, (len(items) + page_size - 1) // page_size)
        start = (page - 1) * page_size
        end = start + page_size
        return items[start:end], total_pages

    def _parse_page_or_filter_args(
        self,
        primary_arg: str | None,
        trailing_parts: tuple[str, ...],
        *,
        subject: str,
    ) -> tuple[int, str, str | None]:
        page = 1
        filter_text = ""
        if primary_arg:
            primary_arg = primary_arg.strip()
            trailing_filter = ":".join(trailing_parts).strip()
            if primary_arg.isdigit():
                page = int(primary_arg)
                filter_text = trailing_filter
            elif trailing_filter:
                return 1, "", f"When using two {subject} arguments, the first must be a page number."
            else:
                filter_text = primary_arg
        elif trailing_parts:
            filter_text = ":".join(trailing_parts).strip()

        if page < 1:
            return 1, "", f"{subject.capitalize()} page numbers start at 1."
        return page, filter_text, None

    def _parse_key_value_options(
        self,
        option_parts: tuple[str, ...],
        *,
        allowed_keys: set[str],
        subject: str,
    ) -> tuple[dict[str, str] | None, str | None]:
        options: dict[str, str] = {}
        for raw_part in option_parts:
            part = str(raw_part or "").strip()
            if not part:
                continue
            if "=" not in part:
                return None, f"{subject} options must use `key=value` segments."
            key, value = part.split("=", 1)
            normalized_key = key.strip().casefold().replace("-", "_")
            if normalized_key not in allowed_keys:
                supported = ", ".join(sorted(allowed_keys))
                return None, f"Unsupported {subject} option `{key.strip()}`. Supported: {supported}."
            options[normalized_key] = value.strip()
        return options, None

    async def _resolve_category_channel(self, guild, reference: str):
        channel, channel_error = self._resolve_channel(guild, reference)
        if channel_error:
            return None, channel_error
        if self._channel_kind(channel) != "category":
            return None, "That channel is not a category."
        return channel, None

    def _parse_optional_int(self, raw_value: str, *, label: str, minimum: int | None = None, maximum: int | None = None) -> tuple[int | None, str | None]:
        normalized = str(raw_value or "").strip()
        if not normalized:
            return None, f"Missing {label}."
        if not normalized.isdigit():
            return None, f"{label.capitalize()} must be an integer."
        value = int(normalized)
        if minimum is not None and value < minimum:
            return None, f"{label.capitalize()} must be at least {minimum}."
        if maximum is not None and value > maximum:
            return None, f"{label.capitalize()} must be at most {maximum}."
        return value, None

    def _contains_match(self, value, needle: str) -> bool:
        if value is None:
            return False
        return needle in str(value).casefold()

    def _role_matches_filter(self, guild, role, filter_text: str) -> bool:
        needle = filter_text.casefold()
        if self._contains_match(getattr(role, "name", None), needle):
            return True
        if self._contains_match(getattr(role, "id", None), needle):
            return True
        if needle == "managed" and getattr(role, "managed", False):
            return True
        if needle == "hoisted" and getattr(role, "hoist", False):
            return True
        if needle == "mentionable" and getattr(role, "mentionable", False):
            return True
        if needle in {"everyone", "@everyone"} and getattr(role, "id", None) == getattr(guild, "id", None):
            return True
        return False

    def _channel_matches_filter(self, channel, filter_text: str) -> bool:
        needle = filter_text.casefold()
        if self._contains_match(getattr(channel, "name", None), needle):
            return True
        if self._contains_match(getattr(channel, "id", None), needle):
            return True
        if self._contains_match(self._channel_kind(channel), needle):
            return True
        category = getattr(channel, "category", None)
        if self._contains_match(getattr(category, "name", None), needle):
            return True
        topic = getattr(channel, "topic", None)
        if self._contains_match(topic, needle):
            return True
        if needle == "nsfw" and getattr(channel, "nsfw", False):
            return True
        return False

    def _member_matches_filter(self, member, filter_text: str) -> bool:
        needle = filter_text.casefold()
        for candidate in (
            getattr(member, "display_name", None),
            getattr(member, "name", None),
            getattr(member, "id", None),
            str(member),
        ):
            if self._contains_match(candidate, needle):
                return True
        return False

    def _webhook_matches_filter(self, webhook, filter_text: str) -> bool:
        needle = filter_text.casefold()
        if self._contains_match(getattr(webhook, "name", None), needle):
            return True
        if self._contains_match(getattr(webhook, "id", None), needle):
            return True
        channel = getattr(webhook, "channel", None)
        if self._contains_match(getattr(channel, "name", None), needle):
            return True
        user = getattr(webhook, "user", None)
        for candidate in (
            getattr(user, "display_name", None),
            getattr(user, "name", None),
            getattr(user, "id", None),
            str(user) if user is not None else None,
        ):
            if self._contains_match(candidate, needle):
                return True
        return False

    def _sticker_matches_filter(self, sticker, filter_text: str) -> bool:
        needle = filter_text.casefold()
        for candidate in (
            getattr(sticker, "name", None),
            getattr(sticker, "id", None),
            getattr(sticker, "emoji", None),
            getattr(sticker, "description", None),
        ):
            if self._contains_match(candidate, needle):
                return True
        user = getattr(sticker, "user", None)
        for candidate in (
            getattr(user, "display_name", None),
            getattr(user, "name", None),
            getattr(user, "id", None),
            str(user) if user is not None else None,
        ):
            if self._contains_match(candidate, needle):
                return True
        return False

    def _event_matches_filter(self, event, filter_text: str) -> bool:
        needle = filter_text.casefold()
        for candidate in (
            getattr(event, "name", None),
            getattr(event, "id", None),
            getattr(getattr(event, "status", None), "name", None),
            getattr(getattr(event, "entity_type", None), "name", None),
            getattr(event, "location", None),
            getattr(getattr(event, "channel", None), "name", None),
            getattr(event, "description", None),
        ):
            if self._contains_match(candidate, needle):
                return True
        return False

    def _automod_rule_matches_filter(self, rule, filter_text: str) -> bool:
        needle = filter_text.casefold()
        for candidate in (
            getattr(rule, "name", None),
            getattr(rule, "id", None),
            getattr(getattr(getattr(rule, "trigger", None), "type", None), "name", None),
        ):
            if self._contains_match(candidate, needle):
                return True
        if needle == "enabled" and getattr(rule, "enabled", False):
            return True
        if needle == "disabled" and not getattr(rule, "enabled", False):
            return True
        return False

    def _format_emoji_line(self, emoji) -> str:
        """Render a concise review line for a custom emoji."""
        details = ["animated" if getattr(emoji, "animated", False) else "static"]

        creator = getattr(emoji, "user", None)
        if creator is not None:
            creator_name = (
                getattr(creator, "display_name", None)
                or getattr(creator, "name", None)
                or str(creator)
            )
            details.append(f"by {creator_name}")

        created_at = getattr(emoji, "created_at", None)
        created_at_text = self._format_datetime(created_at)
        if created_at_text is not None:
            details.append(created_at_text)

        if getattr(emoji, "managed", False):
            details.append("managed")
        if not getattr(emoji, "available", True):
            details.append("unavailable")
        if not getattr(emoji, "require_colons", True):
            details.append("no-colons")

        roles = list(getattr(emoji, "roles", []) or [])
        if roles:
            role_names = [getattr(role, "name", str(role)) for role in roles[:3]]
            if len(roles) > 3:
                role_names.append(f"+{len(roles) - 3} more")
            details.append("roles: " + ", ".join(role_names))
        else:
            details.append("roles: unrestricted")

        return f"- :{emoji.name}: `{emoji.id}` ({'; '.join(details)})"

    def _parse_timeout_duration(self, raw_duration: str) -> tuple[timedelta | None, str | None]:
        normalized = "".join(str(raw_duration or "").split()).lower()
        if not normalized:
            return None, "Missing timeout duration."

        total_seconds = 0
        consumed = 0
        unit_seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
        for match in DURATION_PART_RE.finditer(normalized):
            value = int(match.group("value"))
            unit = match.group("unit").lower()
            total_seconds += value * unit_seconds[unit]
            consumed += len(match.group(0))

        if consumed != len(normalized) or total_seconds <= 0:
            return None, "Invalid duration. Use values like `10m`, `2h`, `3d`, or `1w2d`."
        if total_seconds > 28 * 24 * 60 * 60:
            return None, "Discord timeouts max out at 28 days."
        return timedelta(seconds=total_seconds), None

    async def _select_emoji_source_attachment(self, ctx, selector: str | None = None):
        attachments = list(getattr(getattr(ctx, "message", None), "attachments", []) or [])
        collect_source_attachments = getattr(self, "_collect_source_attachments", None)
        if callable(collect_source_attachments) and getattr(ctx, "message", None) is not None:
            try:
                attachments, _warnings = await collect_source_attachments(
                    ctx.message,
                    getattr(ctx.message, "content", "") or "",
                )
            except Exception:
                attachments = list(getattr(getattr(ctx, "message", None), "attachments", []) or [])
        image_attachments = [
            attachment for attachment in attachments
            if re.search(r"\.(png|jpe?g|gif|webp)$", str(getattr(attachment, "filename", "")), re.IGNORECASE)
        ]
        if not image_attachments:
            return None, "No image attachment found. Attach a PNG, JPG, GIF, or WEBP image."

        if not selector:
            return image_attachments[0], None

        selector = selector.strip()
        if selector.isdigit():
            selector_id = int(selector)
            for attachment in image_attachments:
                if getattr(attachment, "id", None) == selector_id:
                    return attachment, None
            if 1 <= selector_id <= len(image_attachments):
                return image_attachments[selector_id - 1], None

        for attachment in image_attachments:
            filename = getattr(attachment, "filename", "")
            if selector == filename or selector.casefold() == str(filename).casefold():
                return attachment, None

        return None, f"No image attachment matched `{selector}`."

    async def _select_sticker_source_attachment(self, ctx, selector: str | None = None):
        attachments = list(getattr(getattr(ctx, "message", None), "attachments", []) or [])
        valid = [
            attachment for attachment in attachments
            if re.search(r"\.(png|apng|gif|json)$", str(getattr(attachment, "filename", "")), re.IGNORECASE)
        ]
        if not valid:
            return None, "No sticker source attachment found. Attach a PNG, APNG, GIF, or JSON sticker file."
        if not selector:
            return valid[0], None
        selector = selector.strip()
        if selector.isdigit():
            selector_id = int(selector)
            for attachment in valid:
                if getattr(attachment, "id", None) == selector_id:
                    return attachment, None
            if 1 <= selector_id <= len(valid):
                return valid[selector_id - 1], None
        for attachment in valid:
            filename = getattr(attachment, "filename", "")
            if selector == filename or selector.casefold() == str(filename).casefold():
                return attachment, None
        return None, f"No sticker attachment matched `{selector}`."

    @llm_command("kick", description="Kick a server member (authorized admin)", args="member:reason?")
    async def cmd_kick(self, ctx, member_ref: str, *reason_parts: str) -> CmdResult:
        guild, bot_member, error = await self._ensure_admin_command(ctx, action="kick members", required_permissions=["kick_members"])
        if error:
            return error
        member, resolve_error = await self._resolve_member(guild, member_ref)
        if resolve_error:
            return CmdResult(success=False, error=resolve_error)
        hierarchy_error = self._can_bot_manage_member(guild, bot_member, member)
        if hierarchy_error:
            return CmdResult(success=False, error=hierarchy_error)
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Kick", reason)

        async def execute() -> CmdResult:
            try:
                await member.kick(reason=audit_reason)
            except discord.Forbidden:
                return CmdResult(success=False, error="Discord denied the kick. Check role hierarchy and permissions.")
            except discord.HTTPException as exc:
                return CmdResult(success=False, error=f"Kick failed: {exc}")
            return CmdResult(success=True, message=f"Kicked {member.display_name}.")

        return await self._request_destructive_confirmation(ctx, summary=f"kick {member.display_name}", executor=execute)

    @llm_command("ban", description="Ban a server member (authorized admin)", args="member:reason?")
    async def cmd_ban(self, ctx, member_ref: str, *reason_parts: str) -> CmdResult:
        guild, bot_member, error = await self._ensure_admin_command(ctx, action="ban members", required_permissions=["ban_members"])
        if error:
            return error
        member, resolve_error = await self._resolve_member(guild, member_ref)
        if resolve_error:
            return CmdResult(success=False, error=resolve_error)
        hierarchy_error = self._can_bot_manage_member(guild, bot_member, member)
        if hierarchy_error:
            return CmdResult(success=False, error=hierarchy_error)
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Ban", reason)

        async def execute() -> CmdResult:
            try:
                await guild.ban(member, reason=audit_reason)
            except discord.Forbidden:
                return CmdResult(success=False, error="Discord denied the ban. Check role hierarchy and permissions.")
            except discord.HTTPException as exc:
                return CmdResult(success=False, error=f"Ban failed: {exc}")
            return CmdResult(success=True, message=f"Banned {member.display_name}.")

        return await self._request_destructive_confirmation(ctx, summary=f"ban {member.display_name}", executor=execute)

    @llm_command("softban", description="Softban a server member and clear one day of messages (authorized admin)", args="member:reason?")
    async def cmd_softban(self, ctx, member_ref: str, *reason_parts: str) -> CmdResult:
        guild, bot_member, error = await self._ensure_admin_command(ctx, action="softban members", required_permissions=["ban_members"])
        if error:
            return error
        member, resolve_error = await self._resolve_member(guild, member_ref)
        if resolve_error:
            return CmdResult(success=False, error=resolve_error)
        hierarchy_error = self._can_bot_manage_member(guild, bot_member, member)
        if hierarchy_error:
            return CmdResult(success=False, error=hierarchy_error)
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Softban", reason)

        async def execute() -> CmdResult:
            try:
                await guild.ban(member, reason=audit_reason, delete_message_seconds=86400)
                await guild.unban(member, reason=audit_reason)
            except discord.Forbidden:
                return CmdResult(success=False, error="Discord denied the softban. Check hierarchy and permissions.")
            except discord.HTTPException as exc:
                return CmdResult(success=False, error=f"Softban failed: {exc}")
            return CmdResult(success=True, message=f"Softbanned {member.display_name}.")

        return await self._request_destructive_confirmation(ctx, summary=f"softban {member.display_name}", executor=execute)

    @llm_command("massban", description="Ban multiple users by member ref or user ID (authorized admin)", args="targets_csv:delete_days?:reason?")
    async def cmd_massban(self, ctx, targets_csv: str, delete_days_text: str | None = None, *reason_parts: str) -> CmdResult:
        guild, bot_member, error = await self._ensure_admin_command(ctx, action="massban users", required_permissions=["ban_members"])
        if error:
            return error

        delete_days = 0
        if delete_days_text:
            if not delete_days_text.isdigit():
                return CmdResult(success=False, error="Massban delete days must be an integer from 0 to 7.")
            delete_days = int(delete_days_text)
            if not 0 <= delete_days <= 7:
                return CmdResult(success=False, error="Massban delete days must be between 0 and 7.")

        raw_refs = [part.strip() for part in targets_csv.split(",") if part.strip()]
        if not raw_refs:
            return CmdResult(success=False, error="Massban requires at least one member reference or user ID.")

        targets = []
        seen_ids = set()
        for reference in raw_refs:
            target, resolve_error = await self._resolve_ban_target(guild, reference)
            if resolve_error:
                return CmdResult(success=False, error=resolve_error)
            target_id = getattr(target, "id", None)
            if target_id in seen_ids:
                continue
            if isinstance(target, discord.Member):
                hierarchy_error = self._can_bot_manage_member(guild, bot_member, target)
                if hierarchy_error:
                    return CmdResult(success=False, error=f"{reference}: {hierarchy_error}")
            seen_ids.add(target_id)
            targets.append(target)

        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Massban", reason)

        async def execute() -> CmdResult:
            banned = []
            for target in targets:
                try:
                    await guild.ban(target, reason=audit_reason, delete_message_seconds=delete_days * 86400)
                except discord.Forbidden:
                    return CmdResult(success=False, error="Discord denied part of the massban. Check hierarchy and permissions.")
                except discord.HTTPException as exc:
                    target_label = getattr(target, "display_name", None) or getattr(target, "id", "unknown user")
                    return CmdResult(success=False, error=f"Massban failed on {target_label}: {exc}")
                banned.append(str(getattr(target, "display_name", None) or getattr(target, "id", "unknown")))
            return CmdResult(success=True, message=f"Banned {len(banned)} user(s).")

        return await self._request_destructive_confirmation(ctx, summary=f"massban {len(targets)} user(s)", executor=execute)

    @llm_command("unban", description="Unban a user (authorized admin)", args="user:reason?")
    async def cmd_unban(self, ctx, user_ref: str, *reason_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="unban users", required_permissions=["ban_members"])
        if error:
            return error
        entry, resolve_error = await self._resolve_ban_entry(guild, user_ref)
        if resolve_error:
            return CmdResult(success=False, error=resolve_error)
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Unban", reason)

        async def execute() -> CmdResult:
            try:
                await guild.unban(entry.user, reason=audit_reason)
            except discord.Forbidden:
                return CmdResult(success=False, error="Discord denied the unban.")
            except discord.HTTPException as exc:
                return CmdResult(success=False, error=f"Unban failed: {exc}")
            return CmdResult(success=True, message=f"Unbanned {entry.user}.")

        return await self._request_destructive_confirmation(ctx, summary=f"unban {entry.user}", executor=execute)

    @llm_command("timeout", description="Timeout a member (authorized admin)", args="member:duration:reason?")
    async def cmd_timeout(self, ctx, member_ref: str, duration_text: str, *reason_parts: str) -> CmdResult:
        guild, bot_member, error = await self._ensure_admin_command(ctx, action="timeout members", required_permissions=["moderate_members"])
        if error:
            return error
        member, resolve_error = await self._resolve_member(guild, member_ref)
        if resolve_error:
            return CmdResult(success=False, error=resolve_error)
        hierarchy_error = self._can_bot_manage_member(guild, bot_member, member)
        if hierarchy_error:
            return CmdResult(success=False, error=hierarchy_error)
        duration, duration_error = self._parse_timeout_duration(duration_text)
        if duration_error:
            return CmdResult(success=False, error=duration_error)
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Timeout", reason)

        async def execute() -> CmdResult:
            try:
                await member.timeout(discord.utils.utcnow() + duration, reason=audit_reason)
            except discord.Forbidden:
                return CmdResult(success=False, error="Discord denied the timeout. Check role hierarchy and permissions.")
            except discord.HTTPException as exc:
                return CmdResult(success=False, error=f"Timeout failed: {exc}")
            return CmdResult(success=True, message=f"Timed out {member.display_name} for {duration_text}.")

        return await self._request_destructive_confirmation(ctx, summary=f"timeout {member.display_name} for {duration_text}", executor=execute)

    @llm_command("untimeout", description="Remove a member timeout (authorized admin)", args="member:reason?")
    async def cmd_untimeout(self, ctx, member_ref: str, *reason_parts: str) -> CmdResult:
        guild, bot_member, error = await self._ensure_admin_command(ctx, action="remove timeouts", required_permissions=["moderate_members"])
        if error:
            return error
        member, resolve_error = await self._resolve_member(guild, member_ref)
        if resolve_error:
            return CmdResult(success=False, error=resolve_error)
        hierarchy_error = self._can_bot_manage_member(guild, bot_member, member)
        if hierarchy_error:
            return CmdResult(success=False, error=hierarchy_error)
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Remove timeout", reason)

        async def execute() -> CmdResult:
            try:
                await member.timeout(None, reason=audit_reason)
            except discord.Forbidden:
                return CmdResult(success=False, error="Discord denied the timeout removal.")
            except discord.HTTPException as exc:
                return CmdResult(success=False, error=f"Timeout removal failed: {exc}")
            return CmdResult(success=True, message=f"Removed timeout from {member.display_name}.")

        return await self._request_destructive_confirmation(ctx, summary=f"remove timeout from {member.display_name}", executor=execute)

    @llm_command("add_role", description="Add a role to a member (authorized admin)", args="member:role:reason?")
    async def cmd_add_role(self, ctx, member_ref: str, role_ref: str, *reason_parts: str) -> CmdResult:
        guild, bot_member, error = await self._ensure_admin_command(ctx, action="manage roles", required_permissions=["manage_roles"])
        if error:
            return error
        member, resolve_error = await self._resolve_member(guild, member_ref)
        if resolve_error:
            return CmdResult(success=False, error=resolve_error)
        hierarchy_error = self._can_bot_manage_member(guild, bot_member, member)
        if hierarchy_error:
            return CmdResult(success=False, error=hierarchy_error)
        role, role_error = self._resolve_role(guild, role_ref)
        if role_error:
            return CmdResult(success=False, error=role_error)
        role_manage_error = self._can_bot_manage_role(guild, bot_member, role)
        if role_manage_error:
            return CmdResult(success=False, error=role_manage_error)
        if role in getattr(member, "roles", []):
            return CmdResult(success=True, message=f"{member.display_name} already has `{role.name}`.")
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Add role", reason)
        try:
            await member.add_roles(role, reason=audit_reason)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the role update.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Role update failed: {exc}")
        return CmdResult(success=True, message=f"Added `{role.name}` to {member.display_name}.")

    @llm_command("remove_role", description="Remove a role from a member (authorized admin)", args="member:role:reason?")
    async def cmd_remove_role(self, ctx, member_ref: str, role_ref: str, *reason_parts: str) -> CmdResult:
        guild, bot_member, error = await self._ensure_admin_command(ctx, action="manage roles", required_permissions=["manage_roles"])
        if error:
            return error
        member, resolve_error = await self._resolve_member(guild, member_ref)
        if resolve_error:
            return CmdResult(success=False, error=resolve_error)
        hierarchy_error = self._can_bot_manage_member(guild, bot_member, member)
        if hierarchy_error:
            return CmdResult(success=False, error=hierarchy_error)
        role, role_error = self._resolve_role(guild, role_ref)
        if role_error:
            return CmdResult(success=False, error=role_error)
        role_manage_error = self._can_bot_manage_role(guild, bot_member, role)
        if role_manage_error:
            return CmdResult(success=False, error=role_manage_error)
        if role not in getattr(member, "roles", []):
            return CmdResult(success=True, message=f"{member.display_name} doesn't have `{role.name}`.")
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Remove role", reason)

        async def execute() -> CmdResult:
            try:
                await member.remove_roles(role, reason=audit_reason)
            except discord.Forbidden:
                return CmdResult(success=False, error="Discord denied the role update.")
            except discord.HTTPException as exc:
                return CmdResult(success=False, error=f"Role update failed: {exc}")
            return CmdResult(success=True, message=f"Removed `{role.name}` from {member.display_name}.")

        return await self._request_destructive_confirmation(ctx, summary=f"remove role {role.name} from {member.display_name}", executor=execute)

    @llm_command("set_nick", description="Set a member nickname (authorized admin)", args="member:nickname:reason?")
    async def cmd_set_nick(self, ctx, member_ref: str, nickname: str, *reason_parts: str) -> CmdResult:
        guild, bot_member, error = await self._ensure_admin_command(ctx, action="manage nicknames", required_permissions=["manage_nicknames"])
        if error:
            return error
        member, resolve_error = await self._resolve_member(guild, member_ref)
        if resolve_error:
            return CmdResult(success=False, error=resolve_error)
        hierarchy_error = self._can_bot_manage_member(guild, bot_member, member)
        if hierarchy_error:
            return CmdResult(success=False, error=hierarchy_error)
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Set nickname", reason)
        try:
            await member.edit(nick=nickname, reason=audit_reason)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the nickname change.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Nickname change failed: {exc}")
        return CmdResult(success=True, message=f"Nickname set for {member.display_name}.")

    @llm_command("clear_nick", description="Clear a member nickname (authorized admin)", args="member:reason?")
    async def cmd_clear_nick(self, ctx, member_ref: str, *reason_parts: str) -> CmdResult:
        guild, bot_member, error = await self._ensure_admin_command(ctx, action="manage nicknames", required_permissions=["manage_nicknames"])
        if error:
            return error
        member, resolve_error = await self._resolve_member(guild, member_ref)
        if resolve_error:
            return CmdResult(success=False, error=resolve_error)
        hierarchy_error = self._can_bot_manage_member(guild, bot_member, member)
        if hierarchy_error:
            return CmdResult(success=False, error=hierarchy_error)
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Clear nickname", reason)

        async def execute() -> CmdResult:
            try:
                await member.edit(nick=None, reason=audit_reason)
            except discord.Forbidden:
                return CmdResult(success=False, error="Discord denied the nickname clear.")
            except discord.HTTPException as exc:
                return CmdResult(success=False, error=f"Nickname clear failed: {exc}")
            return CmdResult(success=True, message=f"Nickname cleared for {member.display_name}.")

        return await self._request_destructive_confirmation(ctx, summary=f"clear nickname for {member.display_name}", executor=execute)

    @llm_command("voice_kick", description="Disconnect a member from voice (authorized admin)", args="member:reason?")
    async def cmd_voice_kick(self, ctx, member_ref: str, *reason_parts: str) -> CmdResult:
        guild, bot_member, error = await self._ensure_admin_command(ctx, action="disconnect members from voice", required_permissions=["move_members"])
        if error:
            return error
        member, resolve_error = await self._resolve_member(guild, member_ref)
        if resolve_error:
            return CmdResult(success=False, error=resolve_error)
        hierarchy_error = self._can_bot_manage_member(guild, bot_member, member)
        if hierarchy_error:
            return CmdResult(success=False, error=hierarchy_error)
        if getattr(getattr(member, "voice", None), "channel", None) is None:
            return CmdResult(success=False, error=f"{member.display_name} is not in voice.")
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Voice kick", reason)

        async def execute() -> CmdResult:
            try:
                await member.move_to(None, reason=audit_reason)
            except discord.Forbidden:
                return CmdResult(success=False, error="Discord denied the voice disconnect.")
            except discord.HTTPException as exc:
                return CmdResult(success=False, error=f"Voice disconnect failed: {exc}")
            return CmdResult(success=True, message=f"Disconnected {member.display_name} from voice.")

        return await self._request_destructive_confirmation(ctx, summary=f"disconnect {member.display_name} from voice", executor=execute)

    @llm_command("voice_move", description="Move a member to another voice channel (authorized admin)", args="member:channel:reason?")
    async def cmd_voice_move(self, ctx, member_ref: str, channel_ref: str, *reason_parts: str) -> CmdResult:
        guild, bot_member, error = await self._ensure_admin_command(ctx, action="move members in voice", required_permissions=["move_members"])
        if error:
            return error
        member, resolve_error = await self._resolve_member(guild, member_ref)
        if resolve_error:
            return CmdResult(success=False, error=resolve_error)
        hierarchy_error = self._can_bot_manage_member(guild, bot_member, member)
        if hierarchy_error:
            return CmdResult(success=False, error=hierarchy_error)
        target_channel, channel_error = await self._resolve_voice_channel(guild, channel_ref)
        if channel_error:
            return CmdResult(success=False, error=channel_error)
        if getattr(getattr(member, "voice", None), "channel", None) is None:
            return CmdResult(success=False, error=f"{member.display_name} is not in voice.")
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Voice move", reason)
        try:
            await member.move_to(target_channel, reason=audit_reason)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the voice move.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Voice move failed: {exc}")
        return CmdResult(success=True, message=f"Moved {member.display_name} to `#{target_channel.name}`.")

    @llm_command("voice_mute", description="Server mute a member in voice (authorized admin)", args="member:reason?")
    async def cmd_voice_mute(self, ctx, member_ref: str, *reason_parts: str) -> CmdResult:
        guild, bot_member, error = await self._ensure_admin_command(ctx, action="server mute members", required_permissions=["mute_members"])
        if error:
            return error
        member, resolve_error = await self._resolve_member(guild, member_ref)
        if resolve_error:
            return CmdResult(success=False, error=resolve_error)
        hierarchy_error = self._can_bot_manage_member(guild, bot_member, member)
        if hierarchy_error:
            return CmdResult(success=False, error=hierarchy_error)
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Voice mute", reason)
        try:
            await member.edit(mute=True, reason=audit_reason)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the voice mute.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Voice mute failed: {exc}")
        return CmdResult(success=True, message=f"Server-muted {member.display_name}.")

    @llm_command("voice_unmute", description="Remove a server mute from a member (authorized admin)", args="member:reason?")
    async def cmd_voice_unmute(self, ctx, member_ref: str, *reason_parts: str) -> CmdResult:
        guild, bot_member, error = await self._ensure_admin_command(ctx, action="remove server mutes", required_permissions=["mute_members"])
        if error:
            return error
        member, resolve_error = await self._resolve_member(guild, member_ref)
        if resolve_error:
            return CmdResult(success=False, error=resolve_error)
        hierarchy_error = self._can_bot_manage_member(guild, bot_member, member)
        if hierarchy_error:
            return CmdResult(success=False, error=hierarchy_error)
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Voice unmute", reason)
        try:
            await member.edit(mute=False, reason=audit_reason)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the voice unmute.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Voice unmute failed: {exc}")
        return CmdResult(success=True, message=f"Removed server mute from {member.display_name}.")

    @llm_command("voice_deafen", description="Server deafen a member in voice (authorized admin)", args="member:reason?")
    async def cmd_voice_deafen(self, ctx, member_ref: str, *reason_parts: str) -> CmdResult:
        guild, bot_member, error = await self._ensure_admin_command(ctx, action="server deafen members", required_permissions=["deafen_members"])
        if error:
            return error
        member, resolve_error = await self._resolve_member(guild, member_ref)
        if resolve_error:
            return CmdResult(success=False, error=resolve_error)
        hierarchy_error = self._can_bot_manage_member(guild, bot_member, member)
        if hierarchy_error:
            return CmdResult(success=False, error=hierarchy_error)
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Voice deafen", reason)
        try:
            await member.edit(deafen=True, reason=audit_reason)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the voice deafen.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Voice deafen failed: {exc}")
        return CmdResult(success=True, message=f"Server-deafened {member.display_name}.")

    @llm_command("voice_undeafen", description="Remove a server deafening from a member (authorized admin)", args="member:reason?")
    async def cmd_voice_undeafen(self, ctx, member_ref: str, *reason_parts: str) -> CmdResult:
        guild, bot_member, error = await self._ensure_admin_command(ctx, action="remove server deafens", required_permissions=["deafen_members"])
        if error:
            return error
        member, resolve_error = await self._resolve_member(guild, member_ref)
        if resolve_error:
            return CmdResult(success=False, error=resolve_error)
        hierarchy_error = self._can_bot_manage_member(guild, bot_member, member)
        if hierarchy_error:
            return CmdResult(success=False, error=hierarchy_error)
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Voice undeafen", reason)
        try:
            await member.edit(deafen=False, reason=audit_reason)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the voice undeafen.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Voice undeafen failed: {exc}")
        return CmdResult(success=True, message=f"Removed server deafening from {member.display_name}.")

    @llm_command("emoji_create", description="Create a custom emoji from an attached image (authorized admin)", args="name:attachment?")
    async def cmd_emoji_create(self, ctx, name: str, attachment_selector: str | None = None) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage emojis", required_permissions=["manage_emojis_and_stickers"])
        if error:
            return error
        normalized_name = re.sub(r"[^A-Za-z0-9_]", "_", name.strip())
        normalized_name = re.sub(r"_+", "_", normalized_name).strip("_")
        if not normalized_name or len(normalized_name) < 2:
            return CmdResult(success=False, error="Emoji names must resolve to at least 2 alphanumeric/underscore characters.")
        if len(normalized_name) > 32:
            return CmdResult(success=False, error="Emoji names max out at 32 characters.")
        attachment, attachment_error = await self._select_emoji_source_attachment(ctx, attachment_selector)
        if attachment_error:
            return CmdResult(success=False, error=attachment_error)
        try:
            image_bytes = await attachment.read()
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Couldn't read the image attachment: {exc}")
        if len(image_bytes) > 256 * 1024:
            return CmdResult(success=False, error="Emoji source images must be 256KB or smaller.")
        audit_reason = self._build_audit_reason(ctx, "Create emoji", f"name={normalized_name}")
        try:
            emoji = await guild.create_custom_emoji(name=normalized_name, image=image_bytes, reason=audit_reason)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied emoji creation.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Emoji creation failed: {exc}")
        return CmdResult(success=True, message=f"Created emoji `:{emoji.name}:` ({emoji.id}).")

    @llm_command("emoji_inspect", description="Inspect one custom emoji with a fresh API fetch (authorized admin)", args="emoji")
    async def cmd_emoji_inspect(self, ctx, emoji_ref: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="inspect emojis", required_permissions=["manage_emojis_and_stickers"])
        if error:
            return error
        emoji, resolve_error = self._resolve_emoji(guild, emoji_ref)
        if resolve_error:
            return CmdResult(success=False, error=resolve_error)

        detailed_emoji = await self._fetch_detailed_emoji(guild, emoji)

        creator = getattr(detailed_emoji, "user", None)
        creator_text = (
            getattr(creator, "display_name", None)
            or getattr(creator, "name", None)
            or str(creator)
            if creator is not None else "unknown"
        )
        roles = list(getattr(detailed_emoji, "roles", []) or [])
        role_text = ", ".join(getattr(role, "name", str(role)) for role in roles) if roles else "unrestricted"
        created_at_text = self._format_datetime(getattr(detailed_emoji, "created_at", None)) or "unknown"
        animated_text = "animated" if getattr(detailed_emoji, "animated", False) else "static"
        managed_text = "yes" if getattr(detailed_emoji, "managed", False) else "no"
        available_text = "yes" if getattr(detailed_emoji, "available", True) else "no"

        return CmdResult(
            success=True,
            message=(
                f"Emoji `:{detailed_emoji.name}:` `{detailed_emoji.id}`\n"
                f"- type: {animated_text}\n"
                f"- created: {created_at_text}\n"
                f"- added by: {creator_text}\n"
                f"- managed: {managed_text}\n"
                f"- available: {available_text}\n"
                f"- roles: {role_text}"
            ),
        )

    @llm_command("emoji_list", description="List custom emojis in the current server with optional page/filter (authorized admin)", args="page_or_filter?:filter?")
    async def cmd_emoji_list(self, ctx, page_or_filter: str | None = None, *filter_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(
            ctx,
            action="review emojis",
            required_permissions=["manage_emojis_and_stickers"],
            bot_required_permissions=[],
        )
        if error:
            return error

        page = 1
        filter_text = ""
        if page_or_filter:
            page_or_filter = page_or_filter.strip()
            trailing_filter = ":".join(filter_parts).strip()
            if page_or_filter.isdigit():
                page = int(page_or_filter)
                filter_text = trailing_filter
            elif trailing_filter:
                return CmdResult(success=False, error="When using two emoji_list arguments, the first must be a page number.")
            else:
                filter_text = page_or_filter
        elif filter_parts:
            filter_text = ":".join(filter_parts).strip()

        if page < 1:
            return CmdResult(success=False, error="Emoji list page numbers start at 1.")

        emojis = sorted(list(getattr(guild, "emojis", []) or []), key=lambda emoji: str(getattr(emoji, "name", "")).casefold())
        if not emojis:
            return CmdResult(success=True, message="No custom emojis in this server.")

        filtered_emojis = await self._filter_emojis(guild, emojis, filter_text)
        if not filtered_emojis:
            return CmdResult(success=True, message=f"No custom emojis matched `{filter_text}`.")

        page_items, total_pages = self._paginate_items(filtered_emojis, page, page_size=REVIEW_LIST_PAGE_SIZE)
        if not page_items:
            return CmdResult(
                success=False,
                error=f"Emoji list page `{page}` is out of range. There {'is' if total_pages == 1 else 'are'} {total_pages} page{'s' if total_pages != 1 else ''}.",
            )

        lines = [self._format_emoji_line(emoji) for emoji in page_items]
        page_header = (
            f"Custom emojis in {getattr(guild, 'name', 'this server')}"
            f"{f' matching `{filter_text}`' if filter_text else ''} "
            f"(page {page}/{total_pages}; {len(filtered_emojis)} match{'es' if len(filtered_emojis) != 1 else ''}):"
        )
        message = "\n".join([page_header, *lines])
        return CmdResult(success=True, message=message)

    @llm_command("emoji_delete", description="Delete a custom emoji (authorized admin)", args="emoji:reason?")
    async def cmd_emoji_delete(self, ctx, emoji_ref: str, *reason_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage emojis", required_permissions=["manage_emojis_and_stickers"])
        if error:
            return error
        emoji, resolve_error = self._resolve_emoji(guild, emoji_ref)
        if resolve_error:
            return CmdResult(success=False, error=resolve_error)
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Delete emoji", reason)

        async def execute() -> CmdResult:
            try:
                await emoji.delete(reason=audit_reason)
            except discord.Forbidden:
                return CmdResult(success=False, error="Discord denied the emoji delete.")
            except discord.HTTPException as exc:
                return CmdResult(success=False, error=f"Emoji delete failed: {exc}")
            return CmdResult(success=True, message=f"Deleted emoji `:{emoji.name}:`.")

        return await self._request_destructive_confirmation(ctx, summary=f"delete emoji :{emoji.name}:", executor=execute)

    @llm_command("emoji_rename", description="Rename a custom emoji (authorized admin)", args="emoji:new_name:reason?")
    async def cmd_emoji_rename(self, ctx, emoji_ref: str, new_name: str, *reason_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage emojis", required_permissions=["manage_emojis_and_stickers"])
        if error:
            return error
        emoji, resolve_error = self._resolve_emoji(guild, emoji_ref)
        if resolve_error:
            return CmdResult(success=False, error=resolve_error)
        normalized_name = re.sub(r"[^A-Za-z0-9_]", "_", new_name.strip())
        normalized_name = re.sub(r"_+", "_", normalized_name).strip("_")
        if not normalized_name or len(normalized_name) < 2:
            return CmdResult(success=False, error="Emoji names must resolve to at least 2 alphanumeric/underscore characters.")
        if len(normalized_name) > 32:
            return CmdResult(success=False, error="Emoji names max out at 32 characters.")
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Rename emoji", reason)
        try:
            await emoji.edit(name=normalized_name, reason=audit_reason)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the emoji rename.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Emoji rename failed: {exc}")
        return CmdResult(success=True, message=f"Renamed emoji to `:{normalized_name}:`.")

    @llm_command("role_list", description="List server roles with optional page/filter (authorized admin)", args="page_or_filter?:filter?")
    async def cmd_role_list(self, ctx, page_or_filter: str | None = None, *filter_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(
            ctx,
            action="review roles",
            required_permissions=["manage_roles"],
            bot_required_permissions=[],
        )
        if error:
            return error
        page, filter_text, parse_error = self._parse_page_or_filter_args(page_or_filter, filter_parts, subject="role list")
        if parse_error:
            return CmdResult(success=False, error=parse_error)
        roles = sorted(list(getattr(guild, "roles", []) or []), key=lambda role: (-self._role_position(role), getattr(role, "id", 0)))
        if not roles:
            return CmdResult(success=True, message="No roles found in this server.")
        filtered_roles = [role for role in roles if not filter_text or self._role_matches_filter(guild, role, filter_text)]
        if not filtered_roles:
            return CmdResult(success=True, message=f"No roles matched `{filter_text}`.")
        page_items, total_pages = self._paginate_items(filtered_roles, page, page_size=REVIEW_LIST_PAGE_SIZE)
        if not page_items:
            return CmdResult(success=False, error=f"Role list page `{page}` is out of range. There {'is' if total_pages == 1 else 'are'} {total_pages} page{'s' if total_pages != 1 else ''}.")
        lines = [self._format_role_line(guild, role) for role in page_items]
        return CmdResult(
            success=True,
            message="\n".join([
                f"Roles in {getattr(guild, 'name', 'this server')}{f' matching `{filter_text}`' if filter_text else ''} (page {page}/{total_pages}; {len(filtered_roles)} match{'es' if len(filtered_roles) != 1 else ''}):",
                *lines,
            ]),
        )

    @llm_command("role_create", description="Create a server role (authorized admin)", args="name")
    async def cmd_role_create(self, ctx, name: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage roles", required_permissions=["manage_roles"])
        if error:
            return error
        normalized_name = name.strip()
        if not normalized_name:
            return CmdResult(success=False, error="Role name can't be empty.")
        audit_reason = self._build_audit_reason(ctx, "Create role", f"name={normalized_name}")
        try:
            role = await guild.create_role(name=normalized_name, reason=audit_reason)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the role creation.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Role creation failed: {exc}")
        return CmdResult(success=True, message=f"Created role `@{role.name}` ({role.id}).")

    @llm_command("role_create_config", description="Create a server role with keyed options (authorized admin)", args="name:option=value...")
    async def cmd_role_create_config(self, ctx, name: str, *option_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage roles", required_permissions=["manage_roles"])
        if error:
            return error
        normalized_name = name.strip()
        if not normalized_name:
            return CmdResult(success=False, error="Role name can't be empty.")
        options, options_error = self._parse_key_value_options(
            option_parts,
            allowed_keys={"color", "colour", "hoist", "mentionable"},
            subject="role create",
        )
        if options_error:
            return CmdResult(success=False, error=options_error)

        create_kwargs = {"name": normalized_name}
        if "color" in options or "colour" in options:
            colour_value, colour_error = self._parse_role_colour(options.get("color") or options.get("colour"))
            if colour_error:
                return CmdResult(success=False, error=colour_error)
            create_kwargs["color"] = colour_value
        if "hoist" in options:
            hoist_value, toggle_error = self._parse_toggle_value(options["hoist"], label="role hoist")
            if toggle_error:
                return CmdResult(success=False, error=toggle_error)
            create_kwargs["hoist"] = hoist_value
        if "mentionable" in options:
            mentionable_value, toggle_error = self._parse_toggle_value(options["mentionable"], label="role mentionable")
            if toggle_error:
                return CmdResult(success=False, error=toggle_error)
            create_kwargs["mentionable"] = mentionable_value

        audit_reason = self._build_audit_reason(ctx, "Create role", f"name={normalized_name}")
        try:
            role = await guild.create_role(reason=audit_reason, **create_kwargs)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the role creation.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Role creation failed: {exc}")
        return CmdResult(success=True, message=f"Created role `@{role.name}` ({role.id}).")

    @llm_command("role_rename", description="Rename a server role (authorized admin)", args="role:new_name:reason?")
    async def cmd_role_rename(self, ctx, role_ref: str, new_name: str, *reason_parts: str) -> CmdResult:
        guild, bot_member, error = await self._ensure_admin_command(ctx, action="manage roles", required_permissions=["manage_roles"])
        if error:
            return error
        role, role_error = self._resolve_role(guild, role_ref)
        if role_error:
            return CmdResult(success=False, error=role_error)
        role_manage_error = self._can_bot_manage_role(guild, bot_member, role)
        if role_manage_error:
            return CmdResult(success=False, error=role_manage_error)
        normalized_name = new_name.strip()
        if not normalized_name:
            return CmdResult(success=False, error="Role name can't be empty.")
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Rename role", reason)
        try:
            await role.edit(name=normalized_name, reason=audit_reason)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the role rename.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Role rename failed: {exc}")
        return CmdResult(success=True, message=f"Renamed role to `@{normalized_name}`.")

    @llm_command("role_delete", description="Delete a server role (authorized admin)", args="role:reason?")
    async def cmd_role_delete(self, ctx, role_ref: str, *reason_parts: str) -> CmdResult:
        guild, bot_member, error = await self._ensure_admin_command(ctx, action="manage roles", required_permissions=["manage_roles"])
        if error:
            return error
        role, role_error = self._resolve_role(guild, role_ref)
        if role_error:
            return CmdResult(success=False, error=role_error)
        role_manage_error = self._can_bot_manage_role(guild, bot_member, role)
        if role_manage_error:
            return CmdResult(success=False, error=role_manage_error)
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Delete role", reason)

        async def execute() -> CmdResult:
            try:
                await role.delete(reason=audit_reason)
            except discord.Forbidden:
                return CmdResult(success=False, error="Discord denied the role delete.")
            except discord.HTTPException as exc:
                return CmdResult(success=False, error=f"Role delete failed: {exc}")
            return CmdResult(success=True, message=f"Deleted role `@{role.name}`.")

        return await self._request_destructive_confirmation(ctx, summary=f"delete role @{role.name}", executor=execute)

    @llm_command("role_color", description="Set or clear a role color (authorized admin)", args="role:color:reason?")
    async def cmd_role_color(self, ctx, role_ref: str, colour_text: str, *reason_parts: str) -> CmdResult:
        guild, bot_member, error = await self._ensure_admin_command(ctx, action="manage roles", required_permissions=["manage_roles"])
        if error:
            return error
        role, role_error = self._resolve_role(guild, role_ref)
        if role_error:
            return CmdResult(success=False, error=role_error)
        role_manage_error = self._can_bot_manage_role(guild, bot_member, role)
        if role_manage_error:
            return CmdResult(success=False, error=role_manage_error)
        colour_value, colour_error = self._parse_role_colour(colour_text)
        if colour_error:
            return CmdResult(success=False, error=colour_error)
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Set role color", reason)
        try:
            await role.edit(color=colour_value, reason=audit_reason)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the role color update.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Role color update failed: {exc}")
        if colour_value == 0:
            return CmdResult(success=True, message=f"Cleared the color for `@{role.name}`.")
        return CmdResult(success=True, message=f"Updated the color for `@{role.name}`.")

    @llm_command("role_hoist", description="Set whether a role is displayed separately (authorized admin)", args="role:on_or_off:reason?")
    async def cmd_role_hoist(self, ctx, role_ref: str, hoist_text: str, *reason_parts: str) -> CmdResult:
        guild, bot_member, error = await self._ensure_admin_command(ctx, action="manage roles", required_permissions=["manage_roles"])
        if error:
            return error
        role, role_error = self._resolve_role(guild, role_ref)
        if role_error:
            return CmdResult(success=False, error=role_error)
        role_manage_error = self._can_bot_manage_role(guild, bot_member, role)
        if role_manage_error:
            return CmdResult(success=False, error=role_manage_error)
        hoist_value, toggle_error = self._parse_toggle_value(hoist_text, label="role hoist")
        if toggle_error:
            return CmdResult(success=False, error=toggle_error)
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Set role hoist", reason)
        try:
            await role.edit(hoist=hoist_value, reason=audit_reason)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the role hoist update.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Role hoist update failed: {exc}")
        return CmdResult(success=True, message=f"Set hoist for `@{role.name}` to {'on' if hoist_value else 'off'}.")

    @llm_command("role_mentionable", description="Set whether a role can be mentioned (authorized admin)", args="role:on_or_off:reason?")
    async def cmd_role_mentionable(self, ctx, role_ref: str, mentionable_text: str, *reason_parts: str) -> CmdResult:
        guild, bot_member, error = await self._ensure_admin_command(ctx, action="manage roles", required_permissions=["manage_roles"])
        if error:
            return error
        role, role_error = self._resolve_role(guild, role_ref)
        if role_error:
            return CmdResult(success=False, error=role_error)
        role_manage_error = self._can_bot_manage_role(guild, bot_member, role)
        if role_manage_error:
            return CmdResult(success=False, error=role_manage_error)
        mentionable_value, toggle_error = self._parse_toggle_value(mentionable_text, label="role mentionable")
        if toggle_error:
            return CmdResult(success=False, error=toggle_error)
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Set role mentionable", reason)
        try:
            await role.edit(mentionable=mentionable_value, reason=audit_reason)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the role mentionable update.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Role mentionable update failed: {exc}")
        return CmdResult(success=True, message=f"Set mentionable for `@{role.name}` to {'on' if mentionable_value else 'off'}.")

    @llm_command("channel_list", description="List server channels with optional page/filter (authorized admin)", args="page_or_filter?:filter?")
    async def cmd_channel_list(self, ctx, page_or_filter: str | None = None, *filter_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(
            ctx,
            action="review channels",
            required_permissions=["manage_channels"],
            bot_required_permissions=[],
        )
        if error:
            return error
        page, filter_text, parse_error = self._parse_page_or_filter_args(page_or_filter, filter_parts, subject="channel list")
        if parse_error:
            return CmdResult(success=False, error=parse_error)
        channels = sorted(
            list(getattr(guild, "channels", []) or []),
            key=lambda channel: (getattr(channel, "position", 0), str(getattr(channel, "name", "")).casefold(), getattr(channel, "id", 0)),
        )
        if not channels:
            return CmdResult(success=True, message="No channels found in this server.")
        filtered_channels = [channel for channel in channels if not filter_text or self._channel_matches_filter(channel, filter_text)]
        if not filtered_channels:
            return CmdResult(success=True, message=f"No channels matched `{filter_text}`.")
        page_items, total_pages = self._paginate_items(filtered_channels, page, page_size=REVIEW_LIST_PAGE_SIZE)
        if not page_items:
            return CmdResult(success=False, error=f"Channel list page `{page}` is out of range. There {'is' if total_pages == 1 else 'are'} {total_pages} page{'s' if total_pages != 1 else ''}.")
        lines = [self._format_channel_line(channel) for channel in page_items]
        return CmdResult(
            success=True,
            message="\n".join([
                f"Channels in {getattr(guild, 'name', 'this server')}{f' matching `{filter_text}`' if filter_text else ''} (page {page}/{total_pages}; {len(filtered_channels)} match{'es' if len(filtered_channels) != 1 else ''}):",
                *lines,
            ]),
        )

    @llm_command("channel_create_text", description="Create a text channel (authorized admin)", args="name:topic?")
    async def cmd_channel_create_text(self, ctx, name: str, *topic_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage channels", required_permissions=["manage_channels"])
        if error:
            return error
        normalized_name = name.strip()
        if not normalized_name:
            return CmdResult(success=False, error="Channel name can't be empty.")
        topic = ":".join(topic_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Create text channel", f"name={normalized_name}")
        kwargs = {"reason": audit_reason}
        if topic is not None:
            kwargs["topic"] = topic
        try:
            channel = await guild.create_text_channel(normalized_name, **kwargs)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the text channel creation.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Text channel creation failed: {exc}")
        return CmdResult(success=True, message=f"Created text channel `#{channel.name}` ({channel.id}).")

    @llm_command("channel_create_text_config", description="Create a text channel with keyed options (authorized admin)", args="name:option=value...")
    async def cmd_channel_create_text_config(self, ctx, name: str, *option_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage channels", required_permissions=["manage_channels"])
        if error:
            return error
        normalized_name = name.strip()
        if not normalized_name:
            return CmdResult(success=False, error="Channel name can't be empty.")
        options, options_error = self._parse_key_value_options(
            option_parts,
            allowed_keys={"topic", "category", "slowmode", "nsfw", "news"},
            subject="text channel create",
        )
        if options_error:
            return CmdResult(success=False, error=options_error)

        kwargs = {"reason": self._build_audit_reason(ctx, "Create text channel", f"name={normalized_name}")}
        if "topic" in options:
            kwargs["topic"] = options["topic"]
        if "category" in options:
            category, category_error = await self._resolve_category_channel(guild, options["category"])
            if category_error:
                return CmdResult(success=False, error=category_error)
            kwargs["category"] = category
        if "slowmode" in options:
            slowmode_value, slowmode_error = self._parse_optional_int(options["slowmode"], label="slowmode", minimum=0, maximum=21600)
            if slowmode_error:
                return CmdResult(success=False, error=slowmode_error)
            kwargs["slowmode_delay"] = slowmode_value
        if "nsfw" in options:
            nsfw_value, toggle_error = self._parse_toggle_value(options["nsfw"], label="nsfw")
            if toggle_error:
                return CmdResult(success=False, error=toggle_error)
            kwargs["nsfw"] = nsfw_value
        if "news" in options:
            news_value, toggle_error = self._parse_toggle_value(options["news"], label="news")
            if toggle_error:
                return CmdResult(success=False, error=toggle_error)
            kwargs["news"] = news_value
        try:
            channel = await guild.create_text_channel(normalized_name, **kwargs)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the text channel creation.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Text channel creation failed: {exc}")
        return CmdResult(success=True, message=f"Created text channel `#{channel.name}` ({channel.id}).")

    @llm_command("channel_create_voice", description="Create a voice channel (authorized admin)", args="name:user_limit?")
    async def cmd_channel_create_voice(self, ctx, name: str, user_limit_text: str | None = None) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage channels", required_permissions=["manage_channels"])
        if error:
            return error
        normalized_name = name.strip()
        if not normalized_name:
            return CmdResult(success=False, error="Channel name can't be empty.")
        kwargs = {"reason": self._build_audit_reason(ctx, "Create voice channel", f"name={normalized_name}")}
        if user_limit_text:
            if not user_limit_text.isdigit():
                return CmdResult(success=False, error="Voice channel user limits must be an integer from 0 to 99.")
            user_limit = int(user_limit_text)
            if not 0 <= user_limit <= 99:
                return CmdResult(success=False, error="Voice channel user limits must be between 0 and 99.")
            kwargs["user_limit"] = user_limit
        try:
            channel = await guild.create_voice_channel(normalized_name, **kwargs)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the voice channel creation.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Voice channel creation failed: {exc}")
        return CmdResult(success=True, message=f"Created voice channel `#{channel.name}` ({channel.id}).")

    @llm_command("channel_create_voice_config", description="Create a voice channel with keyed options (authorized admin)", args="name:option=value...")
    async def cmd_channel_create_voice_config(self, ctx, name: str, *option_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage channels", required_permissions=["manage_channels"])
        if error:
            return error
        normalized_name = name.strip()
        if not normalized_name:
            return CmdResult(success=False, error="Channel name can't be empty.")
        options, options_error = self._parse_key_value_options(
            option_parts,
            allowed_keys={"category", "bitrate", "user_limit", "nsfw"},
            subject="voice channel create",
        )
        if options_error:
            return CmdResult(success=False, error=options_error)

        kwargs = {"reason": self._build_audit_reason(ctx, "Create voice channel", f"name={normalized_name}")}
        if "category" in options:
            category, category_error = await self._resolve_category_channel(guild, options["category"])
            if category_error:
                return CmdResult(success=False, error=category_error)
            kwargs["category"] = category
        if "bitrate" in options:
            bitrate_value, bitrate_error = self._parse_optional_int(options["bitrate"], label="bitrate", minimum=8000, maximum=384000)
            if bitrate_error:
                return CmdResult(success=False, error=bitrate_error)
            kwargs["bitrate"] = bitrate_value
        if "user_limit" in options:
            user_limit_value, user_limit_error = self._parse_optional_int(options["user_limit"], label="user limit", minimum=0, maximum=99)
            if user_limit_error:
                return CmdResult(success=False, error=user_limit_error)
            kwargs["user_limit"] = user_limit_value
        if "nsfw" in options:
            nsfw_value, toggle_error = self._parse_toggle_value(options["nsfw"], label="nsfw")
            if toggle_error:
                return CmdResult(success=False, error=toggle_error)
            kwargs["nsfw"] = nsfw_value
        try:
            channel = await guild.create_voice_channel(normalized_name, **kwargs)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the voice channel creation.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Voice channel creation failed: {exc}")
        return CmdResult(success=True, message=f"Created voice channel `#{channel.name}` ({channel.id}).")

    @llm_command("channel_create_category", description="Create a category channel (authorized admin)", args="name")
    async def cmd_channel_create_category(self, ctx, name: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage channels", required_permissions=["manage_channels"])
        if error:
            return error
        normalized_name = name.strip()
        if not normalized_name:
            return CmdResult(success=False, error="Category name can't be empty.")
        audit_reason = self._build_audit_reason(ctx, "Create category channel", f"name={normalized_name}")
        try:
            channel = await guild.create_category(normalized_name, reason=audit_reason)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the category creation.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Category creation failed: {exc}")
        return CmdResult(success=True, message=f"Created category `{channel.name}` ({channel.id}).")

    @llm_command("channel_create_forum", description="Create a forum channel (authorized admin)", args="name:topic?")
    async def cmd_channel_create_forum(self, ctx, name: str, *topic_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage channels", required_permissions=["manage_channels"])
        if error:
            return error
        normalized_name = name.strip()
        if not normalized_name:
            return CmdResult(success=False, error="Forum channel name can't be empty.")
        topic = ":".join(topic_parts).strip() or None
        kwargs = {"reason": self._build_audit_reason(ctx, "Create forum channel", f"name={normalized_name}")}
        if topic is not None:
            kwargs["topic"] = topic
        try:
            channel = await guild.create_forum(normalized_name, **kwargs)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the forum channel creation.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Forum channel creation failed: {exc}")
        return CmdResult(success=True, message=f"Created forum channel `#{channel.name}` ({channel.id}).")

    @llm_command("channel_create_forum_config", description="Create a forum channel with keyed options (authorized admin)", args="name:option=value...")
    async def cmd_channel_create_forum_config(self, ctx, name: str, *option_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage channels", required_permissions=["manage_channels"])
        if error:
            return error
        normalized_name = name.strip()
        if not normalized_name:
            return CmdResult(success=False, error="Channel name can't be empty.")
        options, options_error = self._parse_key_value_options(
            option_parts,
            allowed_keys={"topic", "category", "slowmode", "nsfw", "media"},
            subject="forum channel create",
        )
        if options_error:
            return CmdResult(success=False, error=options_error)

        kwargs = {"reason": self._build_audit_reason(ctx, "Create forum channel", f"name={normalized_name}")}
        if "topic" in options:
            kwargs["topic"] = options["topic"]
        if "category" in options:
            category, category_error = await self._resolve_category_channel(guild, options["category"])
            if category_error:
                return CmdResult(success=False, error=category_error)
            kwargs["category"] = category
        if "slowmode" in options:
            slowmode_value, slowmode_error = self._parse_optional_int(options["slowmode"], label="slowmode", minimum=0, maximum=21600)
            if slowmode_error:
                return CmdResult(success=False, error=slowmode_error)
            kwargs["slowmode_delay"] = slowmode_value
        if "nsfw" in options:
            nsfw_value, toggle_error = self._parse_toggle_value(options["nsfw"], label="nsfw")
            if toggle_error:
                return CmdResult(success=False, error=toggle_error)
            kwargs["nsfw"] = nsfw_value
        if "media" in options:
            media_value, toggle_error = self._parse_toggle_value(options["media"], label="media")
            if toggle_error:
                return CmdResult(success=False, error=toggle_error)
            kwargs["media"] = media_value
        try:
            channel = await guild.create_forum(normalized_name, **kwargs)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the forum channel creation.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Forum channel creation failed: {exc}")
        return CmdResult(success=True, message=f"Created forum channel `#{channel.name}` ({channel.id}).")

    async def _cmd_channel_post_impl(self, ctx, channel_ref: str, *message_parts: str) -> CmdResult:
        guild = getattr(ctx, "guild", None)
        if guild is not None:
            guild, bot_member, error = await self._ensure_admin_command(
                ctx,
                action="post to channels",
                required_permissions=["manage_channels"],
            )
            if error:
                return error
            channel, channel_error = self._resolve_channel(guild, channel_ref)
            if channel_error:
                return CmdResult(success=False, error=channel_error)
        else:
            if not await self._ctx_is_keeper(ctx):
                return CmdResult(success=False, error="Only The Keeper can post to channels from outside a server context.")
            channel, guild, channel_error = await self._resolve_global_channel(channel_ref)
            if channel_error:
                return CmdResult(success=False, error=channel_error)
            bot_member = self._get_bot_member(guild)
            if bot_member is None:
                return CmdResult(success=False, error="Can't resolve my server member record.")
            missing = self._missing_permissions(bot_member, ["manage_channels"])
            if missing:
                return CmdResult(
                    success=False,
                    error=f"I need `{self._format_permission_names(missing)}` to post to channels.",
                )

        permission_name, summary_label = self._channel_post_permission(channel)
        if permission_name is None:
            return CmdResult(success=False, error="That channel type doesn't accept direct messages through this admin surface.")

        content = ":".join(message_parts).strip()
        if not content:
            return CmdResult(success=False, error="Message content can't be empty.")

        channel_permissions = getattr(channel, "permissions_for", None)
        if not callable(channel_permissions):
            return CmdResult(success=False, error="Can't inspect my permissions for that channel.")

        bot_permissions = channel_permissions(bot_member)
        if not getattr(bot_permissions, "view_channel", False):
            return CmdResult(success=False, error=f"I can't view `#{channel.name}`.")
        if not getattr(bot_permissions, permission_name, False):
            permission_label = permission_name.replace("_", " ")
            return CmdResult(success=False, error=f"I need `{permission_label}` in `#{channel.name}` to post {summary_label}.")

        channel_send = getattr(channel, "send", None)
        if not callable(channel_send):
            return CmdResult(success=False, error="That channel doesn't expose a send operation.")
        try:
            await channel_send(content=content)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the cross-channel post.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Cross-channel post failed: {exc}")
        return CmdResult(success=True, message=f"Posted to `#{channel.name}`.")

    @llm_command("channel_post", description="Post a message into another text channel or thread (authorized admin)", args="channel:message")
    async def cmd_channel_post(self, ctx, channel_ref: str, *message_parts: str) -> CmdResult:
        return await self._cmd_channel_post_impl(ctx, channel_ref, *message_parts)

    @llm_command("channel_send", description="Send a message into another text channel or thread (authorized admin)", args="channel:message")
    async def cmd_channel_send(self, ctx, channel_ref: str, *message_parts: str) -> CmdResult:
        return await self._cmd_channel_post_impl(ctx, channel_ref, *message_parts)

    @llm_command("channel_rename", description="Rename a channel (authorized admin)", args="channel:new_name:reason?")
    async def cmd_channel_rename(self, ctx, channel_ref: str, new_name: str, *reason_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage channels", required_permissions=["manage_channels"])
        if error:
            return error
        channel, channel_error = self._resolve_channel(guild, channel_ref)
        if channel_error:
            return CmdResult(success=False, error=channel_error)
        normalized_name = new_name.strip()
        if not normalized_name:
            return CmdResult(success=False, error="Channel name can't be empty.")
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Rename channel", reason)
        try:
            await channel.edit(name=normalized_name, reason=audit_reason)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the channel rename.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Channel rename failed: {exc}")
        return CmdResult(success=True, message=f"Renamed channel to `#{normalized_name}`.")

    @llm_command("channel_topic", description="Set or clear a text-channel topic (authorized admin)", args="channel:topic:reason?")
    async def cmd_channel_topic(self, ctx, channel_ref: str, topic: str, *reason_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage channels", required_permissions=["manage_channels"])
        if error:
            return error
        channel, channel_error = self._resolve_channel(guild, channel_ref)
        if channel_error:
            return CmdResult(success=False, error=channel_error)
        if not hasattr(channel, "topic"):
            return CmdResult(success=False, error="That channel type doesn't have a topic.")
        topic_value = None if topic.strip().casefold() in {"clear", "none", "remove", "-"} else topic.strip()
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Edit channel topic", reason)
        try:
            await channel.edit(topic=topic_value, reason=audit_reason)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the topic update.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Topic update failed: {exc}")
        if topic_value is None:
            return CmdResult(success=True, message=f"Cleared the topic for `#{channel.name}`.")
        return CmdResult(success=True, message=f"Updated the topic for `#{channel.name}`.")

    @llm_command("channel_slowmode", description="Set channel slowmode in seconds (authorized admin)", args="channel:seconds:reason?")
    async def cmd_channel_slowmode(self, ctx, channel_ref: str, seconds_text: str, *reason_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage channels", required_permissions=["manage_channels"])
        if error:
            return error
        channel, channel_error = self._resolve_channel(guild, channel_ref)
        if channel_error:
            return CmdResult(success=False, error=channel_error)
        if not hasattr(channel, "slowmode_delay"):
            return CmdResult(success=False, error="That channel type doesn't support slowmode.")
        normalized = seconds_text.strip().casefold()
        if normalized in {"off", "disable", "disabled", "none"}:
            seconds = 0
        elif normalized.isdigit():
            seconds = int(normalized)
        else:
            return CmdResult(success=False, error="Slowmode must be an integer number of seconds or `off`.")
        if not 0 <= seconds <= 21600:
            return CmdResult(success=False, error="Slowmode must be between 0 and 21600 seconds.")
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Edit channel slowmode", reason)
        try:
            await channel.edit(slowmode_delay=seconds, reason=audit_reason)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the slowmode update.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Slowmode update failed: {exc}")
        if seconds == 0:
            return CmdResult(success=True, message=f"Disabled slowmode for `#{channel.name}`.")
        return CmdResult(success=True, message=f"Set slowmode for `#{channel.name}` to {seconds}s.")

    @llm_command("channel_nsfw", description="Set whether a channel is NSFW (authorized admin)", args="channel:on_or_off:reason?")
    async def cmd_channel_nsfw(self, ctx, channel_ref: str, nsfw_text: str, *reason_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage channels", required_permissions=["manage_channels"])
        if error:
            return error
        channel, channel_error = self._resolve_channel(guild, channel_ref)
        if channel_error:
            return CmdResult(success=False, error=channel_error)
        if not hasattr(channel, "nsfw"):
            return CmdResult(success=False, error="That channel type doesn't support NSFW mode.")
        nsfw_value, toggle_error = self._parse_toggle_value(nsfw_text, label="nsfw")
        if toggle_error:
            return CmdResult(success=False, error=toggle_error)
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Set channel NSFW", reason)
        try:
            await channel.edit(nsfw=nsfw_value, reason=audit_reason)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the NSFW update.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"NSFW update failed: {exc}")
        return CmdResult(success=True, message=f"Set NSFW for `#{channel.name}` to {'on' if nsfw_value else 'off'}.")

    @llm_command("channel_user_limit", description="Set a voice-channel user limit (authorized admin)", args="channel:limit:reason?")
    async def cmd_channel_user_limit(self, ctx, channel_ref: str, limit_text: str, *reason_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage channels", required_permissions=["manage_channels"])
        if error:
            return error
        channel, channel_error = await self._resolve_voice_channel(guild, channel_ref)
        if channel_error:
            return CmdResult(success=False, error=channel_error)
        if not hasattr(channel, "user_limit"):
            return CmdResult(success=False, error="That channel type doesn't support user limits.")
        normalized = limit_text.strip().casefold()
        if normalized in {"off", "none", "disable", "clear"}:
            limit = 0
        elif normalized.isdigit():
            limit = int(normalized)
        else:
            return CmdResult(success=False, error="Voice channel user limit must be an integer from 0 to 99, or `off`.")
        if not 0 <= limit <= 99:
            return CmdResult(success=False, error="Voice channel user limit must be between 0 and 99.")
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Set voice channel user limit", reason)
        try:
            await channel.edit(user_limit=limit, reason=audit_reason)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the voice channel user limit update.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Voice channel user limit update failed: {exc}")
        return CmdResult(success=True, message=f"Set user limit for `#{channel.name}` to {limit}.")

    @llm_command("channel_permissions", description="Set explicit channel permission overwrites for a role or member (authorized admin)", args="channel:target:permission=value...")
    async def cmd_channel_permissions(self, ctx, channel_ref: str, target_ref: str, *option_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage channels", required_permissions=["manage_channels"])
        if error:
            return error
        channel, channel_error = self._resolve_channel(guild, channel_ref)
        if channel_error:
            return CmdResult(success=False, error=channel_error)
        target, target_error = await self._resolve_permission_target(guild, target_ref)
        if target_error:
            return CmdResult(success=False, error=target_error)
        overwrites, reason, parse_error = self._parse_permission_overwrite_options(option_parts, subject="channel permissions")
        if parse_error:
            return CmdResult(success=False, error=parse_error)
        audit_reason = self._build_audit_reason(ctx, "Set channel permissions", reason)
        try:
            await channel.set_permissions(target, reason=audit_reason, **overwrites)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the channel permission overwrite update.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Channel permission overwrite update failed: {exc}")
        target_name = getattr(target, "display_name", None) or getattr(target, "name", None) or str(target)
        changed = ", ".join(f"{key}={'clear' if value is None else ('allow' if value else 'deny')}" for key, value in sorted(overwrites.items()))
        return CmdResult(success=True, message=f"Updated channel overwrites for `{target_name}` in `#{channel.name}`: {changed}.")

    @llm_command("channel_lock", description="Lock a channel against public posting or connecting (authorized admin)", args="channel:reason?")
    async def cmd_channel_lock(self, ctx, channel_ref: str, *reason_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage channels", required_permissions=["manage_channels"])
        if error:
            return error
        channel, channel_error = self._resolve_channel(guild, channel_ref)
        if channel_error:
            return CmdResult(success=False, error=channel_error)
        permission_name, summary_label = self._channel_lock_permission(channel)
        if permission_name is None:
            return CmdResult(success=False, error="That channel type doesn't support lock/unlock through this admin surface.")
        default_role = getattr(guild, "default_role", None)
        if default_role is None:
            return CmdResult(success=False, error="Can't resolve the server's default role for lock permissions.")
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Lock channel", reason)

        async def execute() -> CmdResult:
            try:
                await channel.set_permissions(default_role, reason=audit_reason, **{permission_name: False})
            except discord.Forbidden:
                return CmdResult(success=False, error="Discord denied the channel lock.")
            except discord.HTTPException as exc:
                return CmdResult(success=False, error=f"Channel lock failed: {exc}")
            return CmdResult(success=True, message=f"Locked `#{channel.name}` for public {summary_label}.")

        return await self._request_destructive_confirmation(ctx, summary=f"lock channel #{channel.name}", executor=execute)

    @llm_command("channel_unlock", description="Unlock a previously locked channel (authorized admin)", args="channel:reason?")
    async def cmd_channel_unlock(self, ctx, channel_ref: str, *reason_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage channels", required_permissions=["manage_channels"])
        if error:
            return error
        channel, channel_error = self._resolve_channel(guild, channel_ref)
        if channel_error:
            return CmdResult(success=False, error=channel_error)
        permission_name, summary_label = self._channel_lock_permission(channel)
        if permission_name is None:
            return CmdResult(success=False, error="That channel type doesn't support lock/unlock through this admin surface.")
        default_role = getattr(guild, "default_role", None)
        if default_role is None:
            return CmdResult(success=False, error="Can't resolve the server's default role for lock permissions.")
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Unlock channel", reason)
        try:
            await channel.set_permissions(default_role, reason=audit_reason, **{permission_name: None})
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the channel unlock.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Channel unlock failed: {exc}")
        return CmdResult(success=True, message=f"Unlocked `#{channel.name}` for public {summary_label}.")

    @llm_command("channel_delete", description="Delete a server channel (authorized admin)", args="channel:reason?")
    async def cmd_channel_delete(self, ctx, channel_ref: str, *reason_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage channels", required_permissions=["manage_channels"])
        if error:
            return error
        channel, channel_error = self._resolve_channel(guild, channel_ref)
        if channel_error:
            return CmdResult(success=False, error=channel_error)
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Delete channel", reason)

        async def execute() -> CmdResult:
            try:
                await channel.delete(reason=audit_reason)
            except discord.Forbidden:
                return CmdResult(success=False, error="Discord denied the channel delete.")
            except discord.HTTPException as exc:
                return CmdResult(success=False, error=f"Channel delete failed: {exc}")
            return CmdResult(success=True, message=f"Deleted channel `#{channel.name}`.")

        return await self._request_destructive_confirmation(ctx, summary=f"delete channel #{channel.name}", executor=execute)

    @llm_command("timeout_list", description="List currently timed out members with optional page/filter (authorized admin)", args="page_or_filter?:filter?")
    async def cmd_timeout_list(self, ctx, page_or_filter: str | None = None, *filter_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(
            ctx,
            action="review active timeouts",
            required_permissions=["moderate_members"],
            bot_required_permissions=[],
        )
        if error:
            return error
        page, filter_text, parse_error = self._parse_page_or_filter_args(page_or_filter, filter_parts, subject="timeout list")
        if parse_error:
            return CmdResult(success=False, error=parse_error)
        now = discord.utils.utcnow()
        members = [
            member for member in list(getattr(guild, "members", []) or [])
            if getattr(member, "timed_out_until", None) is not None and getattr(member, "timed_out_until") > now
        ]
        members.sort(key=lambda member: getattr(member, "timed_out_until"))
        if not members:
            return CmdResult(success=True, message="No members are currently timed out.")
        filtered_members = [member for member in members if not filter_text or self._member_matches_filter(member, filter_text)]
        if not filtered_members:
            return CmdResult(success=True, message=f"No timed out members matched `{filter_text}`.")
        page_items, total_pages = self._paginate_items(filtered_members, page, page_size=REVIEW_LIST_PAGE_SIZE)
        if not page_items:
            return CmdResult(success=False, error=f"Timeout list page `{page}` is out of range. There {'is' if total_pages == 1 else 'are'} {total_pages} page{'s' if total_pages != 1 else ''}.")
        lines = []
        for member in page_items:
            until_text = self._format_datetime(getattr(member, "timed_out_until", None)) or "unknown"
            lines.append(f"- {member.display_name} `{member.id}` (until {until_text})")
        return CmdResult(
            success=True,
            message="\n".join([
                f"Active timeouts in {getattr(guild, 'name', 'this server')}{f' matching `{filter_text}`' if filter_text else ''} (page {page}/{total_pages}; {len(filtered_members)} match{'es' if len(filtered_members) != 1 else ''}):",
                *lines,
            ]),
        )

    @llm_command("recent_joins", description="List the most recent member joins (authorized admin)", args="count?")
    async def cmd_recent_joins(self, ctx, count_text: str | None = None) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(
            ctx,
            action="review recent joins",
            required_permissions=["kick_members"],
            bot_required_permissions=[],
        )
        if error:
            return error
        count = 10
        if count_text:
            if not count_text.isdigit():
                return CmdResult(success=False, error="Recent join count must be an integer from 1 to 50.")
            count = int(count_text)
            if not 1 <= count <= 50:
                return CmdResult(success=False, error="Recent join count must be between 1 and 50.")
        members = [member for member in list(getattr(guild, "members", []) or []) if getattr(member, "joined_at", None) is not None]
        members.sort(key=lambda member: getattr(member, "joined_at"), reverse=True)
        members = members[:count]
        if not members:
            return CmdResult(success=True, message="No member join timestamps are available in this server.")
        lines = []
        for member in members:
            joined_text = self._format_datetime(getattr(member, "joined_at", None)) or "unknown"
            lines.append(f"- {member.display_name} `{member.id}` (joined {joined_text})")
        return CmdResult(
            success=True,
            message=self._build_listing_message(
                f"Recent joins in {getattr(guild, 'name', 'this server')} ({len(members)} shown):",
                lines,
            ),
        )

    @llm_command("prune_estimate", description="Estimate how many inactive members would be pruned (authorized admin)", args="days")
    async def cmd_prune_estimate(self, ctx, days_text: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(
            ctx,
            action="estimate pruned members",
            required_permissions=["kick_members", "manage_guild"],
        )
        if error:
            return error
        if not days_text.isdigit():
            return CmdResult(success=False, error="Prune days must be an integer from 1 to 30.")
        days = int(days_text)
        if not 1 <= days <= 30:
            return CmdResult(success=False, error="Prune days must be between 1 and 30.")
        try:
            count = await guild.estimate_pruned_members(days=days)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the prune estimate.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Prune estimate failed: {exc}")
        return CmdResult(success=True, message=f"Discord estimates {count if count is not None else 'an unknown number of'} members would be pruned after {days} inactive days.")

    @llm_command("prune_members", description="Prune inactive members from the server (authorized admin)", args="days:reason?")
    async def cmd_prune_members(self, ctx, days_text: str, *reason_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(
            ctx,
            action="prune inactive members",
            required_permissions=["kick_members", "manage_guild"],
        )
        if error:
            return error
        if not days_text.isdigit():
            return CmdResult(success=False, error="Prune days must be an integer from 1 to 30.")
        days = int(days_text)
        if not 1 <= days <= 30:
            return CmdResult(success=False, error="Prune days must be between 1 and 30.")
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Prune members", reason)

        async def execute() -> CmdResult:
            try:
                count = await guild.prune_members(days=days, reason=audit_reason)
            except discord.Forbidden:
                return CmdResult(success=False, error="Discord denied the prune.")
            except discord.HTTPException as exc:
                return CmdResult(success=False, error=f"Prune failed: {exc}")
            if count is None:
                return CmdResult(success=True, message=f"Started prune for members inactive {days} day(s).")
            return CmdResult(success=True, message=f"Pruned {count} member(s) inactive for {days} day(s).")

        return await self._request_destructive_confirmation(ctx, summary=f"prune members inactive for {days} day(s)", executor=execute)

    @llm_command("webhook_list", description="List server webhooks with optional page/filter (authorized admin)", args="page_or_filter?:filter?")
    async def cmd_webhook_list(self, ctx, page_or_filter: str | None = None, *filter_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="review webhooks", required_permissions=["manage_webhooks"])
        if error:
            return error
        page, filter_text, parse_error = self._parse_page_or_filter_args(page_or_filter, filter_parts, subject="webhook list")
        if parse_error:
            return CmdResult(success=False, error=parse_error)
        try:
            webhooks = await guild.webhooks()
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the webhook list.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Webhook list failed: {exc}")
        if not webhooks:
            return CmdResult(success=True, message="No webhooks found in this server.")
        filtered_webhooks = [webhook for webhook in webhooks if not filter_text or self._webhook_matches_filter(webhook, filter_text)]
        if not filtered_webhooks:
            return CmdResult(success=True, message=f"No webhooks matched `{filter_text}`.")
        page_items, total_pages = self._paginate_items(filtered_webhooks, page, page_size=REVIEW_LIST_PAGE_SIZE)
        if not page_items:
            return CmdResult(success=False, error=f"Webhook list page `{page}` is out of range. There {'is' if total_pages == 1 else 'are'} {total_pages} page{'s' if total_pages != 1 else ''}.")
        lines = [self._format_webhook_line(webhook) for webhook in page_items]
        return CmdResult(success=True, message="\n".join([
            f"Webhooks in {getattr(guild, 'name', 'this server')}{f' matching `{filter_text}`' if filter_text else ''} (page {page}/{total_pages}; {len(filtered_webhooks)} match{'es' if len(filtered_webhooks) != 1 else ''}):",
            *lines,
        ]))

    @llm_command("webhook_create", description="Create a webhook in a channel, optionally with avatar attachment (authorized admin)", args="channel:name:avatar_attachment?")
    async def cmd_webhook_create(self, ctx, channel_ref: str, name: str, avatar_selector: str | None = None) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage webhooks", required_permissions=["manage_webhooks"])
        if error:
            return error
        channel, channel_error = self._resolve_channel(guild, channel_ref)
        if channel_error:
            return CmdResult(success=False, error=channel_error)
        create_webhook = getattr(channel, "create_webhook", None)
        if not callable(create_webhook):
            return CmdResult(success=False, error="That channel type doesn't support webhooks.")
        normalized_name = name.strip()
        if not normalized_name:
            return CmdResult(success=False, error="Webhook name can't be empty.")
        avatar_bytes = None
        if avatar_selector is not None:
            attachment, attachment_error = await self._select_emoji_source_attachment(ctx, avatar_selector)
            if attachment_error:
                return CmdResult(success=False, error=attachment_error)
            try:
                avatar_bytes = await attachment.read()
            except discord.HTTPException as exc:
                return CmdResult(success=False, error=f"Couldn't read the webhook avatar attachment: {exc}")
        audit_reason = self._build_audit_reason(ctx, "Create webhook", f"name={normalized_name}")
        try:
            webhook = await create_webhook(name=normalized_name, avatar=avatar_bytes, reason=audit_reason)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the webhook creation.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Webhook creation failed: {exc}")
        return CmdResult(success=True, message=f"Created webhook `{webhook.name}` ({webhook.id}) in `#{channel.name}`.")

    @llm_command("webhook_rename", description="Rename a webhook (authorized admin)", args="webhook:new_name:reason?")
    async def cmd_webhook_rename(self, ctx, webhook_ref: str, new_name: str, *reason_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage webhooks", required_permissions=["manage_webhooks"])
        if error:
            return error
        webhook, webhook_error = await self._resolve_webhook(guild, webhook_ref)
        if webhook_error:
            return CmdResult(success=False, error=webhook_error)
        normalized_name = new_name.strip()
        if not normalized_name:
            return CmdResult(success=False, error="Webhook name can't be empty.")
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Rename webhook", reason)
        try:
            await webhook.edit(name=normalized_name, reason=audit_reason)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the webhook rename.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Webhook rename failed: {exc}")
        return CmdResult(success=True, message=f"Renamed webhook to `{normalized_name}`.")

    @llm_command("webhook_move", description="Move a webhook to another channel (authorized admin)", args="webhook:channel:reason?")
    async def cmd_webhook_move(self, ctx, webhook_ref: str, channel_ref: str, *reason_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage webhooks", required_permissions=["manage_webhooks"])
        if error:
            return error
        webhook, webhook_error = await self._resolve_webhook(guild, webhook_ref)
        if webhook_error:
            return CmdResult(success=False, error=webhook_error)
        channel, channel_error = self._resolve_channel(guild, channel_ref)
        if channel_error:
            return CmdResult(success=False, error=channel_error)
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Move webhook", reason)
        try:
            await webhook.edit(channel=channel, reason=audit_reason)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the webhook move.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Webhook move failed: {exc}")
        return CmdResult(success=True, message=f"Moved webhook `{webhook.name}` to `#{channel.name}`.")

    @llm_command("webhook_delete", description="Delete a webhook (authorized admin)", args="webhook:reason?")
    async def cmd_webhook_delete(self, ctx, webhook_ref: str, *reason_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage webhooks", required_permissions=["manage_webhooks"])
        if error:
            return error
        webhook, webhook_error = await self._resolve_webhook(guild, webhook_ref)
        if webhook_error:
            return CmdResult(success=False, error=webhook_error)
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Delete webhook", reason)

        async def execute() -> CmdResult:
            try:
                await webhook.delete(reason=audit_reason)
            except discord.Forbidden:
                return CmdResult(success=False, error="Discord denied the webhook delete.")
            except discord.HTTPException as exc:
                return CmdResult(success=False, error=f"Webhook delete failed: {exc}")
            return CmdResult(success=True, message=f"Deleted webhook `{webhook.name}`.")

        return await self._request_destructive_confirmation(ctx, summary=f"delete webhook {getattr(webhook, 'name', 'unknown')}", executor=execute)

    @llm_command("sticker_list", description="List server stickers with optional page/filter (authorized admin)", args="page_or_filter?:filter?")
    async def cmd_sticker_list(self, ctx, page_or_filter: str | None = None, *filter_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="review stickers", required_permissions=["manage_emojis_and_stickers"])
        if error:
            return error
        page, filter_text, parse_error = self._parse_page_or_filter_args(page_or_filter, filter_parts, subject="sticker list")
        if parse_error:
            return CmdResult(success=False, error=parse_error)
        try:
            stickers = await guild.fetch_stickers()
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the sticker list.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Sticker list failed: {exc}")
        if not stickers:
            return CmdResult(success=True, message="No stickers found in this server.")
        filtered_stickers = [sticker for sticker in stickers if not filter_text or self._sticker_matches_filter(sticker, filter_text)]
        if not filtered_stickers:
            return CmdResult(success=True, message=f"No stickers matched `{filter_text}`.")
        page_items, total_pages = self._paginate_items(filtered_stickers, page, page_size=REVIEW_LIST_PAGE_SIZE)
        if not page_items:
            return CmdResult(success=False, error=f"Sticker list page `{page}` is out of range. There {'is' if total_pages == 1 else 'are'} {total_pages} page{'s' if total_pages != 1 else ''}.")
        lines = [self._format_sticker_line(sticker) for sticker in page_items]
        return CmdResult(success=True, message="\n".join([
            f"Stickers in {getattr(guild, 'name', 'this server')}{f' matching `{filter_text}`' if filter_text else ''} (page {page}/{total_pages}; {len(filtered_stickers)} match{'es' if len(filtered_stickers) != 1 else ''}):",
            *lines,
        ]))

    @llm_command("sticker_inspect", description="Inspect one server sticker (authorized admin)", args="sticker")
    async def cmd_sticker_inspect(self, ctx, sticker_ref: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="inspect stickers", required_permissions=["manage_emojis_and_stickers"])
        if error:
            return error
        sticker, sticker_error = await self._resolve_sticker(guild, sticker_ref)
        if sticker_error:
            return CmdResult(success=False, error=sticker_error)
        created = self._format_datetime(getattr(sticker, "created_at", None)) or "unknown"
        user = getattr(sticker, "user", None)
        creator = getattr(user, "display_name", None) or getattr(user, "name", None) or ("unknown" if user is None else str(user))
        return CmdResult(
            success=True,
            message=(
                f"Sticker `{getattr(sticker, 'name', 'unknown')}` `{getattr(sticker, 'id', 'unknown')}`\n"
                f"- description: {getattr(sticker, 'description', None) or 'none'}\n"
                f"- emoji: {getattr(sticker, 'emoji', None) or 'unknown'}\n"
                f"- created: {created}\n"
                f"- added by: {creator}"
            ),
        )

    @llm_command("sticker_create", description="Create a sticker from an attached file (authorized admin)", args="name:emoji:description:attachment?")
    async def cmd_sticker_create(self, ctx, name: str, emoji: str, description: str, attachment_selector: str | None = None) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage stickers", required_permissions=["manage_emojis_and_stickers"])
        if error:
            return error
        normalized_name = name.strip()
        if len(normalized_name) < 2:
            return CmdResult(success=False, error="Sticker names must be at least 2 characters.")
        attachment, attachment_error = await self._select_sticker_source_attachment(ctx, attachment_selector)
        if attachment_error:
            return CmdResult(success=False, error=attachment_error)
        try:
            sticker_bytes = await attachment.read()
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Couldn't read the sticker attachment: {exc}")
        sticker_file = discord.File(BytesIO(sticker_bytes), filename=getattr(attachment, "filename", "sticker.png"))
        audit_reason = self._build_audit_reason(ctx, "Create sticker", f"name={normalized_name}")
        try:
            sticker = await guild.create_sticker(name=normalized_name, description=description, emoji=emoji, file=sticker_file, reason=audit_reason)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the sticker creation.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Sticker creation failed: {exc}")
        return CmdResult(success=True, message=f"Created sticker `{sticker.name}` ({sticker.id}).")

    @llm_command("sticker_rename", description="Rename a sticker (authorized admin)", args="sticker:new_name:reason?")
    async def cmd_sticker_rename(self, ctx, sticker_ref: str, new_name: str, *reason_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage stickers", required_permissions=["manage_emojis_and_stickers"])
        if error:
            return error
        sticker, sticker_error = await self._resolve_sticker(guild, sticker_ref)
        if sticker_error:
            return CmdResult(success=False, error=sticker_error)
        normalized_name = new_name.strip()
        if len(normalized_name) < 2:
            return CmdResult(success=False, error="Sticker names must be at least 2 characters.")
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Rename sticker", reason)
        try:
            await sticker.edit(name=normalized_name, reason=audit_reason)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the sticker rename.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Sticker rename failed: {exc}")
        return CmdResult(success=True, message=f"Renamed sticker to `{normalized_name}`.")

    @llm_command("sticker_delete", description="Delete a sticker (authorized admin)", args="sticker:reason?")
    async def cmd_sticker_delete(self, ctx, sticker_ref: str, *reason_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage stickers", required_permissions=["manage_emojis_and_stickers"])
        if error:
            return error
        sticker, sticker_error = await self._resolve_sticker(guild, sticker_ref)
        if sticker_error:
            return CmdResult(success=False, error=sticker_error)
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Delete sticker", reason)

        async def execute() -> CmdResult:
            try:
                await sticker.delete(reason=audit_reason)
            except discord.Forbidden:
                return CmdResult(success=False, error="Discord denied the sticker delete.")
            except discord.HTTPException as exc:
                return CmdResult(success=False, error=f"Sticker delete failed: {exc}")
            return CmdResult(success=True, message=f"Deleted sticker `{sticker.name}`.")

        return await self._request_destructive_confirmation(ctx, summary=f"delete sticker {getattr(sticker, 'name', 'unknown')}", executor=execute)

    @llm_command("event_list", description="List scheduled events with optional page/filter (authorized admin)", args="page_or_filter?:filter?")
    async def cmd_event_list(self, ctx, page_or_filter: str | None = None, *filter_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="review scheduled events", required_permissions=["manage_events"])
        if error:
            return error
        page, filter_text, parse_error = self._parse_page_or_filter_args(page_or_filter, filter_parts, subject="event list")
        if parse_error:
            return CmdResult(success=False, error=parse_error)
        try:
            events = await guild.fetch_scheduled_events(with_counts=True)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the scheduled event list.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Scheduled event list failed: {exc}")
        if not events:
            return CmdResult(success=True, message="No scheduled events found in this server.")
        filtered_events = [event for event in events if not filter_text or self._event_matches_filter(event, filter_text)]
        if not filtered_events:
            return CmdResult(success=True, message=f"No scheduled events matched `{filter_text}`.")
        page_items, total_pages = self._paginate_items(filtered_events, page, page_size=REVIEW_LIST_PAGE_SIZE)
        if not page_items:
            return CmdResult(success=False, error=f"Event list page `{page}` is out of range. There {'is' if total_pages == 1 else 'are'} {total_pages} page{'s' if total_pages != 1 else ''}.")
        lines = [self._format_event_line(event) for event in page_items]
        any_recurrence_exposed = any(getattr(e, "recurrence_rule", None) is not None for e in filtered_events)
        footer_parts = []
        if not any_recurrence_exposed:
            footer_parts.append("[Unverified] Recurring events may appear as a single upcoming occurrence; discord.py does not expose recurrence metadata.")
        footer = "\n".join(footer_parts)
        return CmdResult(success=True, message="\n".join(filter(None, [
            f"Scheduled events in {getattr(guild, 'name', 'this server')}{f' matching `{filter_text}`' if filter_text else ''} (page {page}/{total_pages}; {len(filtered_events)} match{'es' if len(filtered_events) != 1 else ''}):",
            *lines,
            footer,
        ])))

    @llm_command("event_inspect", description="Inspect one scheduled event (authorized admin)", args="event")
    async def cmd_event_inspect(self, ctx, event_ref: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="inspect scheduled events", required_permissions=["manage_events"])
        if error:
            return error
        event, event_error = await self._resolve_scheduled_event(guild, event_ref)
        if event_error:
            return CmdResult(success=False, error=event_error)
        start_text = self._format_datetime(getattr(event, "start_time", None)) or "unknown"
        end_text = self._format_datetime(getattr(event, "end_time", None)) or "none"
        channel = getattr(event, "channel", None)
        location = getattr(event, "location", None) or getattr(channel, "name", None) or "none"
        status = getattr(getattr(event, "status", None), "name", None) or str(getattr(event, "status", "unknown"))
        entity_type = getattr(getattr(event, "entity_type", None), "name", None) or str(getattr(event, "entity_type", "unknown"))
        recurrence_rule = getattr(event, "recurrence_rule", None)
        if recurrence_rule is not None:
            recurrence_text = str(recurrence_rule)
        else:
            recurrence_text = "not available [Unverified] (discord.py may not expose recurrence metadata; a recurring series may appear as a single upcoming event)"
        return CmdResult(
            success=True,
            message=(
                f"Scheduled event `{event.name}` `{event.id}`\n"
                f"- status: {status}\n"
                f"- type: {entity_type}\n"
                f"- start: {start_text}\n"
                f"- end: {end_text}\n"
                f"- location: {location}\n"
                f"- description: {getattr(event, 'description', None) or 'none'}\n"
                f"- recurrence: {recurrence_text}"
            ),
        )

    @llm_command("event_create_external", description="Create an external scheduled event (authorized admin)", args="name:start_iso:end_iso:location:description?")
    async def cmd_event_create_external(self, ctx, name: str, start_iso: str, end_iso: str, location: str, *description_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage scheduled events", required_permissions=["manage_events"])
        if error:
            return error
        start_time, start_error = self._parse_iso_datetime(start_iso)
        if start_error:
            return CmdResult(success=False, error=start_error)
        end_time, end_error = self._parse_iso_datetime(end_iso)
        if end_error:
            return CmdResult(success=False, error=end_error)
        if end_time <= start_time:
            return CmdResult(success=False, error="Scheduled event end time must be after the start time.")
        description = ":".join(description_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Create external scheduled event", f"name={name.strip()}")
        kwargs = {
            "name": name.strip(),
            "start_time": start_time,
            "end_time": end_time,
            "entity_type": discord.EntityType.external,
            "privacy_level": discord.PrivacyLevel.guild_only,
            "location": location.strip(),
            "reason": audit_reason,
        }
        if description:
            kwargs["description"] = description
        try:
            event = await guild.create_scheduled_event(**kwargs)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the scheduled event creation.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Scheduled event creation failed: {exc}")
        return CmdResult(success=True, message=f"Created external scheduled event `{event.name}` ({event.id}).")

    @llm_command("event_create_external_config", description="Create an external scheduled event with keyed options (authorized admin)", args="name:start_iso:end_iso:location:option=value...")
    async def cmd_event_create_external_config(self, ctx, name: str, start_iso: str, end_iso: str, location: str, *option_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage scheduled events", required_permissions=["manage_events"])
        if error:
            return error
        start_time, start_error = self._parse_iso_datetime(start_iso)
        if start_error:
            return CmdResult(success=False, error=start_error)
        end_time, end_error = self._parse_iso_datetime(end_iso)
        if end_error:
            return CmdResult(success=False, error=end_error)
        if end_time <= start_time:
            return CmdResult(success=False, error="Scheduled event end time must be after the start time.")
        options, options_error = self._parse_key_value_options(
            option_parts,
            allowed_keys={"description", "image"},
            subject="external event create",
        )
        if options_error:
            return CmdResult(success=False, error=options_error)
        audit_reason = self._build_audit_reason(ctx, "Create external scheduled event", f"name={name.strip()}")
        kwargs = {
            "name": name.strip(),
            "start_time": start_time,
            "end_time": end_time,
            "entity_type": discord.EntityType.external,
            "privacy_level": discord.PrivacyLevel.guild_only,
            "location": location.strip(),
            "reason": audit_reason,
        }
        if "description" in options:
            kwargs["description"] = options["description"]
        if "image" in options:
            attachment, attachment_error = await self._select_emoji_source_attachment(ctx, options["image"])
            if attachment_error:
                return CmdResult(success=False, error=attachment_error)
            try:
                kwargs["image"] = await attachment.read()
            except discord.HTTPException as exc:
                return CmdResult(success=False, error=f"Couldn't read the event image attachment: {exc}")
        try:
            event = await guild.create_scheduled_event(**kwargs)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the scheduled event creation.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Scheduled event creation failed: {exc}")
        return CmdResult(success=True, message=f"Created external scheduled event `{event.name}` ({event.id}).")

    @llm_command("event_create_voice", description="Create a voice or stage channel scheduled event (authorized admin)", args="name:start_iso:channel:description?")
    async def cmd_event_create_voice(self, ctx, name: str, start_iso: str, channel_ref: str, *description_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage scheduled events", required_permissions=["manage_events"])
        if error:
            return error
        start_time, start_error = self._parse_iso_datetime(start_iso)
        if start_error:
            return CmdResult(success=False, error=start_error)
        channel, channel_error = await self._resolve_voice_channel(guild, channel_ref)
        if channel_error:
            return CmdResult(success=False, error=channel_error)
        entity_type = self._entity_type_for_voice_channel(channel)
        description = ":".join(description_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Create voice scheduled event", f"name={name.strip()}")
        kwargs = {
            "name": name.strip(),
            "start_time": start_time,
            "entity_type": entity_type,
            "privacy_level": discord.PrivacyLevel.guild_only,
            "channel": channel,
            "reason": audit_reason,
        }
        if description:
            kwargs["description"] = description
        try:
            event = await guild.create_scheduled_event(**kwargs)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the scheduled event creation.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Scheduled event creation failed: {exc}")
        kind_label = "stage" if entity_type == discord.EntityType.stage_instance else "voice"
        return CmdResult(success=True, message=f"Created {kind_label} scheduled event `{event.name}` ({event.id}).")

    @llm_command("event_status", description="Update a scheduled event status (authorized admin)", args="event:status:reason?")
    async def cmd_event_status(self, ctx, event_ref: str, status_text: str, *reason_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage scheduled events", required_permissions=["manage_events"])
        if error:
            return error
        event, event_error = await self._resolve_scheduled_event(guild, event_ref)
        if event_error:
            return CmdResult(success=False, error=event_error)
        status_map = {
            "scheduled": discord.EventStatus.scheduled,
            "active": discord.EventStatus.active,
            "completed": discord.EventStatus.completed,
            "ended": discord.EventStatus.completed,
            "canceled": discord.EventStatus.cancelled,
            "cancelled": discord.EventStatus.cancelled,
        }
        status_value = status_map.get(status_text.strip().casefold())
        if status_value is None:
            return CmdResult(success=False, error="Event status must be one of `scheduled`, `active`, `completed`, or `canceled`.")
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Update scheduled event status", reason)
        try:
            await event.edit(status=status_value, reason=audit_reason)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the scheduled event status update.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Scheduled event status update failed: {exc}")
        return CmdResult(success=True, message=f"Set scheduled event `{event.name}` to {status_value.name}.")

    @llm_command("event_edit_config", description="Edit a scheduled event with keyed options (authorized admin)", args="event:option=value...")
    async def cmd_event_edit_config(self, ctx, event_ref: str, *option_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage scheduled events", required_permissions=["manage_events"])
        if error:
            return error
        event, event_error = await self._resolve_scheduled_event(guild, event_ref)
        if event_error:
            return CmdResult(success=False, error=event_error)
        options, options_error = self._parse_key_value_options(
            option_parts,
            allowed_keys={"name", "description", "location", "start", "end", "channel", "image"},
            subject="event edit",
        )
        if options_error:
            return CmdResult(success=False, error=options_error)

        edit_kwargs = {}
        if "name" in options:
            normalized_name = options["name"].strip()
            if not normalized_name:
                return CmdResult(success=False, error="Event name can't be empty.")
            edit_kwargs["name"] = normalized_name
        if "description" in options:
            edit_kwargs["description"] = None if self._is_clear_value(options["description"]) else options["description"]
        if "location" in options:
            edit_kwargs["location"] = None if self._is_clear_value(options["location"]) else options["location"]
        if "start" in options:
            start_time, start_error = self._parse_iso_datetime(options["start"])
            if start_error:
                return CmdResult(success=False, error=start_error)
            edit_kwargs["start_time"] = start_time
        if "end" in options:
            if self._is_clear_value(options["end"]):
                edit_kwargs["end_time"] = None
            else:
                end_time, end_error = self._parse_iso_datetime(options["end"])
                if end_error:
                    return CmdResult(success=False, error=end_error)
                edit_kwargs["end_time"] = end_time
        if "channel" in options:
            channel, channel_error = await self._resolve_voice_channel(guild, options["channel"])
            if channel_error:
                return CmdResult(success=False, error=channel_error)
            edit_kwargs["channel"] = channel
            edit_kwargs["entity_type"] = self._entity_type_for_voice_channel(channel)
        if "image" in options:
            attachment, attachment_error = await self._select_emoji_source_attachment(ctx, options["image"])
            if attachment_error:
                return CmdResult(success=False, error=attachment_error)
            try:
                edit_kwargs["image"] = await attachment.read()
            except discord.HTTPException as exc:
                return CmdResult(success=False, error=f"Couldn't read the event image attachment: {exc}")

        start_time = edit_kwargs.get("start_time", getattr(event, "start_time", None))
        end_time = edit_kwargs.get("end_time", getattr(event, "end_time", None))
        if start_time is not None and end_time is not None and end_time <= start_time:
            return CmdResult(success=False, error="Scheduled event end time must be after the start time.")

        if not edit_kwargs:
            return CmdResult(success=False, error="Event edit needs at least one keyed option.")

        audit_reason = self._build_audit_reason(ctx, "Edit scheduled event", None)
        try:
            await event.edit(reason=audit_reason, **edit_kwargs)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the scheduled event edit.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Scheduled event edit failed: {exc}")
        return CmdResult(success=True, message=f"Updated scheduled event `{getattr(event, 'name', 'unknown')}`.")

    @llm_command("event_delete", description="Delete a scheduled event (authorized admin)", args="event:reason?")
    async def cmd_event_delete(self, ctx, event_ref: str, *reason_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage scheduled events", required_permissions=["manage_events"])
        if error:
            return error
        event, event_error = await self._resolve_scheduled_event(guild, event_ref)
        if event_error:
            return CmdResult(success=False, error=event_error)
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Delete scheduled event", reason)

        async def execute() -> CmdResult:
            try:
                await event.delete(reason=audit_reason)
            except discord.Forbidden:
                return CmdResult(success=False, error="Discord denied the scheduled event delete.")
            except discord.HTTPException as exc:
                return CmdResult(success=False, error=f"Scheduled event delete failed: {exc}")
            return CmdResult(success=True, message=f"Deleted scheduled event `{event.name}`.")

        return await self._request_destructive_confirmation(ctx, summary=f"delete scheduled event {getattr(event, 'name', 'unknown')}", executor=execute)

    @llm_command("automod_list", description="List automod rules with optional page/filter (authorized admin)", args="page_or_filter?:filter?")
    async def cmd_automod_list(self, ctx, page_or_filter: str | None = None, *filter_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="review automod rules", required_permissions=["manage_guild"])
        if error:
            return error
        page, filter_text, parse_error = self._parse_page_or_filter_args(page_or_filter, filter_parts, subject="automod list")
        if parse_error:
            return CmdResult(success=False, error=parse_error)
        try:
            rules = await guild.fetch_automod_rules()
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the automod rule list.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Automod rule list failed: {exc}")
        if not rules:
            return CmdResult(success=True, message="No automod rules found in this server.")
        filtered_rules = [rule for rule in rules if not filter_text or self._automod_rule_matches_filter(rule, filter_text)]
        if not filtered_rules:
            return CmdResult(success=True, message=f"No automod rules matched `{filter_text}`.")
        page_items, total_pages = self._paginate_items(filtered_rules, page, page_size=REVIEW_LIST_PAGE_SIZE)
        if not page_items:
            return CmdResult(success=False, error=f"Automod list page `{page}` is out of range. There {'is' if total_pages == 1 else 'are'} {total_pages} page{'s' if total_pages != 1 else ''}.")
        lines = [self._format_automod_rule_line(rule) for rule in page_items]
        return CmdResult(success=True, message="\n".join([
            f"Automod rules in {getattr(guild, 'name', 'this server')}{f' matching `{filter_text}`' if filter_text else ''} (page {page}/{total_pages}; {len(filtered_rules)} match{'es' if len(filtered_rules) != 1 else ''}):",
            *lines,
        ]))

    @llm_command("automod_inspect", description="Inspect one automod rule (authorized admin)", args="rule")
    async def cmd_automod_inspect(self, ctx, rule_ref: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="inspect automod rules", required_permissions=["manage_guild"])
        if error:
            return error
        rule, rule_error = await self._resolve_automod_rule(guild, rule_ref)
        if rule_error:
            return CmdResult(success=False, error=rule_error)
        trigger = getattr(rule, "trigger", None)
        trigger_type = getattr(getattr(trigger, "type", None), "name", None) or "unknown"
        actions = getattr(rule, "actions", []) or []
        action_parts = [getattr(getattr(action, "type", None), "name", None) or "unknown" for action in actions]
        return CmdResult(
            success=True,
            message=(
                f"Automod rule `{rule.name}` `{rule.id}`\n"
                f"- enabled: {'yes' if getattr(rule, 'enabled', False) else 'no'}\n"
                f"- trigger: {trigger_type}\n"
                f"- actions: {', '.join(action_parts) or 'none'}"
            ),
        )

    @llm_command("automod_create_keyword_rule", description="Create a keyword-block automod rule (authorized admin)", args="name:keywords_csv:custom_message?")
    async def cmd_automod_create_keyword_rule(self, ctx, name: str, keywords_csv: str, *custom_message_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage automod rules", required_permissions=["manage_guild"])
        if error:
            return error
        keywords = self._parse_keyword_list(keywords_csv)
        if not keywords:
            return CmdResult(success=False, error="Keyword automod rules need at least one comma-separated keyword.")
        custom_message = ":".join(custom_message_parts).strip() or None
        trigger = discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.keyword, keyword_filter=keywords)
        actions = [discord.AutoModRuleAction(custom_message=custom_message)] if custom_message else [discord.AutoModRuleAction()]
        audit_reason = self._build_audit_reason(ctx, "Create keyword automod rule", f"name={name.strip()}")
        try:
            rule = await guild.create_automod_rule(
                name=name.strip(),
                event_type=discord.AutoModRuleEventType.message_send,
                trigger=trigger,
                actions=actions,
                enabled=True,
                reason=audit_reason,
            )
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the automod rule creation.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Automod rule creation failed: {exc}")
        return CmdResult(success=True, message=f"Created keyword automod rule `{rule.name}` ({rule.id}).")

    @llm_command("automod_create_keyword_rule_config", description="Create a keyword-block automod rule with keyed options (authorized admin)", args="name:keywords_csv:option=value...")
    async def cmd_automod_create_keyword_rule_config(self, ctx, name: str, keywords_csv: str, *option_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage automod rules", required_permissions=["manage_guild"])
        if error:
            return error
        keywords = self._parse_keyword_list(keywords_csv)
        if not keywords:
            return CmdResult(success=False, error="Keyword automod rules need at least one comma-separated keyword.")
        options, options_error = self._parse_key_value_options(
            option_parts,
            allowed_keys={"custom_message", "allow_list", "alert_channel", "timeout"},
            subject="keyword automod rule create",
        )
        if options_error:
            return CmdResult(success=False, error=options_error)
        allow_list = self._parse_keyword_list(options["allow_list"]) if "allow_list" in options else None
        actions, actions_error = await self._build_automod_actions(guild, options)
        if actions_error:
            return CmdResult(success=False, error=actions_error)
        trigger = discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.keyword, keyword_filter=keywords, allow_list=allow_list)
        audit_reason = self._build_audit_reason(ctx, "Create keyword automod rule", f"name={name.strip()}")
        try:
            rule = await guild.create_automod_rule(
                name=name.strip(),
                event_type=discord.AutoModRuleEventType.message_send,
                trigger=trigger,
                actions=actions,
                enabled=True,
                reason=audit_reason,
            )
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the automod rule creation.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Automod rule creation failed: {exc}")
        return CmdResult(success=True, message=f"Created keyword automod rule `{rule.name}` ({rule.id}).")

    @llm_command("automod_create_regex_rule", description="Create a regex-block automod rule (authorized admin)", args="name:patterns_csv:custom_message?")
    async def cmd_automod_create_regex_rule(self, ctx, name: str, patterns_csv: str, *custom_message_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage automod rules", required_permissions=["manage_guild"])
        if error:
            return error
        patterns = self._parse_keyword_list(patterns_csv)
        if not patterns:
            return CmdResult(success=False, error="Regex automod rules need at least one comma-separated pattern.")
        custom_message = ":".join(custom_message_parts).strip() or None
        trigger = discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.keyword, regex_patterns=patterns)
        actions = [discord.AutoModRuleAction(custom_message=custom_message)] if custom_message else [discord.AutoModRuleAction()]
        audit_reason = self._build_audit_reason(ctx, "Create regex automod rule", f"name={name.strip()}")
        try:
            rule = await guild.create_automod_rule(
                name=name.strip(),
                event_type=discord.AutoModRuleEventType.message_send,
                trigger=trigger,
                actions=actions,
                enabled=True,
                reason=audit_reason,
            )
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the automod rule creation.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Automod rule creation failed: {exc}")
        return CmdResult(success=True, message=f"Created regex automod rule `{rule.name}` ({rule.id}).")

    @llm_command("automod_create_preset_rule", description="Create a preset-filter automod rule (authorized admin)", args="name:presets_csv:option=value...")
    async def cmd_automod_create_preset_rule(self, ctx, name: str, presets_csv: str, *option_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage automod rules", required_permissions=["manage_guild"])
        if error:
            return error
        presets, presets_error = self._parse_automod_presets(presets_csv)
        if presets_error:
            return CmdResult(success=False, error=presets_error)
        options, options_error = self._parse_key_value_options(
            option_parts,
            allowed_keys={"custom_message", "allow_list", "alert_channel", "timeout"},
            subject="preset automod rule create",
        )
        if options_error:
            return CmdResult(success=False, error=options_error)
        allow_list = self._parse_keyword_list(options["allow_list"]) if "allow_list" in options else None
        actions, actions_error = await self._build_automod_actions(guild, options)
        if actions_error:
            return CmdResult(success=False, error=actions_error)
        trigger = discord.AutoModTrigger(
            type=discord.AutoModRuleTriggerType.keyword_preset,
            presets=presets,
            allow_list=allow_list,
        )
        audit_reason = self._build_audit_reason(ctx, "Create preset automod rule", f"name={name.strip()}")
        try:
            rule = await guild.create_automod_rule(
                name=name.strip(),
                event_type=discord.AutoModRuleEventType.message_send,
                trigger=trigger,
                actions=actions,
                enabled=True,
                reason=audit_reason,
            )
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the automod rule creation.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Automod rule creation failed: {exc}")
        return CmdResult(success=True, message=f"Created preset automod rule `{rule.name}` ({rule.id}).")

    @llm_command("automod_create_spam_rule", description="Create a spam automod rule (authorized admin)", args="name:option=value...")
    async def cmd_automod_create_spam_rule(self, ctx, name: str, *option_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage automod rules", required_permissions=["manage_guild"])
        if error:
            return error
        options, options_error = self._parse_key_value_options(
            option_parts,
            allowed_keys={"custom_message", "alert_channel", "timeout"},
            subject="spam automod rule create",
        )
        if options_error:
            return CmdResult(success=False, error=options_error)
        actions, actions_error = await self._build_automod_actions(guild, options)
        if actions_error:
            return CmdResult(success=False, error=actions_error)
        trigger = discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.spam)
        audit_reason = self._build_audit_reason(ctx, "Create spam automod rule", f"name={name.strip()}")
        try:
            rule = await guild.create_automod_rule(
                name=name.strip(),
                event_type=discord.AutoModRuleEventType.message_send,
                trigger=trigger,
                actions=actions,
                enabled=True,
                reason=audit_reason,
            )
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the automod rule creation.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Automod rule creation failed: {exc}")
        return CmdResult(success=True, message=f"Created spam automod rule `{rule.name}` ({rule.id}).")

    @llm_command("automod_create_mention_rule", description="Create a mention-spam automod rule (authorized admin)", args="name:mention_limit:timeout_duration?")
    async def cmd_automod_create_mention_rule(self, ctx, name: str, mention_limit_text: str, timeout_text: str | None = None) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage automod rules", required_permissions=["manage_guild"])
        if error:
            return error
        if not mention_limit_text.isdigit():
            return CmdResult(success=False, error="Mention limit must be an integer.")
        mention_limit = int(mention_limit_text)
        if mention_limit <= 0:
            return CmdResult(success=False, error="Mention limit must be greater than 0.")
        trigger = discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.mention_spam, mention_limit=mention_limit)
        if timeout_text:
            duration, duration_error = self._parse_timeout_duration(timeout_text)
            if duration_error:
                return CmdResult(success=False, error=duration_error)
            actions = [discord.AutoModRuleAction(duration=duration)]
        else:
            actions = [discord.AutoModRuleAction()]
        audit_reason = self._build_audit_reason(ctx, "Create mention automod rule", f"name={name.strip()}")
        try:
            rule = await guild.create_automod_rule(
                name=name.strip(),
                event_type=discord.AutoModRuleEventType.message_send,
                trigger=trigger,
                actions=actions,
                enabled=True,
                reason=audit_reason,
            )
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the automod rule creation.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Automod rule creation failed: {exc}")
        return CmdResult(success=True, message=f"Created mention-spam automod rule `{rule.name}` ({rule.id}).")

    @llm_command("automod_create_mention_rule_config", description="Create a mention-spam automod rule with keyed options (authorized admin)", args="name:mention_limit:option=value...")
    async def cmd_automod_create_mention_rule_config(self, ctx, name: str, mention_limit_text: str, *option_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage automod rules", required_permissions=["manage_guild"])
        if error:
            return error
        if not mention_limit_text.isdigit():
            return CmdResult(success=False, error="Mention limit must be an integer.")
        mention_limit = int(mention_limit_text)
        if mention_limit <= 0:
            return CmdResult(success=False, error="Mention limit must be greater than 0.")
        options, options_error = self._parse_key_value_options(
            option_parts,
            allowed_keys={"timeout", "alert_channel", "raid_protection"},
            subject="mention automod rule create",
        )
        if options_error:
            return CmdResult(success=False, error=options_error)
        actions, actions_error = await self._build_automod_actions(guild, options, allow_custom_message=False)
        if actions_error:
            return CmdResult(success=False, error=actions_error)
        trigger_kwargs = {
            "type": discord.AutoModRuleTriggerType.mention_spam,
            "mention_limit": mention_limit,
        }
        if "raid_protection" in options:
            raid_protection, toggle_error = self._parse_toggle_value(options["raid_protection"], label="raid protection")
            if toggle_error:
                return CmdResult(success=False, error=toggle_error)
            trigger_kwargs["mention_raid_protection"] = raid_protection
        trigger = discord.AutoModTrigger(**trigger_kwargs)
        audit_reason = self._build_audit_reason(ctx, "Create mention automod rule", f"name={name.strip()}")
        try:
            rule = await guild.create_automod_rule(
                name=name.strip(),
                event_type=discord.AutoModRuleEventType.message_send,
                trigger=trigger,
                actions=actions,
                enabled=True,
                reason=audit_reason,
            )
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the automod rule creation.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Automod rule creation failed: {exc}")
        return CmdResult(success=True, message=f"Created mention-spam automod rule `{rule.name}` ({rule.id}).")

    @llm_command("automod_toggle", description="Enable or disable an automod rule (authorized admin)", args="rule:on_or_off:reason?")
    async def cmd_automod_toggle(self, ctx, rule_ref: str, toggle_text: str, *reason_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage automod rules", required_permissions=["manage_guild"])
        if error:
            return error
        rule, rule_error = await self._resolve_automod_rule(guild, rule_ref)
        if rule_error:
            return CmdResult(success=False, error=rule_error)
        enabled, toggle_error = self._parse_toggle_value(toggle_text, label="automod enabled")
        if toggle_error:
            return CmdResult(success=False, error=toggle_error)
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Toggle automod rule", reason)
        try:
            await rule.edit(enabled=enabled, reason=audit_reason)
        except discord.Forbidden:
            return CmdResult(success=False, error="Discord denied the automod rule update.")
        except discord.HTTPException as exc:
            return CmdResult(success=False, error=f"Automod rule update failed: {exc}")
        return CmdResult(success=True, message=f"Set automod rule `{rule.name}` to {'enabled' if enabled else 'disabled'}.")

    @llm_command("automod_delete", description="Delete an automod rule (authorized admin)", args="rule:reason?")
    async def cmd_automod_delete(self, ctx, rule_ref: str, *reason_parts: str) -> CmdResult:
        guild, _bot_member, error = await self._ensure_admin_command(ctx, action="manage automod rules", required_permissions=["manage_guild"])
        if error:
            return error
        rule, rule_error = await self._resolve_automod_rule(guild, rule_ref)
        if rule_error:
            return CmdResult(success=False, error=rule_error)
        reason = ":".join(reason_parts).strip() or None
        audit_reason = self._build_audit_reason(ctx, "Delete automod rule", reason)

        async def execute() -> CmdResult:
            try:
                await rule.delete(reason=audit_reason)
            except discord.Forbidden:
                return CmdResult(success=False, error="Discord denied the automod rule delete.")
            except discord.HTTPException as exc:
                return CmdResult(success=False, error=f"Automod rule delete failed: {exc}")
            return CmdResult(success=True, message=f"Deleted automod rule `{rule.name}`.")

        return await self._request_destructive_confirmation(ctx, summary=f"delete automod rule {getattr(rule, 'name', 'unknown')}", executor=execute)
