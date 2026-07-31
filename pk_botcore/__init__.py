"""pk.botcore - Shared infrastructure for PK Discord bots.

This package provides common utilities for pk.zalgo and pk.asha:
- Bot bootstrap (create_bot, run_bot, load_extensions)
- Session management (UserSession, load/save)
- Channel configuration (listen modes)
- LLM invocation (Claude, Codex, unified interface)
- Message chunking for Discord
- HTTP utilities
- Dynamic command registry for LLM [CMD:*] directives
- Assistant runtime mixin for LLM-driven cogs
"""

from .sessions import UserSession, load_sessions, save_sessions
from .channels import (
    ChannelConfig,
    MODE_MENTION,
    MODE_SOCIAL,
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
)
from .classifiers import (
    check_relevance,
    check_bot_continuation,
    summarize_exchange,
)
from .chunking import chunk_message
from .http import get_http_session, close_http_session, fetch_json, get_image_data
from .embeds import make_embed
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
    CommandDirective,
    CMD_PATTERN,
    extract_commands,
    extract_command_directives,
    clean_response,
    process_response_async,
)
from .interactions import InteractionLogger
from .assistant_runtime import AssistantRuntimeMixin, ConversationTracker, ActiveTaskState
from .engagement import (
    AccessResult,
    EngagementDecision,
    EngagementPolicy,
    addressed_by_any_name,
    another_bot_mentioned,
    is_addressed_by_name,
    is_stop_phrase,
)
from .storage import atomic_json_dump
from .memory import (
    ChannelMemoryStore,
    SUMMARY_MAX_CHARS,
    SUMMARY_TTL_SECONDS,
    build_memory_window,
    format_memory_block,
)
from .peers import (
    linkify_peer_mentions,
    parse_peer_bots,
)

__version__ = "0.1.0"

__all__ = [
    # Sessions
    "UserSession",
    "load_sessions",
    "save_sessions",
    # Channels
    "ChannelConfig",
    "MODE_MENTION",
    "MODE_SOCIAL",
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
    "summarize_exchange",
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
    "CommandDirective",
    "CMD_PATTERN",
    "extract_commands",
    "extract_command_directives",
    "clean_response",
    "process_response_async",
    # Interactions
    "InteractionLogger",
    # Assistant runtime
    "AssistantRuntimeMixin",
    "ConversationTracker",
    "ActiveTaskState",
    # Engagement pipeline
    "AccessResult",
    "EngagementDecision",
    "EngagementPolicy",
    "addressed_by_any_name",
    "another_bot_mentioned",
    "is_addressed_by_name",
    "is_stop_phrase",
    # Channel memory
    "ChannelMemoryStore",
    "SUMMARY_MAX_CHARS",
    "SUMMARY_TTL_SECONDS",
    "build_memory_window",
    "format_memory_block",
    # Peer bots
    "linkify_peer_mentions",
    "parse_peer_bots",
    # Storage
    "atomic_json_dump",
]
