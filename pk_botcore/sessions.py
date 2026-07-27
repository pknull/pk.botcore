"""Session management for Discord bot users."""

import logging
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .storage import atomic_json_dump

logger = logging.getLogger('pk_botcore.sessions')


def _now_iso() -> str:
    """Return the existing naive UTC timestamp format without deprecated APIs."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


@dataclass
class UserSession:
    """User session state."""
    user_id: int
    session_id: str
    created_at: str = field(default_factory=_now_iso)
    last_used: str = field(default_factory=_now_iso)
    message_count: int = 0


def load_sessions(sessions_file: str) -> dict[int, UserSession]:
    """Load user sessions from JSON file.

    Args:
        sessions_file: Path to the sessions JSON file

    Returns:
        Dictionary mapping user_id to UserSession
    """
    try:
        with open(sessions_file, 'r') as fp:
            data = json.load(fp)

        sessions = {}
        for user_id_str, session_data in data.items():
            fallback_timestamp = _now_iso()
            sessions[int(user_id_str)] = UserSession(
                user_id=int(user_id_str),
                session_id=session_data["session_id"],
                created_at=session_data.get("created_at", fallback_timestamp),
                last_used=session_data.get("last_used", fallback_timestamp),
                message_count=session_data.get("message_count", 0)
            )

        logger.info("Loaded %d sessions from %s", len(sessions), sessions_file)
        return sessions

    except FileNotFoundError:
        logger.info("No existing sessions file at %s, starting fresh", sessions_file)
        return {}
    except Exception as e:
        logger.error("Error loading sessions from %s: %s", sessions_file, e)
        return {}


def save_sessions(sessions: dict[int, UserSession], sessions_file: str) -> None:
    """Save user sessions to JSON file.

    Args:
        sessions: Dictionary mapping user_id to UserSession
        sessions_file: Path to the sessions JSON file
    """
    data = {}
    for user_id, session in sessions.items():
        data[str(user_id)] = {
            "session_id": session.session_id,
            "created_at": session.created_at,
            "last_used": session.last_used,
            "message_count": session.message_count
        }

    atomic_json_dump(data, sessions_file, indent=2)

    logger.debug("Saved %d sessions to %s", len(sessions), sessions_file)
