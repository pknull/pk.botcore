"""Claude Agent SDK invocation utilities."""

import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

logger = logging.getLogger('pk_botcore.claude')

# Status constants for lifecycle callbacks
STATUS_THINKING = "thinking"
STATUS_TOOL = "tool"


@dataclass
class ClaudeResponse:
    """Response from Claude invocation."""
    result: str
    session_id: str | None = None
    duration_ms: int = 0
    is_error: bool = False
    cost_usd: float = 0.0


def _log_tool_use(block) -> None:
    """Log tool usage with relevant arguments for audit trail."""
    name = block.name
    inp = block.input or {}

    if name in ("Read", "Write", "Edit"):
        path = inp.get("file_path", "?")
        logger.info("AUDIT [%s] %s", name, path)
    elif name == "Bash":
        cmd = inp.get("command", "?")
        if len(cmd) > 200:
            cmd = cmd[:200] + "..."
        logger.info("AUDIT [Bash] %s", cmd)
    elif name == "Glob":
        pattern = inp.get("pattern", "?")
        logger.info("AUDIT [Glob] %s", pattern)
    elif name == "Grep":
        pattern = inp.get("pattern", "?")
        path = inp.get("path", ".")
        logger.info("AUDIT [Grep] pattern=%s path=%s", pattern, path)
    elif name == "WebSearch":
        query = inp.get("query", "?")
        logger.info("AUDIT [WebSearch] %s", query)
    elif name == "Task":
        agent = inp.get("subagent_type", "?")
        desc = inp.get("description", "")[:50]
        logger.info("AUDIT [Task] agent=%s desc=%s", agent, desc)
    else:
        logger.info("AUDIT [%s] %s", name, str(inp)[:100])


async def invoke_claude(
    prompt: str,
    *,
    cwd: str,
    allowed_tools: list[str],
    persona_text: str = "",
    speaker_context: str = "",
    attachment_context: str = "",
    session_id: str | None = None,
    timeout: int = 120,
    permission_mode: str = "default",
    status_callback: Callable[[str], Awaitable[None]] | None = None,
    text_callback: Callable[[str], Awaitable[None]] | None = None,
) -> ClaudeResponse:
    """
    Invoke Claude using the Agent SDK.

    Args:
        prompt: User prompt to send to Claude
        cwd: Working directory for Claude
        allowed_tools: List of tools Claude can use
        persona_text: Persona markdown to prepend
        speaker_context: Speaker identification context
        attachment_context: Attachment paths context
        session_id: Optional session ID for continuity
        timeout: Command timeout in seconds
        permission_mode: Permission mode for tool execution
        status_callback: Optional async callback for status updates
        text_callback: Optional async callback for streaming text chunks

    Returns:
        ClaudeResponse with result and metadata
    """
    try:
        from claude_agent_sdk import (
            query,
            ClaudeAgentOptions,
            AssistantMessage,
            ResultMessage,
            TextBlock,
            ToolUseBlock,
            CLINotFoundError,
            ProcessError,
        )
    except ImportError:
        return ClaudeResponse(
            result="Claude Agent SDK not installed. Run: pip install claude-agent-sdk",
            is_error=True,
            duration_ms=0
        )

    # Build full prompt with persona and context
    if persona_text:
        full_prompt = f"{persona_text}\n\n---\n\n{speaker_context}{attachment_context}\n\nMessage:\n{prompt}"
    else:
        full_prompt = f"{speaker_context}{attachment_context}\n\nMessage:\n{prompt}"

    options = ClaudeAgentOptions(
        cwd=cwd,
        allowed_tools=allowed_tools,
        permission_mode=permission_mode,
        resume=session_id,
    )

    logger.debug("Invoking Claude (session: %s, tools: %s)", session_id or "new", allowed_tools)
    start_time = time.time()

    try:
        result_text = []
        new_session_id = None
        cost_usd = 0.0
        is_error = False
        current_status = None

        async for message in query(prompt=full_prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        result_text.append(block.text)
                        if text_callback:
                            await text_callback(block.text)
                        if status_callback and current_status == STATUS_TOOL:
                            current_status = STATUS_THINKING
                            await status_callback(STATUS_THINKING)
                    elif isinstance(block, ToolUseBlock):
                        _log_tool_use(block)
                        if status_callback and current_status != STATUS_TOOL:
                            current_status = STATUS_TOOL
                            await status_callback(STATUS_TOOL)

            elif isinstance(message, ResultMessage):
                new_session_id = message.session_id
                cost_usd = message.total_cost_usd or 0.0
                is_error = message.is_error

        duration_ms = int((time.time() - start_time) * 1000)
        final_text = "\n".join(result_text) if result_text else "No response"

        return ClaudeResponse(
            result=final_text,
            session_id=new_session_id or session_id,
            duration_ms=duration_ms,
            is_error=is_error,
            cost_usd=cost_usd
        )

    except CLINotFoundError:
        return ClaudeResponse(
            result="Claude CLI not found. Install with: pip install claude-agent-sdk",
            is_error=True,
            duration_ms=0
        )
    except ProcessError as e:
        logger.error("Claude process error: %s (exit code: %s)", e, e.exit_code)
        logger.error("ProcessError stderr: %r", e.stderr)
        logger.error("ProcessError stdout: %r", getattr(e, 'stdout', None))
        logger.error("ProcessError args: %r", e.args)
        error_msg = e.stderr if e.stderr else str(e)
        return ClaudeResponse(
            result=f"Error: {error_msg}",
            is_error=True,
            duration_ms=int((time.time() - start_time) * 1000)
        )
    except Exception as e:
        logger.exception("Unexpected error invoking Claude")
        return ClaudeResponse(
            result=f"Unexpected error: {e}",
            is_error=True,
            duration_ms=int((time.time() - start_time) * 1000)
        )


# Relevance check prompt template
RELEVANCE_PROMPT = """You are a relevance filter for an AI assistant named {bot_name}.
Determine if this message warrants a response from the assistant.
Respond ONLY with "yes" or "no".

Respond "yes" if the message:
- Is directed at an AI/bot/assistant
- Asks a question that an AI could help with
- Requests information, advice, or assistance
- Mentions the assistant by name or role

Respond "no" if the message:
- Is casual conversation between humans
- Is a reaction like "lol", "nice", "brb", emoji-only
- Is clearly not seeking AI assistance
- Is a simple greeting with no question

Message: {content}"""


async def check_message_relevance(content: str, bot_name: str = "Bot") -> bool:
    """
    Quick check if a message warrants a response from the bot.

    Uses Haiku via SDK for fast, cheap inference. Returns True if the message
    seems directed at the bot or is asking for help.

    Args:
        content: The message content to check
        bot_name: The bot's name for the prompt

    Returns:
        True if the message is relevant, False otherwise
    """
    if not content.strip():
        return False

    try:
        from claude_agent_sdk import (
            query,
            ClaudeAgentOptions,
            AssistantMessage,
            TextBlock,
        )
    except ImportError:
        return True  # Fail open if SDK not available

    prompt = RELEVANCE_PROMPT.format(bot_name=bot_name, content=content)

    options = ClaudeAgentOptions(
        model="haiku",
        allowed_tools=[],
        permission_mode="default",
    )

    try:
        result_text = ""
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        result_text += block.text

        is_relevant = result_text.strip().lower().startswith("yes")
        logger.debug("Relevance check: %s -> %s", content[:50], is_relevant)
        return is_relevant

    except Exception as e:
        logger.error("Relevance check failed: %s", e)
        return True  # Fail open
