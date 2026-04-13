"""pk.botcore - Shared infrastructure for PK Discord bots.

This package provides common utilities for pk.zalgo and pk.asha:
- Session management (UserSession, load/save)
- Channel configuration (listen modes)
- LLM invocation (Claude, Codex, unified interface)
- Message chunking for Discord
- HTTP utilities
- Dynamic command registry for LLM [CMD:*] directives
"""

from .sessions import UserSession, load_sessions, save_sessions
from .channels import (
    ChannelConfig,
    MODE_MENTION,
    MODE_LISTEN,
    MODE_IGNORE,
    LISTEN_MODES,
    load_channel_config,
    save_channel_config,
)
from .claude import (
    STATUS_THINKING,
    STATUS_TOOL,
)
from .llm import (
    LLMResponse,
    invoke_llm,
    check_relevance,
    check_bot_continuation,
)
from .chunking import chunk_message
from .http import get_http_session, close_http_session, fetch_json, get_image_data
from .embeds import make_embed
from .games import GamesCog
from .bot_app import (
    setup_logging,
    get_token,
    create_bot,
    load_extensions,
    sync_commands,
    register_common_events,
    register_killbot,
    run_bot,
)
from .cmd_executor import (
    CommandRegistry,
    llm_command,
    CmdResult,
    CMD_PATTERN,
    extract_commands,
    clean_response,
    process_response_async,
)
from .interactions import InteractionLogger
from .internal_admin import InternalAdminCog, get_actor_admin_capabilities
from .assistant_runtime import AssistantRuntimeMixin, ConversationTracker, ActiveTaskState

__version__ = "0.1.0"

__all__ = [
    # Sessions
    "UserSession",
    "load_sessions",
    "save_sessions",
    # Channels
    "ChannelConfig",
    "MODE_MENTION",
    "MODE_LISTEN",
    "MODE_IGNORE",
    "LISTEN_MODES",
    "load_channel_config",
    "save_channel_config",
    # LLM (unified interface)
    "LLMResponse",
    "invoke_llm",
    "check_relevance",
    "check_bot_continuation",
    "STATUS_THINKING",
    "STATUS_TOOL",
    # Chunking
    "chunk_message",
    # HTTP
    "get_http_session",
    "close_http_session",
    "fetch_json",
    "get_image_data",
    # Embeds
    "make_embed",
    # Games
    "GamesCog",
    # Bot app
    "setup_logging",
    "get_token",
    "create_bot",
    "load_extensions",
    "sync_commands",
    "register_common_events",
    "register_killbot",
    "run_bot",
    # Commands
    "CommandRegistry",
    "llm_command",
    "CmdResult",
    "CMD_PATTERN",
    "extract_commands",
    "clean_response",
    "process_response_async",
    # Interactions
    "InteractionLogger",
    # Assistant runtime
    "AssistantRuntimeMixin",
    "ConversationTracker",
    "ActiveTaskState",
    # Internal admin
    "InternalAdminCog",
    "get_actor_admin_capabilities",
]
