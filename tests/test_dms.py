from __future__ import annotations

import contextlib
import json
from dataclasses import asdict
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord

from akubot.bot import AkuClient
from akubot.config import Persona, Settings
from akubot.dashboard import Dashboard
from akubot.service import DM_SCOPE, Gate, Notice, Request, Responder
from akubot.storage import Store


class DirectMessageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = Store(root / 'test.sqlite3')
        settings = Settings(7, frozenset({1}), frozenset({10}), root / 'token', root, root / 'persona', 'http://ollama:11434', 'synthetic')
        self.persona = Persona('akubot', 'Synthetic persona', (), ())
        self.client = AkuClient(settings, self.store, self.persona)
        self.client._connection.user = SimpleNamespace(id=99)
        self.model = SimpleNamespace(generate=AsyncMock(return_value='SYNTHETIC_ANSWER'))
        self.client.responder = Responder(self.store, self.model, self.persona, control=self.client.control)
        self.dashboard = Dashboard(self.client)

    async def asyncTearDown(self):
        await self.dashboard.close()
        await self.client.close()
        self.store.close()
        self.temp.cleanup()

    def message(self, text='SYNTHETIC_DM', user=8, channel=80, kind=discord.ChannelType.private, bot=False, webhook=None):
        @contextlib.asynccontextmanager
        async def typing():
            yield
        return SimpleNamespace(
            guild=None, channel=SimpleNamespace(id=channel, type=kind, typing=typing),
            author=SimpleNamespace(id=user, bot=bot), webhook_id=webhook,
            content=text, mentions=[], reference=None, reply=AsyncMock(),
        )

    async def call(self, handler, body):
        return await handler(SimpleNamespace(json=AsyncMock(return_value=body)))

    async def test_dm_without_mention_uses_current_prompts_and_safe_reply(self):
        prompts={'system_prompt': 'SYNTHETIC_SYSTEM', 'user_prompt': 'SYNTHETIC_INSTRUCTION'}
        self.client.control.update_prompts(self.client.control.revision, prompts)
        message=self.message()
        await self.client.on_message(message)
        messages=self.model.generate.call_args.args[0]
        self.assertEqual(messages[0]['content'], prompts['system_prompt'])
        self.assertIn(prompts['user_prompt'], messages[-1]['content'])
        self.assertIn('SYNTHETIC_DM', messages[-1]['content'])
        message.reply.assert_awaited_once()
        self.assertFalse(message.reply.call_args.kwargs['mention_author'])
        self.assertEqual(message.reply.call_args.kwargs['allowed_mentions'].to_dict()['parse'], [])
        self.assertTrue(self.client.intents.dm_messages)
        self.assertFalse(self.client.intents.message_content)
        self.assertEqual(self.store.count(), 0)
        activity=self.client.responder.metrics.snapshot()
        self.assertEqual(activity['recent'][0]['source'], 'dm')
        self.assertEqual(activity['counts']['reply'], 1)
        self.assertNotIn('SYNTHETIC_DM', json.dumps(activity))
        self.assertNotIn('user_id', json.dumps(activity))

    async def test_group_dms_bots_and_webhooks_do_not_reach_model(self):
        for message in (self.message(kind=discord.ChannelType.group), self.message(bot=True), self.message(webhook=1)):
            await self.client.on_message(message)
            message.reply.assert_not_awaited()
        self.model.generate.assert_not_awaited()

    async def test_dm_history_is_separate_from_servers_and_other_people(self):
        responder=self.client.responder
        requests=[Request(DM_SCOPE,80,7,'PRIVATE_DM_A'),Request(1,10,7,'PRIVATE_SERVER'),Request(DM_SCOPE,81,8,'PRIVATE_DM_B'),Request(DM_SCOPE,80,7,'FOLLOWUP')]
        for request in requests:
            responder.gate.users.clear()
            await responder.reply(request)
        calls=[json.dumps(call.args[0]) for call in self.model.generate.call_args_list]
        self.assertNotIn('PRIVATE_DM_A',calls[1])
        self.assertNotIn('PRIVATE_DM_A',calls[2])
        self.assertIn('PRIVATE_DM_A',calls[3])
        self.assertNotIn('PRIVATE_SERVER',calls[3])
        self.assertNotIn('PRIVATE_DM_B',calls[3])

    async def test_dashboard_pause_and_forget_cover_dms(self):
        responder=self.client.responder
        await responder.reply(Request(DM_SCOPE,80,8,'PRIVATE_DM'))
        await self.call(self.dashboard.forget,{})
        self.assertEqual(responder.memory.get((DM_SCOPE,80,8)),[])
        await self.call(self.dashboard.pause,{'paused':True})
        self.model.generate.reset_mock()
        await self.client.on_message(self.message())
        self.model.generate.assert_not_awaited()
        self.assertTrue(self.store.is_paused(DM_SCOPE))
        self.assertTrue(self.store.is_paused(1))
        await self.call(self.dashboard.pause,{'paused':False})
        responder.gate.users.clear()
        message=self.message()
        await self.client.on_message(message)
        self.model.generate.assert_awaited_once()

    async def test_prompt_and_model_changes_clear_dm_history(self):
        responder=self.client.responder
        responder.memory.append((DM_SCOPE,80,8),'PRIVATE_DM','answer')
        await self.call(self.dashboard.save_persona,{'revision':self.client.control.revision,'prompts':{'system_prompt':'new synthetic prompt','user_prompt':'reply'}})
        self.assertFalse(responder.memory.sessions)
        responder.memory.append((DM_SCOPE,80,8),'PRIVATE_DM','answer')
        self.dashboard.model_status=AsyncMock(return_value={'installed':['synthetic','other-synthetic']})
        values=asdict(self.client.control.tuning)
        values['model']='other-synthetic'
        await self.call(self.dashboard.save_settings,{'revision':self.client.control.revision,'tuning':values})
        self.assertFalse(responder.memory.sessions)

    def test_dm_minute_limits_are_per_conversation_and_keep_shared_queue(self):
        gate=Gate(clock=lambda:0)
        gate.guilds[-80]=__import__('collections').deque([0]*30)
        with self.assertRaisesRegex(Notice,'conversation'):
            gate.enter(Request(DM_SCOPE,80,8,'hello'))
        for request in (Request(DM_SCOPE,81,9,'hello'),Request(1,10,10,'hello')):
            gate.enter(request)
            gate.leave()
        gate.pending=4
        with self.assertRaisesRegex(Notice,'few replies'):
            gate.enter(Request(DM_SCOPE,82,11,'hello'))


if __name__ == '__main__':
    unittest.main()
