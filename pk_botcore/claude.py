"""Claude Agent SDK invocation utilities."""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from .limits import llm_slot

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
    sources: list[str] | None = None  # File paths accessed via tools


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


def _collect_source_path(block, sources: list[str]) -> None:
    """Extract file paths from tool-use blocks for source citation."""
    name = block.name
    inp = block.input or {}

    if name in ("Read", "Edit", "Write"):
        path = inp.get("file_path")
        if path:
            sources.append(path)
    elif name == "Grep":
        path = inp.get("path")
        if path:
            sources.append(path)
    elif name == "Glob":
        path = inp.get("path")
        if path:
            sources.append(path)


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
        source_paths: list[str] = []

        async def consume_stream() -> None:
            nonlocal new_session_id, cost_usd, is_error, current_status
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
                            _collect_source_path(block, source_paths)
                            if status_callback and current_status != STATUS_TOOL:
                                current_status = STATUS_TOOL
                                await status_callback(STATUS_TOOL)

                elif isinstance(message, ResultMessage):
                    new_session_id = message.session_id
                    cost_usd = message.total_cost_usd or 0.0
                    is_error = message.is_error

        async with asyncio.timeout(timeout):
            async with llm_slot():
                await consume_stream()

        duration_ms = int((time.time() - start_time) * 1000)
        final_text = "\n".join(result_text) if result_text else "No response"

        # Deduplicate sources preserving order
        seen = set()
        unique_sources = []
        for p in source_paths:
            if p not in seen:
                seen.add(p)
                unique_sources.append(p)

        return ClaudeResponse(
            result=final_text,
            session_id=new_session_id or session_id,
            duration_ms=duration_ms,
            is_error=is_error,
            cost_usd=cost_usd,
            sources=unique_sources or None,
        )

    except TimeoutError:
        logger.error("Claude invocation timed out after %s seconds", timeout)
        return ClaudeResponse(
            result=f"Request timed out after {timeout} seconds",
            session_id=session_id,
            is_error=True,
            duration_ms=int((time.time() - start_time) * 1000),
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
