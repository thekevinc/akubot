import contextlib,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from akubot.bot import AkuClient
from akubot.config import Settings,Persona
from akubot.storage import Store
from akubot.service import Responder
from akubot.triggers import is_reply_to_bot

class DirectReplyTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
  self.store=Store(self.root/'test.sqlite3')
  settings=Settings(7,frozenset({1}),frozenset({10}),self.root/'token',self.root,self.root/'persona','http://ollama:11434','synthetic')
  self.persona=Persona('Aku','Synthetic test',('Short replies',),())
  self.client=AkuClient(settings,self.store,self.persona)
  self.client._connection.user=SimpleNamespace(id=99)
  self.client.responder=SimpleNamespace(reply=AsyncMock(return_value='synthetic reply'))
 async def asyncTearDown(self):
  await self.client.close();self.store.close();self.temp.cleanup()
 def message(self,**changes):
  @contextlib.asynccontextmanager
  async def typing():yield
  parent=SimpleNamespace(id=500,author=SimpleNamespace(id=99,bot=True),guild=SimpleNamespace(id=1),channel=SimpleNamespace(id=10),webhook_id=None,content='PRIVATE_QUOTED_TEXT_DO_NOT_COPY')
  result=SimpleNamespace(content='go for it',guild=SimpleNamespace(id=1,name='Synthetic server'),channel=SimpleNamespace(id=10,typing=typing),author=SimpleNamespace(id=7,bot=False),webhook_id=None,reply=AsyncMock(),mentions=[],reference=SimpleNamespace(type=0,message_id=500,guild_id=1,channel_id=10,resolved=parent))
  for key,value in changes.items():setattr(result,key,value)
  return result
 async def test_direct_reply_without_literal_mention_uses_only_new_text(self):
  message=self.message();await self.client.on_message(message)
  request=self.client.responder.reply.call_args.args[0]
  self.assertEqual(request.text,'go for it')
  self.assertEqual(request.key,(1,10,7))
  self.assertNotIn('PRIVATE_QUOTED_TEXT',request.text)
  self.assertFalse(message.reply.call_args.kwargs['mention_author'])
 async def test_reply_mention_metadata_does_not_require_at_text(self):
  message=self.message(reference=None,mentions=[SimpleNamespace(id=99)])
  await self.client.on_message(message)
  self.client.responder.reply.assert_awaited_once()
 async def test_other_people_forwards_deleted_and_mismatched_replies_are_ignored(self):
  messages=[]
  message=self.message();message.reference.resolved.author.id=8;messages.append(message)
  message=self.message();message.reference.type=1;messages.append(message)
  message=self.message();message.reference.resolved=None;messages.append(message)
  message=self.message();message.reference.guild_id=2;messages.append(message)
  message=self.message();message.reference.channel_id=11;messages.append(message)
  message=self.message();message.reference.message_id=999;messages.append(message)
  message=self.message();message.reference.resolved.channel.id=11;messages.append(message)
  message=self.message();message.reference.resolved.guild.id=2;messages.append(message)
  message=self.message();message.reference.resolved.webhook_id=123;messages.append(message)
  message=self.message();message.reference.message_id=None;messages.append(message)
  for message in messages:
   self.assertFalse(is_reply_to_bot(message,99))
   await self.client.on_message(message);message.reply.assert_not_awaited()
  self.client.responder.reply.assert_not_awaited()
 async def test_reply_trigger_preserves_scope_and_bot_loop_guards(self):
  messages=[self.message(reference=None),self.message(guild=SimpleNamespace(id=2)),self.message(guild=None),self.message(author=SimpleNamespace(id=99,bot=True)),self.message(webhook_id=42)]
  message=self.message();message.channel.id=11;messages.append(message)
  for message in messages:
   await self.client.on_message(message);message.reply.assert_not_awaited()
  self.client.responder.reply.assert_not_awaited()
 async def test_hidden_reply_text_and_pause_never_reach_model(self):
  model=SimpleNamespace(generate=AsyncMock(return_value='synthetic reply'))
  self.client.responder=Responder(self.store,model,self.persona,control=self.client.control)
  message=self.message(content='');await self.client.on_message(message)
  model.generate.assert_not_awaited();self.assertIn('reply ping',message.reply.call_args.args[0])
  self.store.set_paused(1,True)
  message=self.message();await self.client.on_message(message)
  model.generate.assert_not_awaited();self.assertIn('paused',message.reply.call_args.args[0])
  self.assertFalse(self.client.intents.message_content)

if __name__=='__main__':unittest.main()
