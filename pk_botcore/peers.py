"""Peer-bot mention configuration and outbound mention helpers."""

import logging
import re


logger = logging.getLogger(__name__)


def parse_peer_bots(raw: str | None) -> dict[str, int]:
    """Parse comma-separated ``alias:discord_user_id`` peer entries."""
    peers: dict[str, int] = {}
    if not raw:
        return peers

    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            alias, raw_id = entry.split(":", 1)
            alias = alias.strip().lower()
            if not alias:
                raise ValueError("empty alias")
            peers[alias] = int(raw_id.strip())
        except (TypeError, ValueError):
            logger.warning("Skipping malformed PEER_BOTS entry: %r", entry)
    return peers


def linkify_peer_mentions(text: str, peer_map: dict[str, int]) -> str:
    """Convert explicit configured ``@alias`` references to Discord tokens."""
    if not text or not peer_map:
        return text

    aliases = sorted(peer_map, key=len, reverse=True)
    alias_pattern = "|".join(re.escape(alias) for alias in aliases)
    pattern = re.compile(
        rf"(?<![\w<])@(?P<alias>{alias_pattern})(?!\w)",
        re.IGNORECASE,
    )
    return pattern.sub(
        lambda match: f"<@{peer_map[match.group('alias').lower()]}>",
        text,
    )
