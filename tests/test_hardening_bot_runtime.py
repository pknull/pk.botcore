import asyncio
from types import SimpleNamespace

import discord
import pytest

from tests.async_utils import async_test
from pk_botcore.assistant_runtime import AssistantRuntimeMixin
from pk_botcore.bot_app import create_bot, sync_commands


class FakeTree:
    def __init__(self):
        self.global_commands = [object(), object()]
        self.guild_commands = {}
        self.sync_calls = []

    def get_commands(self, *, guild):
        assert guild is None
        return list(self.global_commands)

    def copy_global_to(self, *, guild):
        self.guild_commands[guild.id] = list(self.global_commands)

    def clear_commands(self, *, guild):
        assert guild is None
        self.global_commands.clear()

    def add_command(self, command, *, override):
        assert override
        self.global_commands.append(command)

    async def sync(self, *, guild=None):
        self.sync_calls.append(None if guild is None else guild.id)
        return (
            list(self.global_commands)
            if guild is None
            else list(self.guild_commands[guild.id])
        )


@async_test
async def test_command_sync_preserves_definitions_and_is_idempotent():
    tree = FakeTree()
    guild = SimpleNamespace(id=11, name="test")
    bot = SimpleNamespace(tree=tree, guilds=[guild])
    originals = list(tree.global_commands)

    await asyncio.gather(sync_commands(bot), sync_commands(bot))

    assert tree.global_commands == originals
    assert tree.sync_calls == [None, 11]
    assert tree.guild_commands[11] == originals


@async_test
async def test_command_sync_retries_after_partial_guild_failure():
    class FailOnceTree(FakeTree):
        def __init__(self):
            super().__init__()
            self.failed = False

        async def sync(self, *, guild=None):
            self.sync_calls.append(None if guild is None else guild.id)
            if guild is not None and not self.failed:
                self.failed = True
                raise RuntimeError("temporary Discord failure")
            return (
                list(self.global_commands)
                if guild is None
                else list(self.guild_commands[guild.id])
            )

    tree = FailOnceTree()
    guild = SimpleNamespace(id=11, name="test")
    bot = SimpleNamespace(tree=tree, guilds=[guild])

    await sync_commands(bot)
    assert not getattr(bot, "_pk_botcore_commands_synced", False)

    await sync_commands(bot)
    assert bot._pk_botcore_commands_synced is True
    assert tree.sync_calls == [None, 11, None, 11]


def test_bot_defaults_to_no_allowed_mentions():
    bot = create_bot(intents=discord.Intents.none(), description="test")
    assert bot.allowed_mentions.everyone is False
    assert bot.allowed_mentions.users is False
    assert bot.allowed_mentions.roles is False
    assert bot.allowed_mentions.replied_user is False


def test_bot_allows_explicit_mention_policy():
    policy = discord.AllowedMentions(users=True)
    bot = create_bot(
        intents=discord.Intents.none(),
        description="test",
        allowed_mentions=policy,
    )
    assert bot.allowed_mentions is policy


class Runtime(AssistantRuntimeMixin):
    def __init__(self):
        self.processed = []
        self._init_assistant_runtime(queue_maxsize=2)

    async def _process_queue_item(self, value):
        await asyncio.sleep(0)
        self.processed.append(value)


@async_test
async def test_channel_queues_are_bounded_and_maps_are_cleaned():
    runtime = Runtime()
    admitted = await asyncio.gather(
        runtime._queue_message(1, ("a",)),
        runtime._queue_message(1, ("b",)),
        runtime._queue_message(1, ("c",)),
    )
    for _ in range(20):
        if not runtime._queue_workers:
            break
        await asyncio.sleep(0)

    assert admitted.count(True) == 2
    assert admitted.count(False) == 1
    assert runtime.processed == ["a", "b"]
    assert runtime._message_queues == {}
    assert runtime._queue_workers == {}
