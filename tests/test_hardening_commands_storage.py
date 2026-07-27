import json
import os

import pytest

from tests.async_utils import async_test
from pk_botcore.channels import ChannelConfig, load_channel_config, save_channel_config
from pk_botcore.cmd_executor import (
    CmdResult,
    CommandRegistry,
    clean_response,
    extract_command_directives,
    extract_commands,
    process_response_async,
)
from pk_botcore.sessions import UserSession, load_sessions, save_sessions
from pk_botcore.storage import atomic_json_dump


@async_test
async def test_structured_command_preserves_iso_timestamp_and_colons_end_to_end():
    seen = []

    async def handler(ctx, *args):
        seen.append((ctx, args))
        return CmdResult(success=True, message="scheduled")

    registry = CommandRegistry(bot=None)
    registry._commands["schedule"] = handler
    response = (
        'Before [CMDJSON:{"action":"schedule","args":'
        '["2026-07-27T12:30:00-07:00","notes:with:colons"]}] after'
    )

    cleaned, results = await process_response_async(response, registry, "context")

    assert cleaned == "Before  after"
    assert results == [CmdResult(success=True, message="scheduled")]
    assert seen == [
        (
            "context",
            ("2026-07-27T12:30:00-07:00", "notes:with:colons"),
        )
    ]


@async_test
async def test_legacy_command_format_remains_unchanged():
    seen = []

    async def handler(ctx, *args):
        seen.append(args)
        return CmdResult(success=True)

    registry = CommandRegistry(bot=None)
    registry._commands["roll"] = handler
    text = "Roll [CMD:roll:42:hard] now"

    assert extract_commands(text) == ["roll:42:hard"]
    assert clean_response(text) == "Roll  now"
    cleaned, results = await process_response_async(text, registry, None)
    assert cleaned == "Roll  now"
    assert results[0].success
    assert seen == [("42", "hard")]


def test_invalid_structured_command_is_not_executed_or_hidden():
    text = (
        'Keep [CMDJSON:{"action":"x","args":"literal [CMD:reload]"}] visible'
    )
    assert extract_command_directives(text) == []
    assert clean_response(text) == text


@async_test
async def test_legacy_syntax_inside_structured_argument_is_not_executed():
    seen = []

    async def handler(ctx, *args):
        seen.append(args)
        return CmdResult(success=True)

    registry = CommandRegistry(bot=None)
    registry._commands["echo"] = handler
    registry._commands["reload"] = handler
    text = (
        'before [CMDJSON:{"action":"echo","args":["literal [CMD:reload]"]}] after'
    )

    cleaned, results = await process_response_async(text, registry, None)

    assert seen == [("literal [CMD:reload]",)]
    assert len(results) == 1
    assert cleaned == "before  after"


def test_sessions_and_channels_keep_existing_json_schema(tmp_path):
    sessions_path = tmp_path / "nested" / "sessions.json"
    channels_path = tmp_path / "nested" / "channels.json"
    session = UserSession(user_id=7, session_id="abc", created_at="c", last_used="l", message_count=2)
    config = ChannelConfig(channel_id=9, mode="listen", set_by=7, set_at="when")

    save_sessions({7: session}, str(sessions_path))
    save_channel_config({9: config}, str(channels_path))

    assert json.loads(sessions_path.read_text()) == {
        "7": {
            "session_id": "abc",
            "created_at": "c",
            "last_used": "l",
            "message_count": 2,
        }
    }
    assert json.loads(channels_path.read_text()) == {
        "9": {
            "channel_id": 9,
            "mode": "listen",
            "set_by": 7,
            "set_at": "when",
        }
    }
    assert load_sessions(str(sessions_path))[7] == session
    assert load_channel_config(str(channels_path))[9] == config


def test_atomic_writer_preserves_old_file_if_serialization_fails(tmp_path):
    target = tmp_path / "state.json"
    target.write_text('{"old": true}', encoding="utf-8")

    with pytest.raises(TypeError):
        atomic_json_dump({"bad": object()}, target)

    assert target.read_text(encoding="utf-8") == '{"old": true}'
    assert list(tmp_path.glob(".*.tmp")) == []


def test_atomic_writer_tolerates_unsupported_directory_fsync(monkeypatch, tmp_path):
    target = tmp_path / "state.json"
    real_fsync = os.fsync
    calls = 0

    def fail_second_fsync(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory fsync unsupported")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_second_fsync)

    atomic_json_dump({"saved": True}, target)

    assert json.loads(target.read_text()) == {"saved": True}
