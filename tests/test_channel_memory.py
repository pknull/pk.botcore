"""Non-live tests for bounded guild-channel memory."""

import asyncio
from datetime import timedelta
import json
import logging
from types import SimpleNamespace

import discord
import pytest

from tests.async_utils import async_test
from pk_botcore import classifiers
from pk_botcore import memory
from pk_botcore.assistant_runtime import AssistantRuntimeMixin


def _author(name, *, bot=False):
    return SimpleNamespace(display_name=name, name=name, bot=bot)


def _message(content, created_at, name="User", *, bot=False):
    return SimpleNamespace(
        content=content,
        created_at=created_at,
        author=_author(name, bot=bot),
    )


class HistoryChannel:
    def __init__(self, messages, channel_id=123):
        self.id = channel_id
        self.messages = list(messages)

    def history(self, *, limit, before, after=None, oldest_first=False):
        selected = self.messages
        if after is not None:
            selected = [msg for msg in selected if msg.created_at >= after]
        selected = sorted(
            selected,
            key=lambda msg: msg.created_at,
            reverse=not oldest_first,
        )
        if limit is not None:
            selected = selected[:limit]

        async def generate():
            for msg in selected:
                yield msg

        return generate()


def _trigger(channel, *, guild=True):
    return SimpleNamespace(
        id=999,
        channel=channel,
        guild=SimpleNamespace(id=1) if guild else None,
    )


@async_test
async def test_window_filters_old_messages_and_orders_oldest_first():
    now = discord.utils.utcnow()
    messages = [
        _message("too old", now - timedelta(minutes=46), "Old"),
        _message("newer", now - timedelta(minutes=5), "Second"),
        _message("older", now - timedelta(minutes=20), "First"),
    ]

    window = await memory.build_memory_window(_trigger(HistoryChannel(messages)))

    assert window == [("First", "older"), ("Second", "newer")]


@async_test
async def test_window_caps_to_newest_25():
    now = discord.utils.utcnow()
    messages = [
        _message(str(index), now - timedelta(seconds=index), f"User{index}")
        for index in range(30)
    ]

    window = await memory.build_memory_window(_trigger(HistoryChannel(messages)))

    assert len(window) == 25
    assert window[0] == ("User24", "24")
    assert window[-1] == ("User0", "0")


@async_test
async def test_window_uses_three_message_floor_when_window_empty():
    now = discord.utils.utcnow()
    messages = [
        _message(str(index), now - timedelta(hours=index + 2), f"User{index}")
        for index in range(5)
    ]

    window = await memory.build_memory_window(_trigger(HistoryChannel(messages)))

    assert window == [
        ("User2", "2"),
        ("User1", "1"),
        ("User0", "0"),
    ]


@async_test
async def test_window_truncates_and_labels_bots():
    now = discord.utils.utcnow()
    messages = [
        _message("x" * 501, now - timedelta(minutes=2), "Asha", bot=True),
    ]

    window = await memory.build_memory_window(_trigger(HistoryChannel(messages)))

    assert window == [("Asha (bot)", "x" * 500)]


@pytest.mark.parametrize("history", [None, 3])
def test_window_missing_or_noncallable_history_returns_empty(history):
    channel = SimpleNamespace(id=123, history=history)
    result = asyncio.run(memory.build_memory_window(_trigger(channel)))
    assert result == []


@async_test
async def test_window_http_failure_returns_empty():
    class BrokenChannel:
        id = 123

        def history(self, **kwargs):
            response = SimpleNamespace(status=500, reason="failure")
            raise discord.HTTPException(response, "history failed")

    assert await memory.build_memory_window(_trigger(BrokenChannel())) == []


def _write_entries(path, entries):
    path.write_text(json.dumps(entries), encoding="utf-8")


def test_store_ttl_expiry_is_absent_and_pruned(tmp_path, monkeypatch):
    path = tmp_path / "channel_memory.json"
    monkeypatch.setattr(memory.time, "time", lambda: 100_000.0)
    _write_entries(path, {
        "12": {
            "summary": "stale",
            "updated_at": 100_000.0 - memory.SUMMARY_TTL_SECONDS,
        }
    })
    store = memory.ChannelMemoryStore(path)

    assert store.get(12) is None
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_store_ttl_prune_save_failure_does_not_raise(tmp_path, monkeypatch):
    path = tmp_path / "channel_memory.json"
    monkeypatch.setattr(memory.time, "time", lambda: 100_000.0)
    _write_entries(path, {
        "12": {
            "summary": "stale",
            "updated_at": 100_000.0 - memory.SUMMARY_TTL_SECONDS,
        }
    })
    store = memory.ChannelMemoryStore(path)
    monkeypatch.setattr(
        memory,
        "atomic_json_dump",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read only")),
    )

    assert store.get(12) is None


@async_test
async def test_store_persistence_round_trip(tmp_path, monkeypatch):
    path = tmp_path / "channel_memory.json"

    async def summarize(*args, **kwargs):
        return "new summary"

    monkeypatch.setattr(memory, "summarize_exchange", summarize)
    store = memory.ChannelMemoryStore(path)
    await store.update(12, speaker_name="User", prompt="hello", reply="hi")

    assert memory.ChannelMemoryStore(path).get(12) == "new summary"


def test_store_corrupt_file_is_empty(tmp_path):
    path = tmp_path / "channel_memory.json"
    path.write_text("{broken", encoding="utf-8")

    store = memory.ChannelMemoryStore(path)

    assert store.get(12) is None


def test_store_rejects_nonfinite_timestamp(tmp_path):
    path = tmp_path / "channel_memory.json"
    _write_entries(path, {
        "12": {"summary": "immortal", "updated_at": float("nan")},
    })

    assert memory.ChannelMemoryStore(path).get(12) is None


def test_store_hard_truncates_loaded_summary(tmp_path):
    path = tmp_path / "channel_memory.json"
    _write_entries(path, {
        "12": {"summary": "x" * 1500, "updated_at": memory.time.time()},
    })

    assert memory.ChannelMemoryStore(path).get(12) == "x" * 1200


def test_store_clear_and_clear_all(tmp_path):
    path = tmp_path / "channel_memory.json"
    _write_entries(path, {
        "1": {"summary": "one", "updated_at": memory.time.time()},
        "2": {"summary": "two", "updated_at": memory.time.time()},
    })
    store = memory.ChannelMemoryStore(path)

    store.clear(1)
    assert store.get(1) is None
    assert store.get(2) == "two"
    store.clear_all()
    assert store.get(2) is None
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_store_clear_failures_are_fail_open(tmp_path, monkeypatch):
    path = tmp_path / "channel_memory.json"
    _write_entries(path, {
        "1": {"summary": "one", "updated_at": memory.time.time()},
    })
    store = memory.ChannelMemoryStore(path)
    monkeypatch.setattr(
        memory,
        "atomic_json_dump",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    store.clear(1)
    store.clear_all()

    assert store.get(1) is None


@async_test
async def test_store_update_replaces_and_saves(tmp_path, monkeypatch):
    path = tmp_path / "channel_memory.json"
    captured = {}

    async def summarize(prior, speaker, prompt, reply):
        captured["args"] = (prior, speaker, prompt, reply)
        return "replacement"

    monkeypatch.setattr(memory, "summarize_exchange", summarize)
    store = memory.ChannelMemoryStore(path)
    await store.update(
        7,
        speaker_name="Velathra",
        prompt="question",
        reply="answer",
    )

    assert captured["args"] == ("", "Velathra", "question", "answer")
    assert store.get(7) == "replacement"
    assert json.loads(path.read_text(encoding="utf-8"))["7"]["summary"] == "replacement"


@async_test
async def test_store_update_error_keeps_prior_summary(tmp_path, monkeypatch):
    path = tmp_path / "channel_memory.json"
    _write_entries(path, {
        "7": {"summary": "prior", "updated_at": memory.time.time()},
    })

    async def broken(*args, **kwargs):
        raise RuntimeError("haiku unavailable")

    monkeypatch.setattr(memory, "summarize_exchange", broken)
    store = memory.ChannelMemoryStore(path)
    await store.update(7, speaker_name="User", prompt="q", reply="a")

    assert store.get(7) == "prior"


@async_test
async def test_store_save_error_keeps_prior_summary(tmp_path, monkeypatch):
    path = tmp_path / "channel_memory.json"
    _write_entries(path, {
        "7": {"summary": "prior", "updated_at": memory.time.time()},
    })

    async def summarize(*args, **kwargs):
        return "replacement"

    monkeypatch.setattr(memory, "summarize_exchange", summarize)
    store = memory.ChannelMemoryStore(path)
    monkeypatch.setattr(
        memory,
        "atomic_json_dump",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    await store.update(7, speaker_name="User", prompt="q", reply="a")

    assert store.get(7) == "prior"


@async_test
async def test_store_update_hard_truncates_to_1200(tmp_path, monkeypatch):
    async def summarize(*args, **kwargs):
        return "z" * 1500

    monkeypatch.setattr(memory, "summarize_exchange", summarize)
    store = memory.ChannelMemoryStore(tmp_path / "channel_memory.json")
    await store.update(7, speaker_name="User", prompt="q", reply="a")

    assert store.get(7) == "z" * 1200


@async_test
async def test_store_serializes_updates_per_channel(tmp_path, monkeypatch):
    priors = []

    async def summarize(prior, speaker, prompt, reply):
        priors.append(prior)
        await asyncio.sleep(0)
        return reply

    monkeypatch.setattr(memory, "summarize_exchange", summarize)
    store = memory.ChannelMemoryStore(tmp_path / "channel_memory.json")
    await asyncio.gather(
        store.update(7, speaker_name="One", prompt="q1", reply="first"),
        store.update(7, speaker_name="Two", prompt="q2", reply="second"),
    )

    assert priors == ["", "first"]
    assert store.get(7) == "second"


@async_test
async def test_store_clear_invalidates_inflight_update(tmp_path, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def summarize(*args, **kwargs):
        started.set()
        await release.wait()
        return "resurrected"

    monkeypatch.setattr(memory, "summarize_exchange", summarize)
    store = memory.ChannelMemoryStore(tmp_path / "channel_memory.json")
    task = asyncio.create_task(
        store.update(7, speaker_name="User", prompt="q", reply="a")
    )
    await started.wait()
    store.clear(7)
    release.set()
    await task

    assert store.get(7) is None


@pytest.mark.parametrize(
    ("summary", "window", "expected"),
    [
        (None, [], ""),
        ("   ", [], ""),
        (
            "gist",
            [],
            "Channel memory (older context, attributed gist):\ngist\n\n",
        ),
        (
            None,
            [("User", "hello")],
            "Recent channel conversation (oldest first):\n[User]: hello\n\n",
        ),
        (
            "gist",
            [("Asha (bot)", "answer")],
            "Channel memory (older context, attributed gist):\n"
            "gist\n\n"
            "Recent channel conversation (oldest first):\n"
            "[Asha (bot)]: answer\n\n",
        ),
    ],
)
def test_format_memory_block(summary, window, expected):
    assert memory.format_memory_block(summary, window) == expected


@async_test
async def test_summarize_exchange_uses_haiku_text(monkeypatch):
    captured = {}

    async def text(prompt, timeout):
        captured["prompt"] = prompt
        captured["timeout"] = timeout
        return "updated gist"

    monkeypatch.setattr(classifiers, "_haiku_text", text)
    result = await classifiers.summarize_exchange(
        "prior",
        "Velathra",
        "What is my DEX?",
        "55",
    )

    assert result == "updated gist"
    assert captured["timeout"] == 20
    assert "prior" in captured["prompt"]
    assert "Velathra" in captured["prompt"]
    assert "What is my DEX?" in captured["prompt"]
    assert "55" in captured["prompt"]
    assert "1000" in captured["prompt"]


class Runtime(AssistantRuntimeMixin):
    def __init__(self, memory_path=None):
        self._assistant_logger = logging.getLogger("test.channel_memory")
        self._init_assistant_runtime(memory_path=memory_path)


@async_test
async def test_runtime_memory_block_is_empty_for_dm(tmp_path):
    runtime = Runtime(tmp_path / "channel_memory.json")
    channel = SimpleNamespace(
        id=5,
        history=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("DM history must not be fetched")
        ),
    )

    assert await runtime._build_channel_memory_block(
        _trigger(channel, guild=False)
    ) == ""


def test_runtime_schedule_noops_for_dm_or_missing_store(tmp_path):
    no_store = Runtime()
    no_store._schedule_memory_update(
        5,
        speaker_name="User",
        prompt="q",
        reply="a",
    )
    runtime = Runtime(tmp_path / "channel_memory.json")
    runtime._schedule_memory_update(
        None,
        speaker_name="User",
        prompt="q",
        reply="a",
    )
    runtime._schedule_memory_update(
        5,
        speaker_name="User",
        prompt="q",
        reply="   ",
    )

    assert no_store._memory_tasks == set()
    assert runtime._memory_tasks == set()


@async_test
async def test_runtime_clear_invalidates_scheduled_update(tmp_path, monkeypatch):
    async def summarize(*args, **kwargs):
        return "resurrected"

    monkeypatch.setattr(memory, "summarize_exchange", summarize)
    runtime = Runtime(tmp_path / "channel_memory.json")
    runtime._schedule_memory_update(
        5,
        speaker_name="User",
        prompt="q",
        reply="a",
    )
    tasks = list(runtime._memory_tasks)
    runtime._channel_memory.clear(5)
    await asyncio.gather(*tasks)

    assert runtime._channel_memory.get(5) is None


@async_test
async def test_runtime_clear_invalidates_turn_before_it_schedules(
    tmp_path,
    monkeypatch,
):
    async def summarize(*args, **kwargs):
        return "resurrected"

    monkeypatch.setattr(memory, "summarize_exchange", summarize)
    runtime = Runtime(tmp_path / "channel_memory.json")
    runtime._start_channel_memory_turn(5)
    runtime._channel_memory.clear(5)
    runtime._schedule_memory_update(
        5,
        speaker_name="User",
        prompt="q",
        reply="a",
    )
    tasks = list(runtime._memory_tasks)
    await asyncio.gather(*tasks)

    assert runtime._channel_memory.get(5) is None


@async_test
async def test_runtime_task_failure_is_logged_not_raised(caplog):
    class BrokenStore:
        async def update(self, *args, **kwargs):
            raise RuntimeError("write failed")

    runtime = Runtime()
    runtime._channel_memory = BrokenStore()
    with caplog.at_level(logging.ERROR, logger="test.channel_memory"):
        runtime._schedule_memory_update(
            5,
            speaker_name="User",
            prompt="q",
            reply="a",
        )
        tasks = list(runtime._memory_tasks)
        await asyncio.gather(*tasks)

    assert "Channel memory update failed for 5" in caplog.text
    assert runtime._memory_tasks == set()


def test_package_exports_channel_memory_names():
    import pk_botcore

    assert pk_botcore.ChannelMemoryStore is memory.ChannelMemoryStore
    assert pk_botcore.build_memory_window is memory.build_memory_window
    assert pk_botcore.format_memory_block is memory.format_memory_block
    assert pk_botcore.summarize_exchange is classifiers.summarize_exchange
