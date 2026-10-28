from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import aiohttp
import discord

from akubot.channel_policy import ChannelPolicy, snapshot
from akubot.bot import AkuClient, in_scope
from akubot.config import ConfigurationError, Persona, Settings, local_model_url
from akubot.importer import discover, prepare, read_reviewed
from akubot.memory import Memory
from akubot.model import ModelError, Ollama
from akubot.privacy import clean_output
from akubot.service import Notice, Request, Responder
from akubot.storage import Store


ROOT = Path(__file__).resolve().parent.parent
PERSONA = Persona("Aku", "AI double", ("Keep it casual.",), ())


def settings(path: Path) -> Settings:
    return Settings(7, frozenset({1}), frozenset({10}), path / "token", path, ROOT / "config/persona.json", "http://ollama:11434", "llama3.2:3b")


class Model:
    def __init__(self):
        self.calls = []

    async def generate(self, messages):
        self.calls.append(messages)
        return "yeah probably"


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "aku.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_retrieval_deduplicates_and_handles_query_syntax(self):
        self.assertEqual(self.store.add_examples(["pizza sounds good", "yeah tomorrow", "pizza sounds good"]), 2)
        self.assertEqual(self.store.examples('pizza OR "* NEAR(')[0], "pizza sounds good")
        self.assertTrue(self.store.examples("🦆"))
        self.store.clear_examples()
        self.assertEqual(self.store.examples("pizza"), [])
        self.assertEqual(self.store.count(), 0)

    def test_pause_is_persistent_and_guild_scoped(self):
        self.store.set_paused(1, True)
        self.assertTrue(self.store.is_paused(1))
        self.assertFalse(self.store.is_paused(2))
        another = Store(self.root / "aku.sqlite3")
        try:
            self.assertTrue(another.is_paused(1))
        finally:
            another.close()


class MemoryTests(unittest.TestCase):
    def test_scope_expiry_capacity_and_snapshot_copies(self):
        now = [0.0]
        memory = Memory(ttl=10, max_sessions=2, clock=lambda: now[0])
        memory.append((1, 10, 7), "secret from first room", "reply")
        self.assertEqual(memory.get((2, 10, 7)), [])
        self.assertEqual(memory.get((1, 11, 7)), [])
        self.assertEqual(memory.get((1, 10, 8)), [])
        snapshot = memory.get((1, 10, 7))
        snapshot[0]["content"] = "changed"
        self.assertEqual(memory.get((1, 10, 7))[0]["content"], "secret from first room")
        memory.append((1, 10, 8), "other", "reply")
        memory.append((1, 10, 9), "third", "reply")
        self.assertEqual(memory.get((1, 10, 7)), [])
        now[0] = 10
        memory.purge()
        self.assertEqual(len(memory.sessions), 0)

    def test_text_and_turn_counts_are_bounded(self):
        memory = Memory()
        for _ in range(10):
            memory.append((1, 10, 7), "x" * 1500, "y" * 1800)
        self.assertEqual(len(memory.get((1, 10, 7))), 4)
        self.assertTrue(all(len(turn["content"]) <= 512 for turn in memory.get((1, 10, 7))))


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.export = self.root / "export"
        self.policy = ChannelPolicy(snapshot("1", [{"id": "123", "type": 0, "nsfw": False}, {"id": "456", "type": 0, "nsfw": False}]))
        channel = self.export / "Messages" / "c123"
        channel.mkdir(parents=True)
        (channel / "messages.json").write_text(json.dumps([
            {"Contents": "email me at person@example.com https://example.com/private <@123456789012345678> 555-123-4567", "author_id": "7"},
            {"Contents": "FRIENDS_PRIVATE_TEXT", "author_id": "8"},
            {"Contents": "yeah sounds good"},
            {"Contents": "yeah sounds good"},
        ]))

    def tearDown(self):
        self.temp.cleanup()

    def test_explicit_selection_and_redaction(self):
        output = self.root / "review.jsonl"
        kept, skipped = prepare(self.export, output, {"123"}, "7", self.policy)
        self.assertEqual((kept, skipped), (2, 2))
        text = output.read_text()
        for private in ("person@example.com", "https://example.com/private", "123456789012345678", "555-123-4567", "FRIENDS_PRIVATE_TEXT"):
            self.assertNotIn(private, text)
        self.assertEqual(len(read_reviewed(output, self.policy)), 2)

    def test_requires_channel_selection_and_preserves_existing_output(self):
        output = self.root / "review.jsonl"
        with self.assertRaises(ValueError):
            prepare(self.export, output, set(), "7", self.policy)
        output.write_text("existing")
        with self.assertRaises(FileExistsError):
            prepare(self.export, output, {"123"}, "7", self.policy)
        self.assertEqual(output.read_text(), "existing")
        with self.assertRaises(ValueError):
            prepare(self.export, self.export / "review.jsonl", {"123"}, "7", self.policy)

    def test_csv_and_no_symlink_escape(self):
        csv_channel = self.export / "Messages" / "c456"
        csv_channel.mkdir()
        (csv_channel / "messages.csv").write_text('ID,Timestamp,Contents,Attachments\n1,2026-01-01,"yeah, okay",\n')
        linked = self.export / "Messages" / "c999"
        linked.symlink_to(csv_channel, target_is_directory=True)
        self.assertEqual({item.channel_id for item in discover(self.export)}, {"123", "456"})
        output = self.root / "csv-review.jsonl"
        self.assertEqual(prepare(self.export, output, {"456"}, "7", self.policy)[0], 1)
        self.assertEqual(read_reviewed(output, self.policy), ["yeah, okay"])

    def test_partial_output_removed_on_invalid_input(self):
        target = self.export / "Messages" / "c123" / "messages.json"
        target.write_text("{invalid json")
        output = self.root / "review.jsonl"
        with self.assertRaises(ValueError):
            prepare(self.export, output, {"123"}, "7", self.policy)
        self.assertFalse(output.exists())

    def test_reviewed_file_rejects_embedded_metadata(self):
        output = self.root / "approved.jsonl"
        output.write_text(json.dumps({"text": "hello", "author_id": "7"}))
        with self.assertRaises(ValueError):
            read_reviewed(output, self.policy)

    def test_unknown_author_metadata_is_not_treated_as_owner(self):
        target = self.export / "Messages" / "c123" / "messages.json"
        target.write_text(json.dumps([{"Contents": "PRIVATE_OTHER_AUTHOR", "author": {"name": "someone"}}]))
        output = self.root / "review.jsonl"
        self.assertEqual(prepare(self.export, output, {"123"}, "7", self.policy), (0, 1))
        self.assertEqual(output.read_text(), "")

    def test_nsfw_unknown_and_changed_channel_status_are_rejected(self):
        output = self.root / "review.jsonl"
        for channels in ([{"id": "123", "type": 0, "nsfw": True}], []):
            policy = ChannelPolicy(snapshot("1", channels))
            with self.assertRaisesRegex(ValueError, "NSFW"):
                prepare(self.export, output, {"123"}, "7", policy)
            self.assertFalse(output.exists())
        prepare(self.export, output, {"123"}, "7", self.policy)
        updated = ChannelPolicy(snapshot("1", [{"id": "123", "type": 0, "nsfw": True}]))
        with self.assertRaisesRegex(ValueError, "NSFW"):
            read_reviewed(output, updated)

    def test_historical_export_nsfw_flag_also_excludes_channel(self):
        metadata = self.export / "Messages" / "c123" / "channel.json"
        metadata.write_text(json.dumps({"nsfw": True}))
        with self.assertRaisesRegex(ValueError, "NSFW"):
            prepare(self.export, self.root / "review.jsonl", {"123"}, "7", self.policy)


class ChannelPolicyTests(unittest.TestCase):
    def test_flags_names_categories_and_unknown_status(self):
        policy = ChannelPolicy(snapshot("1", [
            {"id": "1", "type": 4, "name": "NSFW category"},
            {"id": "2", "type": 0, "name": "normal", "parent_id": "1", "nsfw": False},
            {"id": "3", "type": 0, "name": "normal", "nsfw": False},
            {"id": "4", "type": 0, "name": "nsfw-chat", "nsfw": False},
            {"id": "5", "type": 0, "name": "unknown"},
        ]))
        self.assertTrue(policy.allows("3"))
        for channel_id in ("1", "2", "4", "5", "missing"):
            self.assertFalse(policy.allows(channel_id))

    def test_expired_missing_and_future_metadata_rejected(self):
        now = datetime.now(timezone.utc)
        for value in ({}, {"version": 1, "source": "discord-api", "verified_at": (now-timedelta(days=2)).isoformat(), "channels": {}},
                      {"version": 1, "source": "discord-api", "verified_at": (now+timedelta(days=1)).isoformat(), "channels": {}}):
            with self.assertRaises(ValueError):
                ChannelPolicy(value, now=now)


class ResponderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "aku.sqlite3")
        self.model = Model()
        self.responder = Responder(self.store, self.model, PERSONA)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_pipeline_context_scope_and_no_live_storage(self):
        await self.responder.reply(Request(1, 10, 7, "CANARY_PRIVATE_ROOM"))
        await self.responder.reply(Request(2, 10, 8, "other server"))
        self.assertNotIn("CANARY_PRIVATE_ROOM", json.dumps(self.model.calls[-1]))
        self.assertEqual(self.store.count(), 0)
        tables = self.store.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        for (table,) in tables:
            self.assertNotIn("CANARY_PRIVATE_ROOM", repr(self.store.db.execute(f'SELECT * FROM "{table}"').fetchall()))

    async def test_forget_cancels_generation_without_restoring_context(self):
        started, finish = asyncio.Event(), asyncio.Event()

        async def generate(messages):
            started.set()
            await finish.wait()
            return "answer to forgotten input"

        self.model.generate = generate
        request = Request(1, 10, 7, "private question")
        task = asyncio.create_task(self.responder.reply(request))
        await started.wait()
        self.responder.forget(request.key)
        finish.set()
        with self.assertRaises(Notice):
            await task
        self.assertEqual(self.responder.memory.get(request.key), [])
        self.assertEqual(self.responder.revisions, {})
        self.assertEqual(self.responder.gate.pending, 0)

    async def test_pause_and_resume_still_cancel_earlier_generation(self):
        started, finish = asyncio.Event(), asyncio.Event()

        async def generate(messages):
            started.set()
            await finish.wait()
            return "stale reply"

        self.model.generate = generate
        request = Request(1, 10, 7, "question")
        task = asyncio.create_task(self.responder.reply(request))
        await started.wait()
        self.responder.pause(1, True)
        self.responder.pause(1, False)
        finish.set()
        with self.assertRaises(Notice):
            await task
        self.assertFalse(self.responder.memory.sessions)

    async def test_rate_limits_and_bounded_queue(self):
        await self.responder.reply(Request(1, 10, 7, "first"))
        with self.assertRaisesRegex(Notice, "2 seconds"):
            await self.responder.reply(Request(1, 10, 7, "second"))
        self.responder.gate.pending = 4
        with self.assertRaisesRegex(Notice, "few replies"):
            await self.responder.reply(Request(1, 10, 8, "third"))

    async def test_model_failure_does_not_log_content_or_restore_memory(self):
        async def generate(messages):
            raise ModelError("CANARY_ERROR_BODY")

        self.model.generate = generate
        with self.assertLogs("akubot", level="WARNING") as logs:
            with self.assertRaises(Notice):
                await self.responder.reply(Request(1, 10, 7, "CANARY_REQUEST"))
        self.assertNotIn("CANARY", " ".join(logs.output))
        self.assertEqual(self.responder.gate.pending, 0)
        self.assertFalse(self.responder.memory.sessions)

    async def test_cancellation_releases_capacity(self):
        started = asyncio.Event()

        async def generate(messages):
            started.set()
            await asyncio.Future()

        self.model.generate = generate
        task = asyncio.create_task(self.responder.reply(Request(1, 10, 7, "question")))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.responder.gate.pending, 0)
        self.assertFalse(self.responder.inflight)


class FakeContent:
    def __init__(self, data):
        self.data = data

    async def iter_chunked(self, count):
        # Fragmentation catches clients that incorrectly assume read(n) is complete.
        for start in range(0, len(self.data), 137):
            yield self.data[start:start + 137]


class FakeResponse:
    def __init__(self, status, data):
        self.status = status
        self.content = FakeContent(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, status=200, data=None):
        self.status = status
        self.data = data or json.dumps({"message": {"content": "hello"}}).encode()
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse(self.status, self.data)


class ModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_local_urls_and_no_redirects_or_tools(self):
        for value in ("https://external.example:443", "http://ollama.external:11434", "http://token@ollama:11434", "http://ollama:11434/private", "http://ollama:11434?key=x"):
            with self.assertRaises(ConfigurationError):
                local_model_url(value)
        session = FakeSession()
        model = Ollama(session, "http://ollama:11434", "llama3.2:3b")
        self.assertEqual(await model.generate([{"role": "user", "content": "hi"}]), "hello")
        _, url, kwargs = session.calls[0]
        self.assertEqual(url, "http://ollama:11434/api/chat")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertNotIn("tools", kwargs["json"])

    async def test_redirect_and_oversized_response_rejected(self):
        for session in (FakeSession(302), FakeSession(data=b"x" * 262_145)):
            with self.assertRaises(ModelError):
                await Ollama(session, "http://ollama:11434", "llama3.2:3b").generate([])

    async def test_internal_thinking_and_mentions_removed(self):
        output = clean_output("<think>internal thought</think>hi @everyone <@1234567890>")
        self.assertNotIn("internal thought", output)
        self.assertNotIn("@everyone", output)
        self.assertNotIn("<@", output)


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "aku.sqlite3")
        self.client = AkuClient(settings(self.root), self.store, PERSONA)
        self.client._connection.user = SimpleNamespace(id=99)
        self.client.responder = SimpleNamespace(reply=AsyncMock(return_value="hello"))

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self.temp.cleanup()

    def message(self, content="<@99> hello", guild_id=1, channel_id=10, author_id=7, bot=False, webhook=None):
        @contextlib.asynccontextmanager
        async def typing():
            yield
        return SimpleNamespace(
            content=content,
            guild=SimpleNamespace(id=guild_id) if guild_id else None,
            channel=SimpleNamespace(id=channel_id, typing=typing),
            author=SimpleNamespace(id=author_id, bot=bot),
            webhook_id=webhook,
            reply=AsyncMock(),
        )

    async def test_explicit_mention_and_send_boundary(self):
        message = self.message()
        await self.client.on_message(message)
        request = self.client.responder.reply.call_args.args[0]
        self.assertEqual(request.text, "hello")
        self.assertEqual(request.key, (1, 10, 7))
        kwargs = message.reply.call_args.kwargs
        self.assertFalse(kwargs["mention_author"])
        self.assertEqual(kwargs["allowed_mentions"].to_dict()["parse"], [])
        self.assertTrue(kwargs["suppress_embeds"])

    async def test_rejects_unmentioned_unapproved_bot_webhook_and_dm(self):
        for message in (
            self.message("ordinary private discussion"), self.message(guild_id=2),
            self.message(channel_id=11), self.message(bot=True),
            self.message(webhook=123), self.message(guild_id=None),
        ):
            await self.client.on_message(message)
            message.reply.assert_not_awaited()
        self.client.responder.reply.assert_not_awaited()

    async def test_owner_controls_check_numeric_id(self):
        interaction = SimpleNamespace(guild_id=1, channel_id=10, user=SimpleNamespace(id=8), response=SimpleNamespace(send_message=AsyncMock()))
        self.assertFalse(await self.client._authorize(interaction, owner=True))
        interaction.user.id = 7
        self.assertTrue(await self.client._authorize(interaction, owner=True))
        interaction.guild_id = 2
        self.assertFalse(await self.client._authorize(interaction, owner=True))

    async def test_privileged_intents_and_message_cache_disabled(self):
        self.assertFalse(self.client.intents.message_content)
        self.assertFalse(self.client.intents.members)
        self.assertFalse(self.client.intents.presences)
        self.assertFalse(self.client.intents.dm_messages)
        self.assertIsNone(self.client._connection._messages)


class SetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("aku_setup", ROOT / "scripts/setup.py")
        cls.setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.setup)

    def test_private_paths_reject_repo_and_onedrive(self):
        with self.assertRaises(ValueError):
            self.setup.validate_private_dir(ROOT / "data")
        with self.assertRaises(ValueError):
            self.setup.validate_private_dir(Path("/tmp/OneDrive/secret"))

    def test_token_stays_outside_repo_env_and_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, private = root / "project", root / "private"
            (project / "config").mkdir(parents=True)
            (project / "config/persona.json").write_text((ROOT / "config/persona.json").read_text())
            token = "FAKE_TEST_ONLY_TOKEN_" + "x" * 40
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.setup.initialize(private, "7", "1", "10", token, project=project)
            self.assertNotIn(token, (project / ".env").read_text())
            self.assertNotIn(token, output.getvalue())
            self.assertEqual((private / "secrets/discord_token.txt").read_text(), token)
            self.assertEqual((private / "secrets/discord_token.txt").stat().st_mode & 0o777, 0o600)

    def test_rerunning_setup_preserves_downloaded_model_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, private = root / "project", root / "private"
            (project / "config").mkdir(parents=True)
            (project / "config/persona.json").write_text((ROOT / "config/persona.json").read_text())
            (project / ".env").write_text("OLLAMA_MODEL=llama3.1:8b\nOLLAMA_CPU_LIMIT=4\n")
            self.setup.initialize(private, "7", "1", "10", "FAKE_TOKEN_" + "x"*40, project=project)
            self.assertIn("OLLAMA_MODEL=llama3.1:8b", (project / ".env").read_text())
            self.assertIn("OLLAMA_CPU_LIMIT=4", (project / ".env").read_text())


class ConnectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("aku_connect", ROOT / "scripts/connect_discord.py")
        cls.connect = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.connect)

    def test_connect_discovers_owner_and_saves_only_private_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project, private = root / "project", root / "private"
            (project / "config").mkdir(parents=True)
            (project / "config/persona.json").write_text((ROOT / "config/persona.json").read_text())
            token = "FAKE_BOT_TOKEN_" + "x"*40
            real_initialize = self.connect.initialize

            def initialize(folder, owner, guilds, channels, secret):
                return real_initialize(folder, owner, guilds, channels, secret, project=project)

            output = io.StringIO()
            argv = ["connect_discord.py", "--guild-id", "1", "--private-dir", str(private)]
            responses = [{"id": "99", "owner": {"id": "7"}}, [{"id": "123", "type": 0, "nsfw": False}, {"id": "456", "type": 0, "nsfw": True}]]
            with patch("sys.argv", argv), patch.object(self.connect, "__file__", str(project / "scripts/connect_discord.py")), \
                 patch.object(self.connect.getpass, "getpass", return_value=token), patch.object(self.connect, "initialize", side_effect=initialize), \
                 patch.object(self.connect, "discord_get", side_effect=responses) as api, contextlib.redirect_stdout(output):
                self.assertEqual(self.connect.main(), 0)
            self.assertNotIn(token, output.getvalue())
            self.assertNotIn(token, (project / ".env").read_text())
            self.assertIn("DISCORD_OWNER_ID=7", (project / ".env").read_text())
            policy = ChannelPolicy.load(private / "config/channel-policy.json")
            self.assertTrue(policy.allows("123"))
            self.assertFalse(policy.allows("456"))
            self.assertEqual([call.args[0] for call in api.call_args_list], ["/oauth2/applications/@me", "/guilds/1/channels"])

    def test_failed_authentication_does_not_save_token(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / "private"
            output = io.StringIO()
            token = "FAKE_INVALID_TOKEN_" + "x"*40
            argv = ["connect_discord.py", "--guild-id", "1", "--private-dir", str(private)]
            with patch("sys.argv", argv), patch.object(self.connect.getpass, "getpass", return_value=token), \
                 patch.object(self.connect, "discord_get", side_effect=ValueError("Discord rejected the bot token.")), contextlib.redirect_stdout(output):
                self.assertEqual(self.connect.main(), 1)
            self.assertFalse(private.exists())
            self.assertNotIn(token, output.getvalue())

    def test_metadata_helper_cannot_fetch_messages_or_external_urls(self):
        for path in ("/channels/1/messages", "https://outside.example", "/guilds/1/channels?extra=1"):
            with self.assertRaises(ValueError):
                self.connect.discord_get(path, "FAKE_TEST_TOKEN")



if __name__ == "__main__":
    unittest.main()

