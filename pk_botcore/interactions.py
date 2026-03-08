"""Interaction logging for Discord bots.

Logs all bot interactions to JSON-lines files for audit and analysis.
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class InteractionEvent:
    """Base event for all interactions."""

    timestamp: str
    event_type: str
    bot_name: str
    channel_id: int
    channel_name: str | None = None
    guild_id: int | None = None
    guild_name: str | None = None
    user_id: int | None = None
    user_name: str | None = None
    is_bot: bool = False
    data: dict = field(default_factory=dict)


class InteractionLogger:
    """Logs bot interactions to JSON-lines file."""

    def __init__(self, bot_name: str, log_path: str | Path | None = None):
        """
        Initialize interaction logger.

        Args:
            bot_name: Name of the bot (e.g., "asha", "zalgo")
            log_path: Path to log file. Defaults to ~/.pk.{bot_name}/interactions.jsonl
        """
        self.bot_name = bot_name.lower()

        if log_path is None:
            log_dir = Path.home() / f".pk.{self.bot_name}"
            log_dir.mkdir(parents=True, exist_ok=True)
            self.log_path = log_dir / "interactions.jsonl"
        else:
            self.log_path = Path(log_path)
            self.log_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info("Interaction logger initialized: %s", self.log_path)

    def _write_event(self, event: InteractionEvent) -> None:
        """Write event to log file."""
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error("Failed to write interaction event: %s", e)

    def _now(self) -> str:
        """Get current timestamp in ISO format."""
        return datetime.now(timezone.utc).isoformat()

    def log_message_received(
        self,
        channel_id: int,
        user_id: int,
        user_name: str,
        content: str,
        is_bot: bool = False,
        channel_name: str | None = None,
        guild_id: int | None = None,
        guild_name: str | None = None,
        is_dm: bool = False,
        is_mention: bool = False,
        listen_mode: bool = False,
    ) -> None:
        """Log a message received by the bot."""
        event = InteractionEvent(
            timestamp=self._now(),
            event_type="message_received",
            bot_name=self.bot_name,
            channel_id=channel_id,
            channel_name=channel_name,
            guild_id=guild_id,
            guild_name=guild_name,
            user_id=user_id,
            user_name=user_name,
            is_bot=is_bot,
            data={
                "content": content[:500],  # Truncate for log size
                "content_length": len(content),
                "is_dm": is_dm,
                "is_mention": is_mention,
                "listen_mode": listen_mode,
            }
        )
        self._write_event(event)

    def log_relevance_check(
        self,
        channel_id: int,
        user_id: int,
        user_name: str,
        content: str,
        passed: bool,
        is_bot: bool = False,
        channel_name: str | None = None,
        guild_id: int | None = None,
        guild_name: str | None = None,
    ) -> None:
        """Log a relevance check result."""
        event = InteractionEvent(
            timestamp=self._now(),
            event_type="relevance_check",
            bot_name=self.bot_name,
            channel_id=channel_id,
            channel_name=channel_name,
            guild_id=guild_id,
            guild_name=guild_name,
            user_id=user_id,
            user_name=user_name,
            is_bot=is_bot,
            data={
                "content": content[:200],
                "passed": passed,
            }
        )
        self._write_event(event)

    def log_response_sent(
        self,
        channel_id: int,
        user_id: int,
        user_name: str,
        prompt: str,
        response: str,
        duration_ms: int,
        cost_usd: float = 0.0,
        is_error: bool = False,
        session_id: str | None = None,
        channel_name: str | None = None,
        guild_id: int | None = None,
        guild_name: str | None = None,
    ) -> None:
        """Log a response sent by the bot."""
        event = InteractionEvent(
            timestamp=self._now(),
            event_type="response_sent",
            bot_name=self.bot_name,
            channel_id=channel_id,
            channel_name=channel_name,
            guild_id=guild_id,
            guild_name=guild_name,
            user_id=user_id,
            user_name=user_name,
            data={
                "prompt": prompt[:500],
                "response": response[:500],
                "response_length": len(response),
                "duration_ms": duration_ms,
                "cost_usd": cost_usd,
                "is_error": is_error,
                "session_id": session_id,
            }
        )
        self._write_event(event)

    def log_command_executed(
        self,
        channel_id: int,
        user_id: int,
        user_name: str,
        command_name: str,
        args: str | None = None,
        success: bool = True,
        error: str | None = None,
        channel_name: str | None = None,
        guild_id: int | None = None,
        guild_name: str | None = None,
    ) -> None:
        """Log a command execution."""
        event = InteractionEvent(
            timestamp=self._now(),
            event_type="command_executed",
            bot_name=self.bot_name,
            channel_id=channel_id,
            channel_name=channel_name,
            guild_id=guild_id,
            guild_name=guild_name,
            user_id=user_id,
            user_name=user_name,
            data={
                "command": command_name,
                "args": args,
                "success": success,
                "error": error,
            }
        )
        self._write_event(event)

    def log_custom(
        self,
        event_type: str,
        channel_id: int,
        data: dict[str, Any],
        user_id: int | None = None,
        user_name: str | None = None,
        is_bot: bool = False,
        channel_name: str | None = None,
        guild_id: int | None = None,
        guild_name: str | None = None,
    ) -> None:
        """Log a custom event."""
        event = InteractionEvent(
            timestamp=self._now(),
            event_type=event_type,
            bot_name=self.bot_name,
            channel_id=channel_id,
            channel_name=channel_name,
            guild_id=guild_id,
            guild_name=guild_name,
            user_id=user_id,
            user_name=user_name,
            is_bot=is_bot,
            data=data,
        )
        self._write_event(event)
