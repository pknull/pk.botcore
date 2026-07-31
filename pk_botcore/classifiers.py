"""Harness-level yes/no classifiers for engagement decisions.

Every bot runs these on the same backend (Claude Haiku) regardless of its
generation backend, so relevance and continuation verdicts are identical
across the fleet. All classifiers FAIL CLOSED: any SDK, timeout, or transport
failure means "stay silent" rather than "speak".
"""

from __future__ import annotations

import asyncio
import logging
from typing import Sequence

from .limits import llm_slot

logger = logging.getLogger("pk_botcore.classifiers")

DEFAULT_CLASSIFIER_TIMEOUT = 15

# Strict-domain relevance filter: the bot only interjects on listed topics.
RELEVANCE_PROMPT = """You are a relevance filter for an AI assistant named {bot_name}.
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
{out_of_scope_section}
Message: {content}"""

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


async def _haiku_text(prompt: str, timeout: int) -> str:
    """Run a text-only Haiku request. Raises on any failure."""
    from claude_agent_sdk import (
        query,
        ClaudeAgentOptions,
        AssistantMessage,
        TextBlock,
    )

    options = ClaudeAgentOptions(
        model="haiku",
        tools=[],
        allowed_tools=[],
        permission_mode="default",
    )
    result_parts: list[str] = []

    async def consume_stream() -> None:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        result_parts.append(block.text)

    async with asyncio.timeout(timeout):
        async with llm_slot():
            await consume_stream()

    return "".join(result_parts)


async def _haiku_yes_no(prompt: str, timeout: int) -> bool:
    """Run a yes/no classification on Haiku. Raises on any failure."""
    result = await _haiku_text(prompt, timeout)
    return result.strip().lower().startswith("yes")


async def summarize_exchange(
    prior_summary: str,
    speaker_name: str,
    prompt: str,
    reply: str,
    timeout: int = 20,
) -> str:
    """Fold one attributed exchange into a bounded rolling summary."""
    request = f"""Fold this exchange into the running channel summary.
Keep attribution by speaker name. Keep the result at or below 1000 characters.
Drop the least-relevant older material first when space is needed.
Return only the updated summary.

Running summary:
{prior_summary or "(empty)"}

Speaker: {speaker_name}
Speaker message:
{prompt}

Assistant response:
{reply}"""
    return await _haiku_text(request, timeout)


def _format_context_section(
    recent_context: Sequence[tuple[str, str]] | None,
) -> str:
    if not recent_context:
        return ""
    lines = [f"[{author}]: {msg}" for author, msg in recent_context]
    return "\nRecent conversation context:\n" + "\n".join(lines) + "\n"


async def check_relevance(
    content: str,
    *,
    bot_name: str = "Bot",
    topics_of_interest: Sequence[str] | None = None,
    out_of_scope: Sequence[str] | None = None,
    recent_context: Sequence[tuple[str, str]] | None = None,
    timeout: int = DEFAULT_CLASSIFIER_TIMEOUT,
) -> bool:
    """Strict-domain check whether an unaddressed message warrants a reply.

    Fails closed: empty content, SDK unavailability, timeouts, and transport
    errors all return False (stay silent).
    """
    if not content.strip():
        return False

    if topics_of_interest:
        scope_section = "\n".join(f"- {topic}" for topic in topics_of_interest)
    else:
        scope_section = "- (no specific domain defined)"

    if out_of_scope:
        oos_list = "\n".join(f"- {topic}" for topic in out_of_scope)
        out_of_scope_section = (
            '\nThese topics belong to a DIFFERENT assistant - always respond "no" for them:\n'
            f"{oos_list}\n"
        )
    else:
        out_of_scope_section = ""

    prompt = RELEVANCE_PROMPT.format(
        bot_name=bot_name,
        content=content,
        scope_section=scope_section,
        out_of_scope_section=out_of_scope_section,
        context_section=_format_context_section(recent_context),
    )

    try:
        is_relevant = await _haiku_yes_no(prompt, timeout)
    except Exception as exc:
        logger.error("Relevance check failed (staying silent): %s", exc)
        return False

    logger.debug("Relevance check: %s -> %s", content[:50], is_relevant)
    return is_relevant


async def check_bot_continuation(
    bot_name: str,
    other_bot: str,
    message: str,
    recent_context: Sequence[tuple[str, str]],
    timeout: int = DEFAULT_CLASSIFIER_TIMEOUT,
) -> bool:
    """Decide whether to continue a bot-to-bot conversation.

    Fails closed: on any error the bot disengages (dropping one exchange is
    cheaper than risking a bot-to-bot loop).
    """
    context_lines = [f"[{author}]: {msg}" for author, msg in recent_context]
    context = "\n".join(context_lines) if context_lines else "(no recent context)"

    prompt = BOT_CONTINUATION_PROMPT.format(
        bot_name=bot_name,
        other_bot=other_bot,
        message=message,
        context=context,
    )

    try:
        should_continue = await _haiku_yes_no(prompt, timeout)
    except Exception as exc:
        logger.error("Bot continuation check failed (disengaging): %s", exc)
        return False

    logger.debug("Bot continuation check: %s -> %s", message[:50], should_continue)
    return should_continue
