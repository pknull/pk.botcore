"""OpenAI Codex SDK invocation utilities.

Parallel implementation to claude.py, using openai-codex-sdk instead.
Uses existing Codex OAuth credentials from ~/.codex/auth.json.
"""

import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

logger = logging.getLogger('pk_botcore.codex')

# Status constants for lifecycle callbacks (same as claude.py)
STATUS_THINKING = "thinking"
STATUS_TOOL = "tool"


@dataclass
class CodexResponse:
    """Response from Codex invocation."""
    result: str
    thread_id: str | None = None
    duration_ms: int = 0
    is_error: bool = False
    input_tokens: int = 0
    output_tokens: int = 0


def _log_item(item) -> None:
    """Log thread item for audit trail."""
    item_type = getattr(item, 'type', 'unknown')

    if item_type == "command_execution":
        cmd = getattr(item, 'command', '?')
        if len(cmd) > 200:
            cmd = cmd[:200] + "..."
        logger.info("AUDIT [Codex Command] %s", cmd)
    elif item_type == "file_change":
        changes = getattr(item, 'changes', [])
        for change in changes:
            path = getattr(change, 'path', '?')
            kind = getattr(change, 'kind', '?')
            logger.info("AUDIT [Codex File %s] %s", kind, path)
    elif item_type == "mcp_tool_call":
        server = getattr(item, 'server', '?')
        tool = getattr(item, 'tool', '?')
        logger.info("AUDIT [Codex MCP] %s/%s", server, tool)
    elif item_type == "web_search":
        query = getattr(item, 'query', '?')
        logger.info("AUDIT [Codex WebSearch] %s", query)
    elif item_type == "agent_message":
        pass  # Don't log agent messages, they're the response
    elif item_type == "reasoning":
        pass  # Don't log reasoning, it's internal
    else:
        logger.info("AUDIT [Codex %s]", item_type)


async def invoke_codex(
    prompt: str,
    *,
    cwd: str,
    model: str | None = None,
    persona_text: str = "",
    speaker_context: str = "",
    attachment_context: str = "",
    command_docs: str = "",
    thread_id: str | None = None,
    timeout: int = 120,
    sandbox_mode: str = "read-only",
    status_callback: Callable[[str], Awaitable[None]] | None = None,
    text_callback: Callable[[str], Awaitable[None]] | None = None,
) -> CodexResponse:
    """
    Invoke Codex using the openai-codex-sdk.

    Args:
        prompt: User prompt to send to Codex
        cwd: Working directory for Codex
        model: Optional Codex model override
        persona_text: Persona markdown to prepend
        speaker_context: Speaker identification context
        attachment_context: Attachment paths context
        command_docs: Auto-generated command documentation
        thread_id: Optional thread ID for continuity (resume conversation)
        timeout: Command timeout in seconds (not currently used by SDK)
        sandbox_mode: One of "read-only", "workspace-write", "danger-full-access"
        status_callback: Optional async callback for status updates
        text_callback: Optional async callback for streaming text chunks

    Returns:
        CodexResponse with result and metadata
    """
    try:
        from openai_codex_sdk import Codex
        from openai_codex_sdk.types import (
            ThreadOptions,
            ItemStartedEvent,
            ItemCompletedEvent,
            TurnCompletedEvent,
            TurnFailedEvent,
            ThreadStartedEvent,
            ThreadErrorEvent,
            AgentMessageItem,
        )
    except ImportError:
        return CodexResponse(
            result="OpenAI Codex SDK not installed. Run: pip install openai-codex-sdk",
            is_error=True,
            duration_ms=0
        )

    # Build full prompt with persona and context
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

    logger.debug("Invoking Codex (thread: %s, cwd: %s)", thread_id or "new", cwd)
    start_time = time.time()

    try:
        # Configure thread options. Read-only turns stay hard-locked, but
        # write-authorized turns must not inherit an approval_policy=never lock.
        approval_policy = "never" if sandbox_mode == "read-only" else "on-failure"
        thread_options = ThreadOptions(
            model=model,
            working_directory=cwd,
            sandbox_mode=sandbox_mode,
            approval_policy=approval_policy,
        )

        codex = Codex()

        # Resume existing thread or start new one
        if thread_id:
            try:
                thread = codex.resume_thread(thread_id, thread_options)
            except Exception as e:
                logger.warning("Failed to resume thread %s: %s, starting fresh", thread_id, e)
                thread = codex.start_thread(thread_options)
        else:
            thread = codex.start_thread(thread_options)

        result_text = []
        new_thread_id = None
        is_error = False
        input_tokens = 0
        output_tokens = 0
        turn_completed = False

        # Use streaming to get events
        # Note: SDK may raise CodexExecError at the end even if we got a valid response
        # due to stderr output like "Reading prompt from stdin..."
        streamed = await thread.run_streamed(full_prompt)

        try:
            async for event in streamed.events:
                event_type = getattr(event, 'type', None)

                if event_type == "thread.started":
                    if isinstance(event, ThreadStartedEvent):
                        new_thread_id = event.thread_id
                        logger.debug("Thread started: %s", new_thread_id)

                elif event_type == "item.started":
                    if isinstance(event, ItemStartedEvent):
                        item = event.item
                        item_type = getattr(item, 'type', None)
                        # Tool/command started
                        if item_type in ("command_execution", "file_change", "mcp_tool_call", "web_search"):
                            # A new tool call means any prior agent_messages were
                            # pre-tool narration; drop them. Whatever accumulates
                            # after the last tool call is the real answer.
                            result_text.clear()
                            _log_item(item)
                            if status_callback:
                                await status_callback(STATUS_TOOL)

                elif event_type == "item.completed":
                    if isinstance(event, ItemCompletedEvent):
                        item = event.item
                        item_type = getattr(item, 'type', None)

                        if item_type == "agent_message" and isinstance(item, AgentMessageItem):
                            # Accumulate — multiple agent_messages after the last
                            # tool call get joined so genuine multi-part answers
                            # are preserved. Intermediate narration is dropped by
                            # the result_text.clear() on the next tool start.
                            result_text.append(item.text)
                            # Do NOT fire text_callback here. Hold display for
                            # the cog-level placeholder rotation instead; the
                            # final answer is delivered by _complete_llm_turn.
                            if status_callback:
                                await status_callback(STATUS_THINKING)

                elif event_type == "turn.completed":
                    turn_completed = True
                    if isinstance(event, TurnCompletedEvent):
                        usage = event.usage
                        if usage:
                            input_tokens = usage.input_tokens
                            output_tokens = usage.output_tokens

                elif event_type == "turn.failed":
                    if isinstance(event, TurnFailedEvent):
                        is_error = True
                        error_msg = event.error.message if event.error else "Unknown error"
                        result_text.append(f"Error: {error_msg}")
                        break

                elif event_type == "error":
                    if isinstance(event, ThreadErrorEvent):
                        # MCP errors are warnings, not fatal - only log them
                        if "MCP client" in event.message:
                            logger.debug("MCP warning: %s", event.message)
                        else:
                            is_error = True
                            result_text.append(f"Error: {event.message}")

        except Exception as stream_error:
            # SDK may raise CodexExecError after streaming completes due to stderr output
            # If we already got a complete response, use it instead of failing
            if turn_completed and result_text:
                logger.debug("Ignoring post-stream error (got valid response): %s", stream_error)
            else:
                raise

        # Get thread ID if not set yet
        if not new_thread_id:
            new_thread_id = thread.id

        duration_ms = int((time.time() - start_time) * 1000)
        final_text = "\n".join(result_text) if result_text else "No response"

        return CodexResponse(
            result=final_text,
            thread_id=new_thread_id or thread_id,
            duration_ms=duration_ms,
            is_error=is_error,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    except Exception as e:
        logger.exception("Unexpected error invoking Codex")
        return CodexResponse(
            result=f"Unexpected error: {e}",
            is_error=True,
            duration_ms=int((time.time() - start_time) * 1000)
        )


# Relevance check using Codex (lightweight check)
RELEVANCE_PROMPT = """You are a relevance filter for an AI assistant named {bot_name}.
Determine if this message warrants a response from {bot_name}.
Respond ONLY with "yes" or "no".

Recent context:
{recent_context}

Respond "yes" if the message:
- Is directed at {bot_name} by name
- Asks a question that {bot_name} could help with
- Is a general request not addressed to anyone specific
- Is a short follow-up, confirmation, correction, or continuation of the recent context
{topics_section}
Respond "no" if the message:
- Is addressed to someone else by name (any name that is NOT {bot_name})
- Is casual conversation between humans
- Is a reaction like "lol", "nice", "brb", emoji-only
- Is a simple greeting with no question
{out_of_scope_section}
Message: {content}"""


async def check_message_relevance_codex(
    content: str,
    bot_name: str = "Bot",
    topics_of_interest: list[str] | None = None,
    out_of_scope: list[str] | None = None,
    recent_context: list[tuple[str, str]] | None = None,
) -> bool:
    """
    Quick check if a message warrants a response from the bot.

    Uses Codex for inference. Returns True if the message
    seems directed at the bot or is asking for help.

    Args:
        content: The message content to check
        bot_name: The bot's name for the prompt
        topics_of_interest: Optional list of topics the bot will engage with
        out_of_scope: Optional list of topics the bot should NOT respond to
        recent_context: Optional list of (author_name, content) tuples for context

    Returns:
        True if the message is relevant, False otherwise
    """
    if not content.strip():
        return False

    try:
        from openai_codex_sdk import Codex
        from openai_codex_sdk.types import ThreadOptions
    except ImportError:
        return True  # Fail open if SDK not available

    # Build prompt sections
    if topics_of_interest:
        topics_list = "\n".join(f"- Discusses {topic}" for topic in topics_of_interest)
        topics_section = f"\nAlso respond \"yes\" if the message:\n{topics_list}\n"
    else:
        topics_section = ""

    if out_of_scope:
        oos_list = "\n".join(f"- Is about {topic}" for topic in out_of_scope)
        out_of_scope_section = f"\nAlso respond \"no\" if the message:\n{oos_list}\n"
    else:
        out_of_scope_section = ""

    if recent_context:
        context_lines = [f"[{author}]: {msg}" for author, msg in recent_context]
        recent_context_text = "\n".join(context_lines)
    else:
        recent_context_text = "(no recent context)"

    prompt = RELEVANCE_PROMPT.format(
        bot_name=bot_name,
        content=content,
        recent_context=recent_context_text,
        topics_section=topics_section,
        out_of_scope_section=out_of_scope_section
    )

    try:
        from openai_codex_sdk.types import (
            ItemCompletedEvent,
            TurnCompletedEvent,
            AgentMessageItem,
        )

        codex = Codex()
        thread_options = ThreadOptions(sandbox_mode="read-only")
        thread = codex.start_thread(thread_options)

        # Use streaming (non-streaming run() has issues with stdin handling)
        result_text = []
        turn_completed = False
        streamed = await thread.run_streamed(prompt)

        try:
            async for event in streamed.events:
                event_type = getattr(event, 'type', None)

                if event_type == "item.completed":
                    if isinstance(event, ItemCompletedEvent):
                        item = event.item
                        if getattr(item, 'type', None) == "agent_message" and isinstance(item, AgentMessageItem):
                            result_text.append(item.text)

                elif event_type == "turn.completed":
                    turn_completed = True
                    break

        except Exception as stream_error:
            # SDK may raise error after streaming due to stderr noise
            if turn_completed and result_text:
                logger.debug("Ignoring post-stream error in relevance check: %s", stream_error)
            else:
                raise

        final_text = "".join(result_text).strip().lower()
        is_relevant = final_text.startswith("yes")
        logger.debug("Relevance check (Codex): %s -> %s", content[:50], is_relevant)
        return is_relevant

    except Exception as e:
        logger.error("Relevance check failed: %s", e)
        return True  # Fail open
