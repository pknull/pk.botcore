"""Bounded, persisted memory for guild-channel assistant turns."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import logging
import math
import os
from pathlib import Path
import time

import discord

from .classifiers import summarize_exchange
from .storage import atomic_json_dump

logger = logging.getLogger("pk_botcore.memory")

SUMMARY_TTL_SECONDS = 24 * 3600
SUMMARY_MAX_CHARS = 1000
_SUMMARY_HARD_MAX_CHARS = 1200


def _message_timestamp_in_window(message, cutoff) -> bool:
    created_at = getattr(message, "created_at", None)
    return created_at is None or created_at >= cutoff


def _format_history_message(message, char_limit: int) -> tuple[str, str]:
    author = getattr(message, "author", None)
    label = (
        getattr(author, "display_name", None)
        or getattr(author, "name", None)
        or "unknown"
    )
    if getattr(author, "bot", False):
        label += " (bot)"
    content = (getattr(message, "content", "") or "")[:char_limit]
    return label, content


async def _read_history(history, **kwargs) -> list:
    return [message async for message in history(**kwargs)]


async def build_memory_window(
    message: discord.Message | discord.Interaction,
    *,
    minutes: int = 45,
    cap: int = 25,
    floor: int = 3,
    char_limit: int = 500,
) -> list[tuple[str, str]]:
    """Build a newest-capped, oldest-first guild-channel message window."""
    channel = getattr(message, "channel", None)
    history = getattr(channel, "history", None)
    if not callable(history):
        return []

    cutoff = discord.utils.utcnow() - timedelta(minutes=minutes)
    try:
        recent = await _read_history(
            history,
            limit=cap,
            before=message,
            after=cutoff,
            oldest_first=False,
        )
        recent = [
            item for item in recent
            if _message_timestamp_in_window(item, cutoff)
        ][:cap]
        if not recent:
            recent = await _read_history(
                history,
                limit=floor,
                before=message,
                oldest_first=False,
            )
    except discord.HTTPException:
        return []

    recent.sort(
        key=lambda item: getattr(item, "created_at", cutoff),
    )
    return [_format_history_message(item, char_limit) for item in recent]


def format_memory_block(
    summary: str | None,
    window: list[tuple[str, str]],
) -> str:
    """Format summary and recent messages for direct prompt prefixing."""
    sections: list[str] = []
    if summary and summary.strip():
        summary = summary.strip()
        sections.append(
            "Channel memory (older context, attributed gist):\n"
            f"{summary}"
        )
    if window:
        lines = [f"[{author}]: {content}" for author, content in window]
        sections.append(
            "Recent channel conversation (oldest first):\n"
            + "\n".join(lines)
        )
    if not sections:
        return ""
    return "\n\n".join(sections) + "\n\n"


class ChannelMemoryStore:
    """Persist TTL-bounded rolling summaries keyed by channel ID."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._entries: dict[int, dict[str, str | float]] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._generation = 0
        self._channel_generations: dict[int, int] = {}
        self._load()

    def _load(self) -> None:
        try:
            with self.path.open(encoding="utf-8") as source:
                payload = json.load(source)
            if not isinstance(payload, dict):
                raise ValueError("channel memory root must be an object")
            for raw_id, raw_entry in payload.items():
                channel_id = int(raw_id)
                if not isinstance(raw_entry, dict):
                    raise ValueError("channel memory entry must be an object")
                summary = raw_entry["summary"]
                updated_at = float(raw_entry["updated_at"])
                if not isinstance(summary, str):
                    raise ValueError("channel memory summary must be text")
                if not math.isfinite(updated_at):
                    raise ValueError("channel memory timestamp must be finite")
                self._entries[channel_id] = {
                    "summary": summary.strip()[:_SUMMARY_HARD_MAX_CHARS],
                    "updated_at": updated_at,
                }
        except FileNotFoundError:
            logger.info("Channel memory file not found: %s", self.path)
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            self._entries.clear()
            logger.warning("Could not load channel memory %s: %s", self.path, exc)

    def _prune_expired(self) -> bool:
        cutoff = time.time() - SUMMARY_TTL_SECONDS
        expired = [
            channel_id
            for channel_id, entry in self._entries.items()
            if float(entry["updated_at"]) <= cutoff
        ]
        for channel_id in expired:
            self._entries.pop(channel_id, None)
        return bool(expired)

    def _save(self) -> None:
        self._prune_expired()
        payload = {
            str(channel_id): entry
            for channel_id, entry in self._entries.items()
        }
        atomic_json_dump(payload, self.path)

    def get(self, channel_id: int) -> str | None:
        """Return a live summary, pruning an expired entry on access."""
        if self._prune_expired():
            try:
                self._save()
            except OSError:
                logger.exception("Could not persist expired channel memory pruning")
        entry = self._entries.get(channel_id)
        if entry is None:
            return None
        return str(entry["summary"]) or None

    def generation(self, channel_id: int) -> tuple[int, int]:
        """Return the invalidation token for a channel's pending updates."""
        return self._generation, self._channel_generations.get(channel_id, 0)

    async def update(
        self,
        channel_id: int,
        *,
        speaker_name: str,
        prompt: str,
        reply: str,
    ) -> None:
        """Fold one successful exchange into the channel summary."""
        lock = self._locks.setdefault(channel_id, asyncio.Lock())
        generation = self.generation(channel_id)
        async with lock:
            previous = self._entries.get(channel_id)
            try:
                prior = self.get(channel_id) or ""
                summary = await summarize_exchange(
                    prior,
                    speaker_name,
                    prompt,
                    reply,
                )
                if generation != self.generation(channel_id):
                    return
                self._entries[channel_id] = {
                    "summary": (summary or "").strip()[:_SUMMARY_HARD_MAX_CHARS],
                    "updated_at": time.time(),
                }
                self._save()
            except Exception:
                if previous is None:
                    self._entries.pop(channel_id, None)
                else:
                    self._entries[channel_id] = previous
                logger.exception(
                    "Could not update channel memory for %s",
                    channel_id,
                )

    def clear(self, channel_id: int) -> None:
        """Remove and persist one channel summary."""
        self._channel_generations[channel_id] = (
            self._channel_generations.get(channel_id, 0) + 1
        )
        self._entries.pop(channel_id, None)
        try:
            self._save()
        except OSError:
            logger.exception("Could not persist channel memory clear for %s", channel_id)

    def clear_all(self) -> None:
        """Remove and persist every channel summary."""
        self._generation += 1
        self._entries.clear()
        try:
            self._save()
        except OSError:
            logger.exception("Could not persist clearing all channel memory")
