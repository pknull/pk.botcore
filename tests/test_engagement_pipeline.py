"""Shared engagement pipeline tests — the per-mode behavior contract.

These tests define the unified semantics both bots inherit:
ignore / mention / social / listen, the timed mute, stop phrases,
rate limiting, bot deference, and continuation gating.
"""

import asyncio
import time

from tests.async_utils import async_test

from pk_botcore import classifiers
from pk_botcore.assistant_runtime import AssistantRuntimeMixin
from pk_botcore.channels import (
    ChannelConfig,
    MODE_IGNORE,
    MODE_LISTEN,
    MODE_MENTION,
    MODE_SOCIAL,
)
from pk_botcore.engagement import (
    AccessResult,
    EngagementPolicy,
    is_stop_phrase,
)

BOT_ID = 900
OWNER_ID = 1
USER_ID = 2
OTHER_BOT_ID = 901
CHANNEL_ID = 111


class FakeUser:
    def __init__(self, id, display_name, bot=False):
        self.id = id
        self.display_name = display_name
        self.name = display_name
        self.bot = bot


class FakeGuild:
    def __init__(self, id=500, name="guild"):
        self.id = id
        self.name = name


class FakeChannel:
    def __init__(self, id=CHANNEL_ID, name="chan"):
        self.id = id
        self.name = name
        self.sent = []

    async def send(self, content):
        self.sent.append(content)


class FakeHistoryChannel(FakeChannel):
    def __init__(self, history_messages, **kwargs):
        super().__init__(**kwargs)
        self._history_messages = history_messages

    def history(self, *, limit, before):
        messages = list(self._history_messages)[:limit]

        async def generate():
            for msg in messages:
                yield msg

        return generate()


class FakeMessage:
    def __init__(
        self,
        content,
        author,
        *,
        guild=FakeGuild(),
        channel=None,
        mentions=(),
        attachments=(),
    ):
        self.content = content
        self.author = author
        self.guild = guild
        self.channel = channel or FakeChannel()
        self.mentions = list(mentions)
        self.attachments = list(attachments)
        self.reference = None


class FakeBot:
    def __init__(self, bot_user):
        self.user = bot_user


class EngagementHost(AssistantRuntimeMixin):
    def __init__(self, policy, bot_name="Asha"):
        self.bot = FakeBot(FakeUser(BOT_ID, bot_name, bot=True))
        self.channel_configs = {}
        self.discarded = []
        self._init_assistant_runtime(policy=policy)

    async def _process_queue_item(self, *args):
        pass

    async def _on_work_item_discarded(self, work_item):
        self.discarded.append(work_item)


def make_policy(**overrides):
    defaults = dict(
        bot_name="Asha",
        name_aliases=("asha",),
        peer_aliases=("zalgo",),
        topics_of_interest=("AAS lore",),
        out_of_scope=("simulation theory",),
        rate_limit=5,
        rate_window=60.0,
        default_mute_seconds=600,
    )
    defaults.update(overrides)
    return EngagementPolicy(**defaults)


def make_host(mode=None, **policy_overrides):
    host = EngagementHost(make_policy(**policy_overrides))
    if mode is not None:
        host.channel_configs[CHANNEL_ID] = ChannelConfig(
            channel_id=CHANNEL_ID, mode=mode, set_by=OWNER_ID, set_at=""
        )
    return host


def human(id=USER_ID, name="PK"):
    return FakeUser(id, name)


def other_bot(name="Zalgo"):
    return FakeUser(OTHER_BOT_ID, name, bot=True)


def engage_window(host, channel_id=CHANNEL_ID):
    """Open the engagement window as if the bot just replied explicitly."""
    host._conversation_tracker._last_spoke[channel_id] = time.time()


def patch_relevance(monkeypatch, result):
    calls = []

    async def fake(content, **kwargs):
        calls.append((content, kwargs))
        return result

    monkeypatch.setattr(classifiers, "check_relevance", fake)
    return calls


def forbid_relevance(monkeypatch):
    async def explode(content, **kwargs):
        raise AssertionError("relevance classifier must not be called")

    monkeypatch.setattr(classifiers, "check_relevance", explode)


def patch_continuation(monkeypatch, result):
    calls = []

    async def fake(**kwargs):
        calls.append(kwargs)
        return result

    monkeypatch.setattr(classifiers, "check_bot_continuation", fake)
    return calls


class TestIgnoreMode:
    @async_test
    async def test_mention_in_ignored_channel_is_silent(self):
        host = make_host(MODE_IGNORE)
        msg = FakeMessage("@Asha hi", human(), mentions=[host.bot.user])
        decision = await host._decide_engagement(msg)
        assert not decision.engage
        assert decision.reason == "ignored"

    @async_test
    async def test_stop_phrase_in_ignored_channel_mutates_nothing(self):
        host = make_host(MODE_IGNORE)
        engage_window(host)
        msg = FakeMessage("asha stfu", human())
        decision = await host._decide_engagement(msg)
        assert decision.reason == "ignored"
        assert not host._is_muted(CHANNEL_ID)
        assert host._conversation_tracker.is_engaged(CHANNEL_ID)

    @async_test
    async def test_command_prefix_skipped(self):
        host = make_host()
        decision = await host._decide_engagement(FakeMessage("!roll d20", human()))
        assert decision.reason == "command_prefix"


class TestMentionMode:
    @async_test
    async def test_mention_engages_with_stripped_prompt(self):
        host = make_host()  # unconfigured channel defaults to mention
        msg = FakeMessage(
            f"<@{BOT_ID}> hello there", human(), mentions=[host.bot.user]
        )
        decision = await host._decide_engagement(msg)
        assert decision.engage
        assert decision.reason == "mention"
        assert decision.prompt == "hello there"

    @async_test
    async def test_name_address_does_not_engage(self):
        host = make_host(MODE_MENTION)
        decision = await host._decide_engagement(
            FakeMessage("asha, what do you think?", human())
        )
        assert not decision.engage
        assert decision.reason == "no_trigger"

    @async_test
    async def test_follow_up_within_window_does_not_engage(self):
        host = make_host(MODE_MENTION)
        engage_window(host)
        decision = await host._decide_engagement(
            FakeMessage("and another thing", human())
        )
        assert not decision.engage
        assert decision.reason == "no_trigger"


class TestSocialMode:
    @async_test
    async def test_name_address_engages_and_opens_window(self):
        host = make_host(MODE_SOCIAL)
        decision = await host._decide_engagement(
            FakeMessage("asha, hello", human())
        )
        assert decision.engage
        assert decision.reason == "name"
        host._conversation_tracker.record_response(CHANNEL_ID)
        assert host._conversation_tracker.is_engaged(CHANNEL_ID)

    @async_test
    async def test_window_follow_up_engages_without_refreshing(self):
        host = make_host(MODE_SOCIAL)
        engage_window(host)
        original = host._conversation_tracker._last_spoke[CHANNEL_ID]
        decision = await host._decide_engagement(
            FakeMessage("and another thing", human())
        )
        assert decision.engage
        assert decision.reason == "engagement"
        host._conversation_tracker.record_response(CHANNEL_ID)
        assert host._conversation_tracker._last_spoke[CHANNEL_ID] == original

    @async_test
    async def test_bot_authored_follow_up_does_not_engage(self):
        host = make_host(MODE_SOCIAL)
        engage_window(host)
        decision = await host._decide_engagement(
            FakeMessage("interesting point", other_bot())
        )
        assert not decision.engage
        assert decision.reason == "no_trigger"

    @async_test
    async def test_follow_up_naming_peer_bot_is_skipped(self):
        host = make_host(MODE_SOCIAL)
        engage_window(host)
        decision = await host._decide_engagement(
            FakeMessage("zalgo, what do you think?", human())
        )
        assert not decision.engage
        assert decision.reason == "no_trigger"

    @async_test
    async def test_expired_window_does_not_engage(self):
        host = make_host(MODE_SOCIAL)
        host._conversation_tracker._last_spoke[CHANNEL_ID] = time.time() - 301
        decision = await host._decide_engagement(
            FakeMessage("still there?", human())
        )
        assert not decision.engage


class TestListenMode:
    @async_test
    async def test_mention_bypasses_relevance(self, monkeypatch):
        forbid_relevance(monkeypatch)
        host = make_host(MODE_LISTEN)
        msg = FakeMessage(f"<@{BOT_ID}> hi", human(), mentions=[host.bot.user])
        decision = await host._decide_engagement(msg)
        assert decision.engage
        assert decision.reason == "mention"

    @async_test
    async def test_relevant_unaddressed_message_engages(self, monkeypatch):
        calls = patch_relevance(monkeypatch, True)
        host = make_host(MODE_LISTEN)
        decision = await host._decide_engagement(
            FakeMessage("anyone know AAS lore?", human())
        )
        assert decision.engage
        assert decision.reason == "relevance"
        _, kwargs = calls[0]
        assert kwargs["bot_name"] == "Asha"
        assert kwargs["topics_of_interest"] == ("AAS lore",)
        assert kwargs["out_of_scope"] == ("simulation theory",)

    @async_test
    async def test_irrelevant_message_is_silent(self, monkeypatch):
        patch_relevance(monkeypatch, False)
        host = make_host(MODE_LISTEN)
        decision = await host._decide_engagement(
            FakeMessage("what's for lunch", human())
        )
        assert not decision.engage
        assert decision.reason == "irrelevant"

    @async_test
    async def test_classifier_failure_is_silent(self, monkeypatch):
        # Real check_relevance with a broken transport: fail closed.
        async def broken(prompt, timeout):
            raise RuntimeError("outage")

        monkeypatch.setattr(classifiers, "_haiku_yes_no", broken)
        host = make_host(MODE_LISTEN)
        decision = await host._decide_engagement(
            FakeMessage("anyone know AAS lore?", human())
        )
        assert not decision.engage
        assert decision.reason == "irrelevant"

    @async_test
    async def test_task_continuation_bypasses_relevance(self, monkeypatch):
        forbid_relevance(monkeypatch)
        host = make_host(MODE_LISTEN)
        host._record_active_task_state(
            context_id=CHANNEL_ID,
            requester_id=USER_ID,
            original_prompt="delete the old files",
            cleaned_text="Confirm before I proceed?",
            saw_commands=True,
        )
        decision = await host._decide_engagement(FakeMessage("yes do it", human()))
        assert decision.engage
        assert decision.reason == "task_continuation"

    @async_test
    async def test_task_continuation_denied_for_other_user(self, monkeypatch):
        patch_relevance(monkeypatch, False)
        host = make_host(MODE_LISTEN)
        host._record_active_task_state(
            context_id=CHANNEL_ID,
            requester_id=USER_ID,
            original_prompt="delete the old files",
            cleaned_text="Confirm before I proceed?",
            saw_commands=True,
        )
        stranger = human(id=99, name="Stranger")
        decision = await host._decide_engagement(FakeMessage("yes do it", stranger))
        assert not decision.engage
        assert decision.reason == "irrelevant"


class TestRateLimit:
    @async_test
    async def test_charged_before_classifier(self, monkeypatch):
        calls = patch_relevance(monkeypatch, True)
        host = make_host(MODE_LISTEN, rate_limit=1)
        first = await host._decide_engagement(FakeMessage("AAS lore?", human()))
        assert first.engage
        second = await host._decide_engagement(FakeMessage("more lore?", human()))
        assert not second.engage
        assert second.reason == "rate_limited"
        assert len(calls) == 1

    @async_test
    async def test_charged_once_per_message(self):
        host = make_host()
        msg = FakeMessage("hi", human(), mentions=[host.bot.user])
        await host._decide_engagement(msg)
        assert len(host._entry_timestamps[USER_ID]) == 1

    @async_test
    async def test_unaddressed_chatter_not_charged(self):
        host = make_host(MODE_SOCIAL)
        await host._decide_engagement(FakeMessage("just chatting", human()))
        assert len(host._entry_timestamps[USER_ID]) == 0

    @async_test
    async def test_owner_exempt(self):
        async def owner_access(message):
            return AccessResult(
                allowed=True, speaker_name="The Keeper",
                role="Keeper", is_owner=True,
            )

        host = make_host(rate_limit=1, access_check=owner_access)
        keeper = human(id=OWNER_ID, name="PK")
        for _ in range(3):
            msg = FakeMessage("hi", keeper, mentions=[host.bot.user])
            decision = await host._decide_engagement(msg)
            assert decision.engage


class TestStopAndMute:
    @async_test
    async def test_stop_phrase_mutes_and_purges(self):
        host = make_host(MODE_SOCIAL)
        engage_window(host)
        host._message_queues[CHANNEL_ID] = asyncio.Queue()
        host._message_queues[CHANNEL_ID].put_nowait(("ctx", "queued prompt"))
        decision = await host._decide_engagement(
            FakeMessage("ZALGO SHUT THE FUCK UP".replace("ZALGO", "ASHA"), human())
        )
        assert not decision.engage
        assert decision.reason == "stop"
        assert host._is_muted(CHANNEL_ID)
        assert not host._conversation_tracker.is_engaged(CHANNEL_ID)
        assert host.discarded == [("ctx", "queued prompt")]
        assert CHANNEL_ID not in host._message_queues

    @async_test
    async def test_stop_phrase_cancels_running_worker(self):
        host = make_host(MODE_SOCIAL)
        engage_window(host)
        started = asyncio.Event()

        async def long_job():
            started.set()
            await asyncio.sleep(60)

        worker = asyncio.create_task(long_job())
        host._queue_workers[CHANNEL_ID] = worker
        await started.wait()
        decision = await host._decide_engagement(FakeMessage("asha stop", human()))
        assert decision.reason == "stop"
        assert worker.cancelled()
        assert CHANNEL_ID not in host._queue_workers

    @async_test
    async def test_mute_is_channel_wide(self):
        host = make_host(MODE_SOCIAL)
        engage_window(host)
        await host._decide_engagement(FakeMessage("asha stfu", human()))
        bystander = human(id=42, name="Bystander")
        decision = await host._decide_engagement(
            FakeMessage("asha, you there?", bystander)
        )
        assert not decision.engage
        assert decision.reason == "muted"

    @async_test
    async def test_mention_breaks_through_without_opening_window(self):
        host = make_host(MODE_SOCIAL)
        await host._mute_channel(CHANNEL_ID, 600)
        msg = FakeMessage(f"<@{BOT_ID}> quick question", human(),
                          mentions=[host.bot.user])
        decision = await host._decide_engagement(msg)
        assert decision.engage
        assert decision.reason == "mention"
        host._conversation_tracker.record_response(CHANNEL_ID)
        assert not host._conversation_tracker.is_engaged(CHANNEL_ID)

    @async_test
    async def test_stop_phrase_inert_while_muted(self):
        host = make_host(MODE_SOCIAL)
        await host._mute_channel(CHANNEL_ID, 600)
        deadline = host._mute_until[CHANNEL_ID]
        decision = await host._decide_engagement(FakeMessage("asha stop", human()))
        assert decision.reason == "muted"
        assert host._mute_until[CHANNEL_ID] == deadline

    @async_test
    async def test_mute_expires(self):
        host = make_host(MODE_SOCIAL)
        host._mute_until[CHANNEL_ID] = time.monotonic() - 1
        decision = await host._decide_engagement(FakeMessage("asha, hi", human()))
        assert decision.engage

    @async_test
    async def test_unmute_lifts_early(self):
        host = make_host(MODE_SOCIAL)
        await host._mute_channel(CHANNEL_ID, 600)
        assert host._unmute_channel(CHANNEL_ID)
        decision = await host._decide_engagement(FakeMessage("asha, hi", human()))
        assert decision.engage

    @async_test
    async def test_dm_stop_phrase_mutes_dm(self):
        host = make_host()
        dm = FakeMessage("stop", human(), guild=None)
        decision = await host._decide_engagement(dm)
        assert decision.reason == "stop"
        follow_up = FakeMessage("hello?", human(), guild=None)
        decision = await host._decide_engagement(follow_up)
        assert decision.reason == "muted"


class TestBotDeferenceAndContinuation:
    @async_test
    async def test_defers_when_other_bot_mentioned(self, monkeypatch):
        forbid_relevance(monkeypatch)
        host = make_host(MODE_LISTEN)
        peer = other_bot()
        msg = FakeMessage(f"<@{OTHER_BOT_ID}> your take?", human(), mentions=[peer])
        decision = await host._decide_engagement(msg)
        assert not decision.engage
        assert decision.reason == "deferred"

    @async_test
    async def test_double_ping_engages_both(self):
        host = make_host()
        peer = other_bot()
        msg = FakeMessage("hi both", human(), mentions=[host.bot.user, peer])
        decision = await host._decide_engagement(msg)
        assert decision.engage

    @async_test
    async def test_bot_mention_gated_by_continuation(self, monkeypatch):
        patch_continuation(monkeypatch, False)
        host = make_host()
        history = [
            FakeMessage("earlier reply", FakeUser(BOT_ID, "Asha", bot=True)),
            FakeMessage("hi", human()),
        ]
        channel = FakeHistoryChannel(history)
        msg = FakeMessage("@Asha continue", other_bot(), channel=channel,
                          mentions=[host.bot.user])
        decision = await host._decide_engagement(msg)
        assert not decision.engage
        assert decision.reason == "bot_disengage"

    @async_test
    async def test_first_contact_skips_continuation(self, monkeypatch):
        async def explode(**kwargs):
            raise AssertionError("continuation must not be called on first contact")

        monkeypatch.setattr(classifiers, "check_bot_continuation", explode)
        host = make_host()
        channel = FakeHistoryChannel([FakeMessage("hi", human())])
        msg = FakeMessage("@Asha hello", other_bot(), channel=channel,
                          mentions=[host.bot.user])
        decision = await host._decide_engagement(msg)
        assert decision.engage

    @async_test
    async def test_relevant_bot_message_gated_by_continuation(self, monkeypatch):
        patch_relevance(monkeypatch, True)
        patch_continuation(monkeypatch, True)
        host = make_host(MODE_LISTEN)
        history = [FakeMessage("earlier", FakeUser(BOT_ID, "Asha", bot=True))]
        channel = FakeHistoryChannel(history)
        msg = FakeMessage("AAS lore is fascinating", other_bot(), channel=channel)
        decision = await host._decide_engagement(msg)
        assert decision.engage
        assert decision.reason == "relevance"


class TestAccess:
    @async_test
    async def test_denied_mention_gets_rejection_reply(self):
        async def deny(message):
            return AccessResult(
                allowed=False,
                rejection_reply="You are outside the planes and unwitnessed.",
            )

        host = make_host(access_check=deny)
        msg = FakeMessage("@Asha hi", human(), mentions=[host.bot.user])
        decision = await host._decide_engagement(msg)
        assert not decision.engage
        assert decision.reason == "unauthorized"
        assert msg.channel.sent == ["You are outside the planes and unwitnessed."]

    @async_test
    async def test_denied_without_mention_is_silent(self):
        async def deny(message):
            return AccessResult(allowed=False, rejection_reply="nope")

        host = make_host(access_check=deny)
        decision = await host._decide_engagement(FakeMessage("asha hi", human()))
        assert not decision.engage
        assert FakeMessage("x", human()).channel.sent == []

    @async_test
    async def test_bot_authors_bypass_access_hook(self):
        async def explode(message):
            raise AssertionError("access hook must not run for bot authors")

        host = make_host(access_check=explode)
        channel = FakeHistoryChannel([])
        msg = FakeMessage("@Asha hi", other_bot(), channel=channel,
                          mentions=[host.bot.user])
        decision = await host._decide_engagement(msg)
        assert decision.engage
        assert decision.role == "Bot"


class TestDMs:
    @async_test
    async def test_human_dm_engages(self):
        host = make_host()
        decision = await host._decide_engagement(
            FakeMessage("hello", human(), guild=None)
        )
        assert decision.engage
        assert decision.reason == "dm"

    @async_test
    async def test_bot_dm_is_silent(self):
        host = make_host()
        decision = await host._decide_engagement(
            FakeMessage("hello", other_bot(), guild=None)
        )
        assert not decision.engage


class TestStopPhraseMatcher:
    def test_full_match_semantics(self):
        aliases = ("zalgo",)
        assert is_stop_phrase("ZALGO SHUT THE FUCK UP", BOT_ID, aliases)
        assert is_stop_phrase("stfu", BOT_ID, aliases)
        assert is_stop_phrase("zalgo, stop please", BOT_ID, aliases)
        assert is_stop_phrase(f"<@{BOT_ID}> be quiet", BOT_ID, aliases)
        assert not is_stop_phrase("what does stfu mean?", BOT_ID, aliases)
        assert not is_stop_phrase("don't stop talking", BOT_ID, aliases)
        assert not is_stop_phrase("we're good, shut up now.", BOT_ID, aliases)
        assert not is_stop_phrase("Kill it", BOT_ID, aliases)
