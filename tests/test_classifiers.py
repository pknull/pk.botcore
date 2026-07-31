"""Unit tests for the unified engagement classifiers (non-live)."""

import json

from tests.async_utils import async_test

from pk_botcore import classifiers
from pk_botcore.channels import (
    LISTEN_MODES,
    MODE_MENTION,
    MODE_SOCIAL,
    load_channel_config,
)


class TestCheckRelevance:
    @async_test
    async def test_empty_content_is_not_relevant(self, monkeypatch):
        called = []

        async def fake_yes_no(prompt, timeout):
            called.append(prompt)
            return True

        monkeypatch.setattr(classifiers, "_haiku_yes_no", fake_yes_no)
        assert await classifiers.check_relevance("   ") is False
        assert called == []

    @async_test
    async def test_fails_closed_on_classifier_error(self, monkeypatch):
        async def broken(prompt, timeout):
            raise RuntimeError("sdk exploded")

        monkeypatch.setattr(classifiers, "_haiku_yes_no", broken)
        assert await classifiers.check_relevance("is this simulated?") is False

    @async_test
    async def test_fails_closed_on_timeout(self, monkeypatch):
        async def slow(prompt, timeout):
            raise TimeoutError()

        monkeypatch.setattr(classifiers, "_haiku_yes_no", slow)
        assert await classifiers.check_relevance("hello there") is False

    @async_test
    async def test_verdict_passthrough(self, monkeypatch):
        async def yes(prompt, timeout):
            return True

        async def no(prompt, timeout):
            return False

        monkeypatch.setattr(classifiers, "_haiku_yes_no", yes)
        assert await classifiers.check_relevance("on topic") is True
        monkeypatch.setattr(classifiers, "_haiku_yes_no", no)
        assert await classifiers.check_relevance("off topic") is False

    @async_test
    async def test_prompt_contains_scope_and_out_of_scope(self, monkeypatch):
        captured = {}

        async def capture(prompt, timeout):
            captured["prompt"] = prompt
            return True

        monkeypatch.setattr(classifiers, "_haiku_yes_no", capture)
        await classifiers.check_relevance(
            "tell me about dice",
            bot_name="Asha",
            topics_of_interest=["CoC 7e rules and dice"],
            out_of_scope=["simulation theory"],
            recent_context=[("PK", "hello"), ("Asha (bot)", "greetings")],
        )
        prompt = captured["prompt"]
        assert "Asha" in prompt
        assert "SPECIALIST" in prompt
        assert "- CoC 7e rules and dice" in prompt
        assert "- simulation theory" in prompt
        assert 'always respond "no"' in prompt
        assert "[PK]: hello" in prompt
        assert "[Asha (bot)]: greetings" in prompt
        assert "tell me about dice" in prompt

    @async_test
    async def test_prompt_omits_optional_sections(self, monkeypatch):
        captured = {}

        async def capture(prompt, timeout):
            captured["prompt"] = prompt
            return True

        monkeypatch.setattr(classifiers, "_haiku_yes_no", capture)
        await classifiers.check_relevance("bare message", bot_name="Zalgo")
        prompt = captured["prompt"]
        assert "(no specific domain defined)" in prompt
        assert "DIFFERENT assistant" not in prompt
        assert "Recent conversation context" not in prompt


class TestCheckBotContinuation:
    @async_test
    async def test_fails_closed_on_error(self, monkeypatch):
        async def broken(prompt, timeout):
            raise RuntimeError("boom")

        monkeypatch.setattr(classifiers, "_haiku_yes_no", broken)
        result = await classifiers.check_bot_continuation(
            bot_name="Asha",
            other_bot="Zalgo",
            message="and another thing",
            recent_context=[("Zalgo (bot)", "hey")],
        )
        assert result is False

    @async_test
    async def test_prompt_composition_and_passthrough(self, monkeypatch):
        captured = {}

        async def capture(prompt, timeout):
            captured["prompt"] = prompt
            return True

        monkeypatch.setattr(classifiers, "_haiku_yes_no", capture)
        result = await classifiers.check_bot_continuation(
            bot_name="Zalgo",
            other_bot="Asha",
            message="what do you think?",
            recent_context=[],
        )
        assert result is True
        assert "You are Zalgo" in captured["prompt"]
        assert "(Asha)" in captured["prompt"]
        assert "(no recent context)" in captured["prompt"]


class TestPackageExports:
    def test_package_reexports_unified_classifiers(self):
        import pk_botcore

        assert pk_botcore.check_relevance is classifiers.check_relevance
        assert pk_botcore.check_bot_continuation is classifiers.check_bot_continuation


class TestSocialMode:
    def test_social_is_a_valid_mode(self, tmp_path):
        config_file = tmp_path / "channel_config.json"
        config_file.write_text(json.dumps({
            "111": {"mode": "social", "set_by": 1, "set_at": "now"},
            "222": {"mode": "bogus", "set_by": 1, "set_at": "now"},
        }))
        configs = load_channel_config(str(config_file))
        assert configs[111].mode == MODE_SOCIAL
        assert configs[222].mode == MODE_MENTION
        assert MODE_SOCIAL in LISTEN_MODES
