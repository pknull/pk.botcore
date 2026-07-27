"""Channel configuration for Discord bot listen modes."""

import json
import logging
from dataclasses import dataclass

from .storage import atomic_json_dump

logger = logging.getLogger('pk_botcore.channels')

# Channel listen modes
MODE_MENTION = "mention"
MODE_LISTEN = "listen"
MODE_IGNORE = "ignore"
LISTEN_MODES = (MODE_MENTION, MODE_LISTEN, MODE_IGNORE)


@dataclass
class ChannelConfig:
    """Channel listen mode configuration."""
    channel_id: int
    mode: str  # "mention", "listen", "ignore"
    set_by: int  # User ID who configured
    set_at: str  # ISO timestamp


def load_channel_config(config_file: str) -> dict[int, ChannelConfig]:
    """Load channel configurations from JSON file.

    Args:
        config_file: Path to the channel config JSON file

    Returns:
        Dictionary mapping channel_id to ChannelConfig
    """
    try:
        with open(config_file, 'r') as fp:
            data = json.load(fp)

        configs = {}
        for channel_id_str, config_data in data.items():
            mode = config_data.get("mode", MODE_MENTION)
            if mode not in LISTEN_MODES:
                logger.warning("Invalid mode '%s' for channel %s, defaulting to mention",
                              mode, channel_id_str)
                mode = MODE_MENTION

            configs[int(channel_id_str)] = ChannelConfig(
                channel_id=int(channel_id_str),
                mode=mode,
                set_by=config_data.get("set_by", 0),
                set_at=config_data.get("set_at", "")
            )

        logger.info("Loaded %d channel configs from %s", len(configs), config_file)
        return configs

    except FileNotFoundError:
        logger.info("No channel config file at %s, using defaults", config_file)
        return {}
    except Exception as e:
        logger.error("Error loading channel config from %s: %s", config_file, e)
        return {}


def save_channel_config(configs: dict[int, ChannelConfig], config_file: str) -> None:
    """Save channel configurations to JSON file.

    Args:
        configs: Dictionary mapping channel_id to ChannelConfig
        config_file: Path to the channel config JSON file
    """
    data = {}
    for channel_id, config in configs.items():
        data[str(channel_id)] = {
            "channel_id": config.channel_id,
            "mode": config.mode,
            "set_by": config.set_by,
            "set_at": config.set_at
        }

    atomic_json_dump(data, config_file, indent=2)

    logger.debug("Saved %d channel configs to %s", len(configs), config_file)
