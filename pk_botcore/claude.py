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
    model: str | None = None,
    fallback_model: str | None = None,
    persona_text: str = "",
    speaker_context: str = "",
    attachment_context: str = "",
    command_docs: str = "",
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
        model: Optional Claude model override
        fallback_model: Optional Claude fallback model override
        persona_text: Persona markdown to prepend
        speaker_context: Speaker identification context
        attachment_context: Attachment paths context
        command_docs: Auto-generated command documentation from CommandRegistry
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

    # Build full prompt with persona, commands, and context
    sections = []
    if persona_text:
        sections.append(persona_text)
    if command_docs:
        sections.append(command_docs)

    preamble = "\n\n---\n\n".join(sections) if sections else ""
    context_parts = [s for s in [speaker_context, attachment_context] if s]
    context = "".join(context_parts)

    if preamble:
        full_prompt = f"{preamble}\n\n---\n\n{context}\n\nMessage:\n{prompt}"
    else:
        full_prompt = f"{context}\n\nMessage:\n{prompt}"

    options = ClaudeAgentOptions(
        cwd=cwd,
        allowed_tools=allowed_tools,
        permission_mode=permission_mode,
        resume=session_id,
        model=model,
        fallback_model=fallback_model,
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


# Relevance check prompt template - permissive (responds to general requests)
RELEVANCE_PROMPT = """You are a relevance filter for an AI assistant named {bot_name}.
Determine if this message warrants a response from {bot_name}.
Respond ONLY with "yes" or "no".
{context_section}
Respond "yes" if the message:
- Is directed at {bot_name} by name
- Asks a question that {bot_name} could help with
- Is a general request not addressed to anyone specific
- Is a follow-up or continuation of a conversation {bot_name} is participating in
{topics_section}
Respond "no" if the message:
- Is addressed to someone else by name (any name that is NOT {bot_name})
- Is casual conversation between humans that doesn't involve {bot_name}
- Is a reaction like "lol", "nice", "brb", emoji-only
- Is a simple greeting with no question
{out_of_scope_section}
Message: {content}"""

# Relevance check prompt template - strict (only responds to specific domain)
RELEVANCE_PROMPT_STRICT = """You are a relevance filter for an AI assistant named {bot_name}.
{bot_name} is a SPECIALIST that ONLY handles specific topics.
Determine if this message is within {bot_name}'s domain or is a continuation of an active conversation.
Respond ONLY with "yes" or "no".
{context_section}
{bot_name}'s domain (ONLY respond "yes" for these):
{scope_section}
Respond "yes" ONLY if the message:
- Is directed at {bot_name} by name, OR
- Clearly falls within {bot_name}'s domain listed above, OR
- Is a follow-up to a conversation {bot_name} is actively participating in (check recent context)

Respond "no" if the message:
- Is addressed to someone else by name
- Is about ANY topic not in {bot_name}'s domain AND not a conversation follow-up
- Is a general utility request (weather, reminders, web search, etc.)
- Is casual conversation, reactions, or greetings

Message: {content}"""


async def check_message_relevance(
    content: str,
    bot_name: str = "Bot",
    topics_of_interest: list[str] | None = None,
    out_of_scope: list[str] | None = None,
    strict: bool = False,
    recent_context: list[tuple[str, str]] | None = None,
) -> bool:
    """
    Quick check if a message warrants a response from the bot.

    Uses Haiku via SDK for fast, cheap inference. Returns True if the message
    seems directed at the bot or is asking for help.

    Args:
        content: The message content to check
        bot_name: The bot's name for the prompt
        topics_of_interest: Optional list of topics the bot will engage with
            even if not directly addressed
        out_of_scope: Optional list of topics the bot should NOT respond to,
            even if the request seems general (for permissive mode)
        strict: If True, ONLY respond to direct address or topics in scope.
            General requests outside the domain get "no". Use for specialist bots.
        recent_context: Optional list of (author_name, content) tuples for
            recent messages to provide conversation context.

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

    # Build context section from recent messages
    if recent_context:
        context_lines = [f"[{author}]: {msg}" for author, msg in recent_context]
        context_section = "\nRecent conversation context:\n" + "\n".join(context_lines) + "\n"
    else:
        context_section = ""

    if strict:
        # Strict mode: only respond to direct address or explicit domain match
        if topics_of_interest:
            scope_list = "\n".join(f"- {topic}" for topic in topics_of_interest)
            scope_section = scope_list
        else:
            scope_section = "- (no specific domain defined)"

        prompt = RELEVANCE_PROMPT_STRICT.format(
            bot_name=bot_name,
            content=content,
            scope_section=scope_section,
            context_section=context_section,
        )
    else:
        # Permissive mode: respond to general requests too
        if topics_of_interest:
            topics_list = "\n".join(f"- Discusses {topic}" for topic in topics_of_interest)
            topics_section = f"\nAlso respond \"yes\" if the message:\n{topics_list}\n"
        else:
            topics_section = ""

        # Build out-of-scope section for permissive mode
        if out_of_scope:
            oos_list = "\n".join(f"- Is about {topic}" for topic in out_of_scope)
            out_of_scope_section = f"\nAlso respond \"no\" if the message:\n{oos_list}\n"
        else:
            out_of_scope_section = ""

        prompt = RELEVANCE_PROMPT.format(
            bot_name=bot_name,
            content=content,
            topics_section=topics_section,
            out_of_scope_section=out_of_scope_section,
            context_section=context_section,
        )

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


BOT_CONTINUATION_PROMPT = """You are {bot_name}, deciding whether to continue a bot-to-bot conversation.

Recent conversation:
{context}

The other bot ({other_bot}) just said: {message}

Respond "yes" if ANY of these are true:
- A human spoke recently (they're orchestrating or interested)
- The other bot asked you a question or made a direct request
- There's new information to engage with
- The exchange is productive and interesting

Respond "no" ONLY if:
- You're clearly going in circles (same points repeated 2+ times)
- The conversation has run its natural course with no new threads
- No humans have participated in the last several messages AND it feels stale

Respond ONLY with "yes" or "no"."""


async def check_bot_continuation(
    bot_name: str,
    other_bot: str,
    message: str,
    recent_context: list[tuple[str, str]],
) -> bool:
    """
    Check if bot should continue a bot-to-bot conversation.

    Uses Haiku for quick decision. Returns True to continue, False to disengage.
    """
    try:
        from claude_agent_sdk import (
            query,
            ClaudeAgentOptions,
            AssistantMessage,
            TextBlock,
        )
    except ImportError:
        return True  # Fail open

    context_lines = [f"[{author}]: {msg}" for author, msg in recent_context]
    context = "\n".join(context_lines) if context_lines else "(no recent context)"

    prompt = BOT_CONTINUATION_PROMPT.format(
        bot_name=bot_name,
        other_bot=other_bot,
        message=message,
        context=context,
    )

    options = ClaudeAgentOptions(
        model="haiku",
        allowed_tools=[],
        permission_mode="default",
    )

    try:
        result_text = ""
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        result_text += block.text

        should_continue = result_text.strip().lower().startswith("yes")
        logger.debug("Bot continuation check: %s -> %s", message[:50], should_continue)
        return should_continue

    except Exception as e:
        logger.error("Bot continuation check failed: %s", e)
        return True  # Fail open
