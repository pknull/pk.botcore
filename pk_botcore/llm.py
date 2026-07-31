"""Unified LLM interface - backend-agnostic invocation.

Dispatches to Claude or Codex based on backend parameter.
"""

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

from .claude import (
    ClaudeResponse,
    invoke_claude,
)
from .classifiers import (
    check_relevance as _check_relevance_unified,
    check_bot_continuation,
)
from .codex import (
    CodexResponse,
    invoke_codex,
)

logger = logging.getLogger('pk_botcore.llm')

Backend = Literal["claude", "codex"]


@dataclass
class LLMResponse:
    """Unified response from LLM invocation (Claude or Codex)."""
    result: str
    session_id: str | None = None
    duration_ms: int = 0
    is_error: bool = False
    cost_usd: float = 0.0
    sources: list[str] | None = None  # File paths accessed via tools

    @classmethod
    def from_claude(cls, resp: ClaudeResponse) -> "LLMResponse":
        return cls(
            result=resp.result,
            session_id=resp.session_id,
            duration_ms=resp.duration_ms,
            is_error=resp.is_error,
            cost_usd=resp.cost_usd,
            sources=resp.sources,
        )

    @classmethod
    def from_codex(cls, resp: CodexResponse) -> "LLMResponse":
        return cls(
            result=resp.result,
            session_id=resp.thread_id,  # Map thread_id to session_id
            duration_ms=resp.duration_ms,
            is_error=resp.is_error,
            cost_usd=0.0,  # Codex doesn't expose cost
        )


async def invoke_llm(
    prompt: str,
    *,
    backend: Backend = "claude",
    cwd: str,
    model: str | None = None,
    fallback_model: str | None = None,
    persona_text: str = "",
    speaker_context: str = "",
    attachment_context: str = "",
    command_docs: str = "",
    session_id: str | None = None,
    timeout: int = 120,
    allowed_tools: list[str] | None = None,  # Required for Claude, ignored for Codex
    sandbox_mode: str = "read-only",  # Codex only
    status_callback: Callable[[str], Awaitable[None]] | None = None,
    text_callback: Callable[[str], Awaitable[None]] | None = None,
) -> LLMResponse:
    """
    Invoke LLM using the specified backend.

    Args:
        prompt: User prompt to send
        backend: "claude" or "codex"
        cwd: Working directory
        model: Optional backend model override
        fallback_model: Optional backend fallback model override
        persona_text: Persona markdown to prepend
        speaker_context: Speaker identification context
        attachment_context: Attachment paths context
        command_docs: Auto-generated command documentation
        session_id: Optional session/thread ID for continuity
        timeout: Command timeout in seconds
        allowed_tools: List of allowed tools (Claude only)
        sandbox_mode: Sandbox mode (Codex only): "read-only", "workspace-write", "danger-full-access"
        status_callback: Optional async callback for status updates
        text_callback: Optional async callback for streaming text chunks

    Returns:
        LLMResponse with result and metadata
    """
    if backend == "codex":
        resp = await invoke_codex(
            prompt=prompt,
            cwd=cwd,
            model=model,
            persona_text=persona_text,
            speaker_context=speaker_context,
            attachment_context=attachment_context,
            command_docs=command_docs,
            thread_id=session_id,
            timeout=timeout,
            sandbox_mode=sandbox_mode,
            status_callback=status_callback,
            text_callback=text_callback,
        )
        return LLMResponse.from_codex(resp)
    else:
        # Claude requires allowed_tools - default to read-only if not specified
        tools = allowed_tools if allowed_tools is not None else ["Read", "Glob", "Grep"]
        resp = await invoke_claude(
            prompt=prompt,
            cwd=cwd,
            model=model,
            fallback_model=fallback_model,
            persona_text=persona_text,
            speaker_context=speaker_context,
            attachment_context=attachment_context,
            command_docs=command_docs,
            session_id=session_id,
            timeout=timeout,
            allowed_tools=tools,
            status_callback=status_callback,
            text_callback=text_callback,
        )
        return LLMResponse.from_claude(resp)


async def check_relevance(
    content: str,
    *,
    backend: Backend = "claude",
    bot_name: str = "Bot",
    topics_of_interest: list[str] | None = None,
    out_of_scope: list[str] | None = None,
    recent_context: list[tuple[str, str]] | None = None,
) -> bool:
    """
    Check if a message warrants a response from the bot.

    Deprecated dispatcher: forwards to the unified strict classifier in
    pk_botcore.classifiers. The `backend` parameter is ignored — classifier
    verdicts run on one backend for every bot so both bots agree.

    Returns:
        True if the message is relevant, False otherwise (and False on any
        classifier failure — fail closed).
    """
    if backend != "claude":
        logger.debug(
            "check_relevance backend=%r ignored; classifiers are unified", backend
        )
    return await _check_relevance_unified(
        content,
        bot_name=bot_name,
        topics_of_interest=topics_of_interest,
        out_of_scope=out_of_scope,
        recent_context=recent_context,
    )
