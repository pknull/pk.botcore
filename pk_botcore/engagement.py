"""Shared engagement pipeline: decides whether a bot speaks at all.

One ordered gate sequence, identical for every bot, parameterized by an
:class:`EngagementPolicy`. The four channel modes:

- ``ignore``  — conversational pipeline never engages (checked first).
- ``mention`` — literal @mention only; no name trigger, no engagement window.
- ``social``  — @mention, name-addressing, or engagement-window follow-ups.
- ``listen``  — social triggers plus strict-domain relevance interjection.

A timed channel mute (slash command or stop phrase) overlays strict-mention
semantics: only literal @mentions get replies, and those replies do not open
an engagement window.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
import logging
import re
import time
from typing import Awaitable, Callable, Sequence

import discord

from . import classifiers
from .channels import MODE_IGNORE, MODE_LISTEN, MODE_MENTION, MODE_SOCIAL

logger = logging.getLogger("pk_botcore.engagement")

DEFAULT_MUTE_SECONDS = 600

# Full-match stop phrases (after normalization strips mentions/names/"please").
STOP_PHRASE_RE = re.compile(
    r"(?:"
    r"stfu|"
    r"shut\s+(?:the\s+fuck\s+)?up|"
    r"stop(?:\s+(?:talking|responding|replying))?|"
    r"be\s+quiet|"
    r"enough"
    r")",
    re.IGNORECASE,
)


def is_addressed_by_name(content: str, name: str) -> bool:
    """Check if the bot is addressed by name (word boundary, case-insensitive)."""
    pattern = rf"\b{re.escape(name)}\b"
    return bool(re.search(pattern, content, re.IGNORECASE))


def addressed_by_any_name(content: str, names: Sequence[str]) -> bool:
    return any(is_addressed_by_name(content, name) for name in names)


def another_bot_mentioned(
    message: discord.Message, self_id: int
) -> discord.abc.User | None:
    """Return the first other bot @mentioned in the message, if any."""
    for mention in message.mentions:
        if getattr(mention, "bot", False) and mention.id != self_id:
            return mention
    return None


def is_stop_phrase(
    content: str,
    bot_id: int | None = None,
    name_aliases: Sequence[str] = (),
) -> bool:
    """Return whether content is an explicit request for the bot to stop talking.

    The entire message (minus mentions of the bot, its name, and "please")
    must be a stop phrase — "we're good, shut up now" does not match; the
    slash-command mute is the guaranteed mechanism.
    """
    normalized = content.strip()
    if bot_id is not None:
        normalized = re.sub(rf"<@!?{bot_id}>", " ", normalized)
    for alias in name_aliases:
        name = re.escape(alias)
        normalized = re.sub(
            rf"^\s*{name}\s*[,.:;!?-]*\s*", "", normalized, flags=re.IGNORECASE
        )
        normalized = re.sub(
            rf"\s*[,.:;!?-]*\s*{name}\s*$", "", normalized, flags=re.IGNORECASE
        )
    normalized = re.sub(r"^\s*please\s+", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+please\s*$", "", normalized, flags=re.IGNORECASE)
    normalized = normalized.strip(" \t\r\n,.:;!?-")
    return bool(STOP_PHRASE_RE.fullmatch(normalized))


@dataclass(frozen=True)
class AccessResult:
    """Outcome of a bot-specific access policy check."""

    allowed: bool
    speaker_name: str = ""
    role: str = "User"  # "Keeper" | "Player" | "User" | "Bot"
    is_owner: bool = False
    rejection_reply: str | None = None  # sent only on a direct guild @mention


@dataclass(frozen=True)
class EngagementPolicy:
    """Per-bot parameters for the shared engagement pipeline."""

    bot_name: str
    name_aliases: tuple[str, ...]
    peer_aliases: tuple[str, ...] = ()
    topics_of_interest: tuple[str, ...] = ()
    out_of_scope: tuple[str, ...] = ()
    access_check: Callable[[discord.Message], Awaitable[AccessResult]] | None = None
    command_prefixes: tuple[str, ...] = ("!",)
    rate_limit: int = 5
    rate_window: float = 60.0
    default_mute_seconds: int = DEFAULT_MUTE_SECONDS
    engagement_window: int = 300


@dataclass(frozen=True)
class EngagementDecision:
    """Result of the pipeline: whether and how to engage with a message."""

    engage: bool
    reason: str
    prompt: str = ""
    speaker_name: str = ""
    role: str = "User"
    is_owner: bool = False
    is_bot_message: bool = False
    # True for soft engagements (relevance interjections, window follow-ups):
    # the message wasn't addressed to us, so the generator may abstain.
    abstainable: bool = False


# Reasons where the bot chose to interject rather than being addressed.
SOFT_ENGAGE_REASONS = frozenset({"relevance", "engagement"})

# Appended to the LLM prompt for soft engagements only. A reply of exactly
# [PASS] is suppressed by the cogs: no message, no window, no memory update.
ABSTAIN_NOTE = (
    "\n\n[Note: this message was not addressed to you directly - you are "
    "choosing to interject. If you have nothing genuinely worth adding, "
    "reply with exactly [PASS] and nothing else, and you will stay silent.]"
)


def is_abstain_reply(text: str) -> bool:
    """True when a generated reply is the abstain sentinel."""
    return text.strip().strip("`").strip() in ("[PASS]", "PASS")


def _skip(reason: str) -> EngagementDecision:
    return EngagementDecision(engage=False, reason=reason)


class EngagementGateMixin:
    """Shared speak/stay-silent pipeline for assistant-style Discord cogs.

    Hosts must also provide the AssistantRuntimeMixin state (conversation
    tracker, queues, active-task helpers) plus ``self.bot`` and, for guild
    mode lookups, ``self.channel_configs``.
    """

    def _init_engagement(self, policy: EngagementPolicy | None) -> None:
        self._engagement_policy = policy
        self._mute_until: dict[int, float] = {}
        self._entry_timestamps: dict[int, deque[float]] = defaultdict(deque)

    # -- mute / silence -------------------------------------------------

    def _is_muted(self, context_id: int) -> bool:
        deadline = self._mute_until.get(context_id)
        if deadline is None:
            return False
        if time.monotonic() >= deadline:
            self._mute_until.pop(context_id, None)
            return False
        return True

    def _mute_remaining(self, context_id: int) -> float:
        if not self._is_muted(context_id):
            return 0.0
        return self._mute_until[context_id] - time.monotonic()

    async def _mute_channel(self, context_id: int, seconds: float) -> None:
        """Silence a channel for a duration: strict-mention overlay + purge."""
        self._mute_until[context_id] = time.monotonic() + max(0.0, seconds)
        await self._silence_context(context_id)

    def _unmute_channel(self, context_id: int) -> bool:
        return self._mute_until.pop(context_id, None) is not None

    async def _silence_context(self, context_id: int) -> None:
        """End engagement and cancel queued conversational work without replying."""
        self._clear_conversation_context(context_id)

        queue = self._message_queues.get(context_id)
        abandoned_items = []
        if queue is not None:
            while True:
                try:
                    abandoned_items.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
                else:
                    queue.task_done()

        worker = self._queue_workers.get(context_id)
        if worker is not None and not worker.done() and worker is not asyncio.current_task():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

        if self._queue_workers.get(context_id) is worker:
            self._queue_workers.pop(context_id, None)
        if queue is None or queue.empty():
            self._message_queues.pop(context_id, None)

        for work_item in abandoned_items:
            try:
                await self._on_work_item_discarded(work_item)
            except Exception:
                logger.exception("Error cleaning up discarded work item")

        # Items enqueued while we were awaiting the cancelled worker (e.g. a
        # muted-@mention breakthrough) would otherwise strand with no worker.
        surviving = self._message_queues.get(context_id)
        if surviving is not None and not surviving.empty():
            current = self._queue_workers.get(context_id)
            if current is None or current.done():
                self._queue_workers[context_id] = asyncio.create_task(
                    self._process_queue(context_id)
                )

    async def _on_work_item_discarded(self, work_item: tuple) -> None:
        """Hook for bots to clean up abandoned queue items (attachments, reactions)."""

    def _decision_still_deliverable(
        self, message: discord.Message, decision: EngagementDecision
    ) -> bool:
        """Recheck mute state after slow tail work (attachment downloads etc.).

        A mute issued between the engagement decision and queue admission
        drops the message — except literal @mentions, which break through.
        """
        if not decision.engage:
            return False
        context_id = self._conversation_tracker.get_context_id(message)
        return not self._is_muted(context_id) or decision.reason == "mention"

    def _context_has_active_work(self, context_id: int) -> bool:
        queue = self._message_queues.get(context_id)
        if queue is not None and not queue.empty():
            return True
        worker = self._queue_workers.get(context_id)
        return worker is not None and not worker.done()

    # -- rate limiting --------------------------------------------------

    def _charge_entry_rate_limit(self, user_id: int, *, exempt: bool) -> bool:
        """Charge one admission; return True when the user is over budget."""
        policy = self._engagement_policy
        if exempt or policy.rate_limit <= 0:
            return False
        now = time.monotonic()
        timestamps = self._entry_timestamps[user_id]
        cutoff = now - policy.rate_window
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()
        if len(timestamps) >= policy.rate_limit:
            return True
        timestamps.append(now)
        return False

    # -- pipeline -------------------------------------------------------

    # Skip reasons worth surfacing at INFO: genuinely anomalous drops an
    # operator would otherwise have to diagnose by inference. Ordinary
    # non-triggers (no_trigger, command_prefix, ignored, self, bot_dm)
    # stay quiet.
    NOTABLE_SKIP_REASONS = frozenset({
        "stop",
        "unauthorized",
        "rate_limited",
        "bot_disengage",
    })

    # Skip reasons that are the ordinary outcome in a two-bot listen setup
    # (relevance misses, peer traffic, mute windows, deferring to a mentioned
    # peer). Logged at DEBUG so a live console stays readable; relevance
    # outcomes are also recorded as structured interaction events.
    ROUTINE_SKIP_REASONS = frozenset({
        "deferred",
        "muted",
        "irrelevant",
        "peer_addressed",
    })

    async def _decide_engagement(self, message: discord.Message) -> EngagementDecision:
        """Run the gate sequence and surface notable silent drops."""
        decision = await self._run_engagement_gates(message)
        if not decision.engage:
            if decision.reason in self.NOTABLE_SKIP_REASONS:
                level = logging.INFO
            elif decision.reason in self.ROUTINE_SKIP_REASONS:
                level = logging.DEBUG
            else:
                level = None
            if level is not None:
                runtime_logger = getattr(self, "_runtime_logger", None)
                log = runtime_logger() if callable(runtime_logger) else logger
                log.log(
                    level,
                    "Engagement skip: %s (author=%s channel=%s)",
                    decision.reason,
                    getattr(message.author, "display_name", message.author.id),
                    getattr(message.channel, "id", None),
                )
        return decision

    async def _run_engagement_gates(self, message: discord.Message) -> EngagementDecision:
        """Run the canonical gate sequence for an incoming message."""
        policy = self._engagement_policy
        if policy is None:
            raise RuntimeError("EngagementPolicy not configured")

        if message.author.id == self.bot.user.id:
            return _skip("self")
        content = message.content or ""
        if content.startswith(tuple(policy.command_prefixes)):
            return _skip("command_prefix")

        mode = self._resolve_channel_mode(message)
        if message.guild is not None and mode == MODE_IGNORE:
            return _skip("ignored")

        context_id = self._conversation_tracker.get_context_id(message)
        muted = self._is_muted(context_id)
        is_mentioned = self.bot.user in message.mentions
        if muted and not is_mentioned:
            return _skip("muted")
        if not muted and await self._stop_phrase_gate(message, context_id):
            return _skip("stop")

        access = await self._engagement_access(message)
        if not access.allowed:
            await self._send_access_rejection(message, access)
            return _skip("unauthorized")

        if message.guild is None:
            decision = await self._decide_dm(message, access)
        else:
            decision = await self._decide_guild(
                message, access, mode, context_id, muted
            )
        if decision.engage:
            self._log_engagement_message(message, decision, mode)
        return decision

    def _resolve_channel_mode(self, message: discord.Message) -> str | None:
        if message.guild is None:
            return None
        configs = getattr(self, "channel_configs", {})
        return getattr(configs.get(message.channel.id), "mode", MODE_MENTION)

    async def _stop_phrase_gate(
        self, message: discord.Message, context_id: int
    ) -> bool:
        """Mute the channel when an addressed human asks the bot to stop."""
        if message.author.bot:
            return False
        policy = self._engagement_policy
        content = message.content or ""
        if not is_stop_phrase(content, self.bot.user.id, policy.name_aliases):
            return False
        addressed = (
            message.guild is None
            or self.bot.user in message.mentions
            or addressed_by_any_name(content, policy.name_aliases)
            or self._conversation_tracker.is_engaged(context_id)
            or self._context_has_active_work(context_id)
        )
        if not addressed:
            return False
        logger.info(
            "Stop phrase from %s; muting context %s for %ss",
            message.author.id, context_id, policy.default_mute_seconds,
        )
        await self._mute_channel(context_id, policy.default_mute_seconds)
        return True

    async def _engagement_access(self, message: discord.Message) -> AccessResult:
        if message.author.bot:
            return AccessResult(
                allowed=True,
                speaker_name=message.author.display_name,
                role="Bot",
            )
        policy = self._engagement_policy
        if policy.access_check is None:
            return AccessResult(
                allowed=True, speaker_name=message.author.display_name
            )
        return await policy.access_check(message)

    async def _send_access_rejection(
        self, message: discord.Message, access: AccessResult
    ) -> None:
        if (
            access.rejection_reply
            and message.guild is not None
            and self.bot.user in message.mentions
        ):
            try:
                await message.channel.send(access.rejection_reply)
            except discord.HTTPException:
                pass

    async def _decide_dm(
        self, message: discord.Message, access: AccessResult
    ) -> EngagementDecision:
        if message.author.bot:
            return _skip("bot_dm")
        if self._charge_entry_rate_limit(message.author.id, exempt=access.is_owner):
            return _skip("rate_limited")
        context_id = self._conversation_tracker.get_context_id(message)
        self._conversation_tracker.mark_explicit(context_id, True)
        return self._engage(message, access, reason="dm")

    async def _decide_guild(
        self,
        message: discord.Message,
        access: AccessResult,
        mode: str | None,
        context_id: int,
        muted: bool,
    ) -> EngagementDecision:
        policy = self._engagement_policy
        is_bot_message = bool(message.author.bot)
        is_mentioned = self.bot.user in message.mentions
        content = message.content or ""

        other_bot = another_bot_mentioned(message, self.bot.user.id)
        if other_bot is not None and not is_mentioned:
            return _skip("deferred")

        trigger = None
        if is_mentioned:
            trigger = "mention"
        elif not muted and mode in (MODE_SOCIAL, MODE_LISTEN):
            if addressed_by_any_name(content, policy.name_aliases):
                trigger = "name"
            elif (
                not is_bot_message
                and self._conversation_tracker.is_engaged(context_id)
                and not addressed_by_any_name(content, policy.peer_aliases)
            ):
                trigger = "engagement"

        if trigger is None:
            if muted or mode != MODE_LISTEN:
                return _skip("muted" if muted else "no_trigger")
            return await self._decide_listen(message, access, context_id)

        if self._charge_entry_rate_limit(message.author.id, exempt=access.is_owner):
            return _skip("rate_limited")
        # Direct peer @mentions are guaranteed a reply, like human mentions:
        # rate caps and the no-window rule for bots bound any ping-pong. The
        # continuation gate still guards name-addressing and listen relevance.
        if (
            is_bot_message
            and trigger != "mention"
            and not await self._bot_continuation_allows(message)
        ):
            return _skip("bot_disengage")

        # Muted @mentions answer but must not open an engagement window.
        explicit = trigger in ("mention", "name") and not muted
        self._conversation_tracker.mark_explicit(context_id, explicit)
        return self._engage(message, access, reason=trigger)

    async def _decide_listen(
        self,
        message: discord.Message,
        access: AccessResult,
        context_id: int,
    ) -> EngagementDecision:
        """Unaddressed message in a listen channel: relevance interjection."""
        policy = self._engagement_policy
        # A message that opens with the peer bot's name belongs to their
        # conversation - stay out without spending a relevance call.
        if addressed_by_any_name(message.content or "", policy.peer_aliases):
            return _skip("peer_addressed")
        active_task_state = self._get_recent_active_task_state(context_id)
        if (
            self._actor_may_continue_active_task(
                active_task_state,
                actor_id=getattr(message.author, "id", None),
                is_keeper=access.is_owner,
            )
            and self._message_likely_continues_active_task(
                message.content, active_task_state
            )
        ):
            if self._charge_entry_rate_limit(
                message.author.id, exempt=access.is_owner
            ):
                return _skip("rate_limited")
            self._conversation_tracker.mark_explicit(context_id, True)
            return self._engage(message, access, reason="task_continuation")

        if self._charge_entry_rate_limit(message.author.id, exempt=access.is_owner):
            return _skip("rate_limited")

        base_context = await self._build_recent_relevance_context(message, limit=8)
        relevance_context = list(base_context)
        if active_task_state is not None:
            relevance_context.append((
                f"{policy.bot_name} task state",
                self._truncate_active_task_text(
                    "\n".join([
                        f"prior request: {active_task_state.original_prompt}",
                        f"last reply: {active_task_state.latest_reply}",
                        *(f"internal: {msg}" for msg in active_task_state.internal_messages),
                        *(f"visible result: {msg}" for msg in active_task_state.visible_messages),
                    ]),
                    limit=1000,
                ),
            ))

        is_relevant = await classifiers.check_relevance(
            message.content,
            bot_name=policy.bot_name,
            topics_of_interest=policy.topics_of_interest,
            out_of_scope=policy.out_of_scope,
            recent_context=relevance_context,
        )
        self._log_relevance_check(message, is_relevant)
        if not is_relevant:
            return _skip("irrelevant")

        if message.author.bot and not await self._bot_continuation_allows(
            message, recent_context=base_context
        ):
            return _skip("bot_disengage")
        # An accepted interjection opens the engagement window: follow-ups
        # within it ride the social triggers instead of paying for relevance.
        self._conversation_tracker.mark_explicit(context_id, True)
        return self._engage(message, access, reason="relevance")

    async def _bot_continuation_allows(
        self,
        message: discord.Message,
        recent_context: list[tuple[str, str]] | None = None,
    ) -> bool:
        """Gate bot-authored triggers through the continuation classifier."""
        policy = self._engagement_policy
        if recent_context is None:
            recent_context = await self._build_recent_relevance_context(
                message, limit=8
            )
        my_name = self.bot.user.display_name
        our_recent_msgs = sum(
            1 for author, _ in recent_context if my_name in author
        )
        if our_recent_msgs == 0:
            logger.debug("First bot interaction, skipping continuation check")
            return True
        should_continue = await classifiers.check_bot_continuation(
            bot_name=policy.bot_name,
            other_bot=message.author.display_name,
            message=(message.content or "")[:500],
            recent_context=recent_context,
        )
        if not should_continue:
            logger.debug(
                "Disengaging from bot conversation: %s",
                message.author.display_name,
            )
        return should_continue

    def _engage(
        self, message: discord.Message, access: AccessResult, *, reason: str
    ) -> EngagementDecision:
        content = message.content or ""
        if self.bot.user in message.mentions:
            prompt = self._strip_bot_mention(content)
        else:
            prompt = content.strip()
        return EngagementDecision(
            engage=True,
            reason=reason,
            prompt=prompt,
            speaker_name=access.speaker_name,
            role=access.role,
            is_owner=access.is_owner,
            is_bot_message=bool(message.author.bot),
            abstainable=reason in SOFT_ENGAGE_REASONS,
        )

    # -- interaction logging --------------------------------------------

    def _log_relevance_check(self, message: discord.Message, passed: bool) -> None:
        interactions = getattr(self, "_interactions", None)
        if interactions is None:
            return
        interactions.log_relevance_check(
            channel_id=message.channel.id,
            user_id=message.author.id,
            user_name=message.author.display_name,
            content=message.content,
            passed=passed,
            is_bot=bool(message.author.bot),
            channel_name=getattr(message.channel, "name", None),
            guild_id=message.guild.id if message.guild else None,
            guild_name=message.guild.name if message.guild else None,
        )

    def _log_engagement_message(
        self,
        message: discord.Message,
        decision: EngagementDecision,
        mode: str | None,
    ) -> None:
        interactions = getattr(self, "_interactions", None)
        if interactions is None:
            return
        interactions.log_message_received(
            channel_id=message.channel.id,
            user_id=message.author.id,
            user_name=message.author.display_name,
            content=decision.prompt,
            is_bot=decision.is_bot_message,
            channel_name=getattr(message.channel, "name", None),
            guild_id=message.guild.id if message.guild else None,
            guild_name=message.guild.name if message.guild else None,
            is_dm=message.guild is None,
            is_mention=self.bot.user in message.mentions,
            listen_mode=mode == MODE_LISTEN,
        )
