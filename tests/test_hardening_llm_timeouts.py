import asyncio
import sys
from types import ModuleType, SimpleNamespace

import pytest

from tests.async_utils import async_test
from pk_botcore import claude, codex
from pk_botcore.limits import llm_slot


def install_fake_claude(monkeypatch, query):
    sdk = ModuleType("claude_agent_sdk")

    class Options:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class AssistantMessage:
        def __init__(self, content):
            self.content = content

    class ResultMessage:
        def __init__(self, session_id, is_error=False):
            self.session_id = session_id
            self.is_error = is_error
            self.total_cost_usd = 0

    class TextBlock:
        def __init__(self, text):
            self.text = text

    class ToolUseBlock:
        pass

    class CLINotFoundError(Exception):
        pass

    class ProcessError(Exception):
        exit_code = 1
        stderr = ""

    sdk.query = query
    sdk.ClaudeAgentOptions = Options
    sdk.AssistantMessage = AssistantMessage
    sdk.ResultMessage = ResultMessage
    sdk.TextBlock = TextBlock
    sdk.ToolUseBlock = ToolUseBlock
    sdk.CLINotFoundError = CLINotFoundError
    sdk.ProcessError = ProcessError
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    return sdk


@async_test
async def test_claude_stream_timeout_cancels_generator(monkeypatch):
    cancelled = asyncio.Event()

    async def query(**kwargs):
        try:
            await asyncio.sleep(10)
            yield None
        finally:
            cancelled.set()

    install_fake_claude(monkeypatch, query)
    response = await claude.invoke_claude(
        "prompt",
        cwd=".",
        allowed_tools=[],
        timeout=0.01,
    )
    assert response.is_error
    assert "timed out" in response.result
    assert cancelled.is_set()


@async_test
async def test_claude_stale_session_error_behavior_is_preserved(monkeypatch):
    holder = {}

    async def query(**kwargs):
        yield holder["sdk"].ResultMessage("stale-session", is_error=True)

    holder["sdk"] = install_fake_claude(monkeypatch, query)
    response = await claude.invoke_claude(
        "prompt",
        cwd=".",
        allowed_tools=[],
        session_id="stale-session",
    )
    assert response.is_error is True
    assert response.session_id == "stale-session"


def install_fake_codex(monkeypatch, events):
    sdk = ModuleType("openai_codex_sdk")
    types = ModuleType("openai_codex_sdk.types")

    class ThreadOptions:
        def __init__(self, **kwargs):
            pass

    class Event:
        pass

    class Thread:
        id = "thread"

        async def run_streamed(self, prompt):
            return SimpleNamespace(events=events())

    class Codex:
        def start_thread(self, options):
            return Thread()

        def resume_thread(self, thread_id, options):
            return Thread()

    sdk.Codex = Codex
    for name in (
        "ItemStartedEvent",
        "ItemCompletedEvent",
        "TurnCompletedEvent",
        "TurnFailedEvent",
        "ThreadStartedEvent",
        "ThreadErrorEvent",
        "AgentMessageItem",
    ):
        setattr(types, name, type(name, (Event,), {}))
    types.ThreadOptions = ThreadOptions
    monkeypatch.setitem(sys.modules, "openai_codex_sdk", sdk)
    monkeypatch.setitem(sys.modules, "openai_codex_sdk.types", types)


@async_test
async def test_codex_stream_timeout_cancels_generator(monkeypatch):
    cancelled = asyncio.Event()

    async def events():
        try:
            await asyncio.sleep(10)
            yield None
        finally:
            cancelled.set()

    install_fake_codex(monkeypatch, events)
    response = await codex.invoke_codex("prompt", cwd=".", timeout=0.01)
    assert response.is_error
    assert "timed out" in response.result
    assert cancelled.is_set()


@async_test
async def test_codex_relevance_helper_has_deadline(monkeypatch):
    cancelled = asyncio.Event()

    async def events():
        try:
            await asyncio.sleep(10)
            yield None
        finally:
            cancelled.set()

    install_fake_codex(monkeypatch, events)
    assert (
        await codex.check_message_relevance_codex("question", timeout=0.01)
        is True
    )
    assert cancelled.is_set()


@async_test
async def test_relevance_and_continuation_helpers_have_deadlines(monkeypatch):
    cancelled_count = 0

    async def query(**kwargs):
        nonlocal cancelled_count
        try:
            await asyncio.sleep(10)
            yield None
        finally:
            cancelled_count += 1

    install_fake_claude(monkeypatch, query)
    assert await claude.check_message_relevance("question", timeout=0.01) is True
    assert (
        await claude.check_bot_continuation(
            "A", "B", "question", [], timeout=0.01
        )
        is True
    )
    assert cancelled_count == 2


@async_test
async def test_global_llm_semaphore_is_environment_configurable(monkeypatch):
    monkeypatch.setenv("PK_BOTCORE_LLM_CONCURRENCY", "1")
    active = 0
    maximum = 0

    async def worker():
        nonlocal active, maximum
        async with llm_slot():
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(worker(), worker(), worker())
    assert maximum == 1
