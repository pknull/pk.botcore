"""Shared assistant conversation/runtime helpers for Discord bot cogs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import os
import re
import sys
import time

import discord

ACTIVE_TASK_CONTINUATION_RE = re.compile(
    r"^(?:"
    r"yes|yeah|yep|yup|no|nah|confirm|confirmed|do it|go ahead|continue|proceed|"
    r"including\b|exclude\b|except\b|spare\b|keep\b|remove\b|delete\b|all of them\b|"
    r"that one\b|those\b|them\b|it\b"
    r")",
    re.IGNORECASE,
)


class ConversationTracker:
    """Track active conversations for engagement-based responses."""

    def __init__(self, engagement_window: int = 300):
        self._last_spoke: dict[int, float] = {}
        self._engagement_window = engagement_window
        self._pending_explicit: dict[int, bool] = {}

    def mark_explicit(self, context_id: int, explicit: bool) -> None:
        self._pending_explicit[context_id] = explicit

    def record_response(self, context_id: int) -> None:
        explicit = self._pending_explicit.pop(context_id, True)
        if explicit:
            self._last_spoke[context_id] = time.time()

    def is_engaged(self, context_id: int) -> bool:
        last = self._last_spoke.get(context_id, 0)
        return (time.time() - last) < self._engagement_window

    def clear(self, context_id: int) -> None:
        self._last_spoke.pop(context_id, None)
        self._pending_explicit.pop(context_id, None)

    def get_context_id(self, message: discord.Message) -> int:
        if isinstance(message.channel, discord.Thread):
            return message.channel.id
        return message.channel.id


@dataclass
class ActiveTaskState:
    """Recent task state to carry across terse follow-ups."""

    updated_at: float
    requester_id: int
    original_prompt: str
    latest_reply: str
    internal_messages: list[str] = field(default_factory=list)
    visible_messages: list[str] = field(default_factory=list)
    command_driven: bool = False


class AssistantRuntimeMixin:
    """Shared runtime helpers for assistant-style Discord cogs."""

    ACTIVE_TASK_WINDOW_SECONDS = 15 * 60
    ACTIVE_TASK_REPLY_LIMIT = 1200
    ACTIVE_TASK_MESSAGE_LIMIT = 6
    RESTART_DELAY_SECONDS = 2.0

    def _init_assistant_runtime(
        self,
        *,
        engagement_window: int = 300,
        queue_maxsize: int | None = None,
    ) -> None:
        if queue_maxsize is None:
            try:
                queue_maxsize = int(
                    os.getenv("PK_BOTCORE_CHANNEL_QUEUE_MAXSIZE", "25")
                )
            except ValueError:
                queue_maxsize = 25
        self._queue_maxsize = max(1, queue_maxsize)
        self._owner_id: int | None = None
        self._message_queues: dict[int, asyncio.Queue] = {}
        self._queue_workers: dict[int, asyncio.Task] = {}
        self._conversation_tracker = ConversationTracker(engagement_window=engagement_window)
        self._active_task_state: dict[int, ActiveTaskState] = {}

    def _runtime_logger(self) -> logging.Logger:
        return getattr(self, "_assistant_logger", logging.getLogger(type(self).__module__))

    async def _queue_message(self, channel_id: int, work_item: tuple) -> bool:
        """Admit a message without creating unbounded blocked producers."""
        if channel_id not in self._message_queues:
            self._message_queues[channel_id] = asyncio.Queue(
                maxsize=self._queue_maxsize
            )

        try:
            self._message_queues[channel_id].put_nowait(work_item)
        except asyncio.QueueFull:
            self._runtime_logger().warning(
                "Channel queue %s is full; rejecting work item", channel_id
            )
            return False

        if channel_id not in self._queue_workers or self._queue_workers[channel_id].done():
            self._queue_workers[channel_id] = asyncio.create_task(self._process_queue(channel_id))
        return True

    async def _process_queue(self, channel_id: int) -> None:
        """Process queued messages one at a time."""
        queue = self._message_queues.get(channel_id)
        if not queue:
            return

        current_task = asyncio.current_task()
        try:
            while True:
                try:
                    work_item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                try:
                    await self._process_queue_item(*work_item)
                except Exception as exc:
                    self._runtime_logger().exception("Error processing queued message: %s", exc)
                finally:
                    queue.task_done()
        finally:
            if (
                self._queue_workers.get(channel_id) is current_task
                and queue.empty()
            ):
                self._queue_workers.pop(channel_id, None)
                self._message_queues.pop(channel_id, None)

    async def _process_queue_item(self, *args):
        raise NotImplementedError

    async def _get_owner_id(self) -> int:
        """Get and cache the bot owner ID."""
        if self._owner_id is None:
            app_info = await self.bot.application_info()
            self._owner_id = app_info.owner.id
        return self._owner_id

    async def _delayed_restart(self) -> None:
        """Replace the current bot process after a short delay."""
        await asyncio.sleep(self.RESTART_DELAY_SECONDS)
        self._runtime_logger().warning("Restarting bot process via execv")
        os.execv(sys.executable, [sys.executable, *sys.argv])

    def _strip_bot_mention(self, content: str) -> str:
        """Remove bot mention from message content."""
        result = content.replace(f"<@{self.bot.user.id}>", "").strip()
        result = result.replace(f"<@!{self.bot.user.id}>", "").strip()
        return result

    def _clear_conversation_context(self, context_id: int) -> None:
        """Clear ephemeral conversation state for a context."""
        self._active_task_state.pop(context_id, None)
        self._conversation_tracker.clear(context_id)

    def _prune_stale_active_tasks(self) -> None:
        cutoff = time.time() - self.ACTIVE_TASK_WINDOW_SECONDS
        stale_contexts = [
            context_id
            for context_id, state in self._active_task_state.items()
            if state.updated_at < cutoff
        ]
        for context_id in stale_contexts:
            self._active_task_state.pop(context_id, None)

    def _get_recent_active_task_state(self, context_id: int) -> ActiveTaskState | None:
        self._prune_stale_active_tasks()
        state = self._active_task_state.get(context_id)
        if state is None:
            return None
        if (time.time() - state.updated_at) > self.ACTIVE_TASK_WINDOW_SECONDS:
            self._active_task_state.pop(context_id, None)
            return None
        return state

    def _truncate_active_task_text(self, text: str, *, limit: int | None = None) -> str:
        limit = limit or self.ACTIVE_TASK_REPLY_LIMIT
        text = text.strip()
        if len(text) <= limit:
            return text
        omitted = len(text) - limit
        return f"{text[:limit].rstrip()}\n...[truncated {omitted} chars]"

    def _should_track_active_task(
        self,
        *,
        cleaned_text: str,
        internal_messages: list[str] | None = None,
        visible_messages: list[str] | None = None,
        saw_commands: bool,
    ) -> bool:
        internal_messages = internal_messages or []
        visible_messages = visible_messages or []
        normalized_reply = cleaned_text.casefold()
        return bool(
            saw_commands
            or internal_messages
            or visible_messages
            or "no changes made yet" in normalized_reply
            or "confirm" in normalized_reply
        )

    def _record_active_task_state(
        self,
        *,
        context_id: int,
        requester_id: int,
        original_prompt: str,
        cleaned_text: str,
        internal_messages: list[str] | None = None,
        visible_messages: list[str] | None = None,
        saw_commands: bool,
    ) -> None:
        internal_messages = internal_messages or []
        visible_messages = visible_messages or []
        if not self._should_track_active_task(
            cleaned_text=cleaned_text,
            internal_messages=internal_messages,
            visible_messages=visible_messages,
            saw_commands=saw_commands,
        ):
            self._active_task_state.pop(context_id, None)
            return

        self._active_task_state[context_id] = ActiveTaskState(
            updated_at=time.time(),
            requester_id=requester_id,
            original_prompt=original_prompt.strip(),
            latest_reply=self._truncate_active_task_text(cleaned_text) if cleaned_text.strip() else "",
            internal_messages=internal_messages[-self.ACTIVE_TASK_MESSAGE_LIMIT:],
            visible_messages=visible_messages[-self.ACTIVE_TASK_MESSAGE_LIMIT:],
            command_driven=saw_commands,
        )

    def _actor_may_continue_active_task(
        self,
        state: ActiveTaskState | None,
        *,
        actor_id: int | None,
        is_keeper: bool,
    ) -> bool:
        if state is None:
            return False
        if is_keeper:
            return True
        return actor_id is not None and actor_id == state.requester_id

    def _message_likely_continues_active_task(self, prompt: str, state: ActiveTaskState | None) -> bool:
        if state is None:
            return False
        normalized = " ".join(str(prompt or "").split()).strip()
        if not normalized:
            return False
        if len(normalized) <= 120 and ACTIVE_TASK_CONTINUATION_RE.search(normalized):
            return True
        lowered = normalized.casefold()
        if len(normalized) <= 200 and any(
            phrase in lowered
            for phrase in (
                "all of them",
                "including",
                "spare",
                "keep milkshake",
                "including milkshake",
                "yes, all",
                "yes all",
            )
        ):
            return True
        return False

    def _augment_prompt_with_active_task_state(
        self,
        *,
        context_id: int,
        prompt: str,
        actor_id: int | None,
        is_keeper: bool,
    ) -> str:
        state = self._get_recent_active_task_state(context_id)
        if (
            state is None
            or not self._actor_may_continue_active_task(state, actor_id=actor_id, is_keeper=is_keeper)
            or not self._message_likely_continues_active_task(prompt, state)
        ):
            return prompt

        sections = [
            "Recent active task context from this conversation:",
            f"- prior user request: {state.original_prompt}",
        ]
        if state.latest_reply:
            sections.append(f"- your last visible reply: {state.latest_reply}")
        if state.internal_messages:
            sections.append("- internal command/task state:")
            sections.extend(
                f"  - {self._truncate_active_task_text(message, limit=400)}"
                for message in state.internal_messages
            )
        if state.visible_messages:
            sections.append("- visible command results already shown:")
            sections.extend(
                f"  - {self._truncate_active_task_text(message, limit=400)}"
                for message in state.visible_messages
            )
        sections.extend(
            [
                "Treat the new user message as a continuation of that same task unless it clearly changes topics.",
                "",
                prompt,
            ]
        )
        return "\n".join(sections)

    async def _build_recent_relevance_context(
        self,
        message: discord.Message,
        *,
        limit: int = 6,
    ) -> list[tuple[str, str]]:
        recent_context: list[tuple[str, str]] = []
        history = getattr(message.channel, "history", None)
        if not callable(history):
            return recent_context
        try:
            async for msg in message.channel.history(limit=limit, before=message):
                author_label = (
                    getattr(msg.author, "display_name", None)
                    or getattr(msg.author, "name", None)
                    or "unknown"
                )
                if getattr(msg.author, "bot", False):
                    author_label += " (bot)"
                recent_context.append((author_label, (getattr(msg, "content", "") or "")[:500]))
        except discord.HTTPException:
            return recent_context
        recent_context.reverse()
        return recent_context

    async def _get_reply_context(self, message: discord.Message) -> str:
        """Get context from the message being replied to, if any."""
        reference = getattr(message, "reference", None)
        if reference is None or not getattr(reference, "message_id", None):
            return ""

        resolved = getattr(reference, "resolved", None)
        if resolved is not None:
            ref_msg = resolved
        else:
            try:
                ref_msg = await message.channel.fetch_message(reference.message_id)
            except (discord.NotFound, discord.HTTPException):
                return ""

        if not ref_msg or not ref_msg.content:
            return ""

        author_name = ref_msg.author.display_name
        is_bot = ref_msg.author.bot
        bot_label = " (bot)" if is_bot else ""
        content = ref_msg.content
        if len(content) > 1500:
            content = content[:1500] + "..."

        return f"[Replying to {author_name}{bot_label}]: {content}\n\n"

    def _extract_command_messages(self, cmd_results: list[object]) -> list[str]:
        messages: list[str] = []
        for cmd_result in cmd_results:
            message = getattr(cmd_result, "message", None)
            error = getattr(cmd_result, "error", None)
            if message:
                messages.append(message)
            elif error:
                messages.append(f"Command error: {error}")
        return messages
