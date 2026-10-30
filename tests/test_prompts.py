from dataclasses import asdict
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from aiohttp.test_utils import make_mocked_request
from akubot.config import Persona
from akubot.control import Control,Tuning,RevisionConflict
from akubot.dashboard import Dashboard
from akubot.prompts import PromptSettings,PromptValidationError,build_messages
from akubot.service import Request,Responder
from akubot.storage import Store

PERSONA=Persona('Aku','Synthetic persona',('Do not include video links.',),('Synthetic public fact.',))
PERSONA_DATA=json.loads(json.dumps(asdict(PERSONA)))
CUSTOM={'system_prompt':'Owner: {name}. Instructions: {style_rules}. Facts: {public_facts}. Examples: {examples}',
        'user_prompt':'Reply to this message:\n{message}'}


class PromptTests(unittest.TestCase):
    def test_owner_rules_are_present_and_values_are_not_recursively_expanded(self):
        messages=build_messages(PERSONA,['SYNTHETIC_EXAMPLE'],PromptSettings(),'{name}')
        self.assertIn('Do not include video links.',messages[0]['content'])
        self.assertIn('SYNTHETIC_EXAMPLE',messages[0]['content'])
        self.assertEqual(messages[-1]['content'],'{name}')
        persona=Persona('{examples}','Synthetic',('{public_facts}',),('SYNTHETIC_FACT',))
        system=PromptSettings.from_dict(CUSTOM).render_system(persona,['SYNTHETIC_EXAMPLE'])
        self.assertIn('Owner: {examples}',system)
        self.assertIn('Instructions: - {public_facts}',system)
    def test_user_template_is_applied_to_raw_history_without_mutating_it(self):
        history=[{'role':'user','content':'first'},{'role':'assistant','content':'answer'}]
        messages=build_messages(PERSONA,[],PromptSettings.from_dict(CUSTOM),'second',history)
        self.assertEqual(messages[1]['content'],'Reply to this message:\nfirst')
        self.assertEqual(messages[2]['content'],'answer')
        self.assertEqual(messages[3]['content'],'Reply to this message:\nsecond')
        self.assertEqual(history[0]['content'],'first')
    def test_template_validation_keeps_incoming_message_and_never_executes_format_fields(self):
        for changes in ({'user_prompt':'message omitted'},{'system_prompt':'{unknown}'},
                        {'system_prompt':''},{'system_prompt':'x'*16001},{'user_prompt':True}):
            with self.assertRaises(PromptValidationError):PromptSettings.from_dict({**CUSTOM,**changes})
        settings=PromptSettings.from_dict({'system_prompt':'JSON: {"value": 1}. {name.__class__}', 'user_prompt':'{message}'})
        self.assertIn('{name.__class__}',settings.render_system(PERSONA,[]))
    def test_saved_settings_migrate_and_preserve_custom_prompts_on_other_saves(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'data.sqlite3')
            try:
                store.write_control({'revision':4,'tuning':asdict(Tuning('base:latest')),'persona':asdict(PERSONA)})
                control=Control(store,'base:latest',PERSONA)
                self.assertEqual(control.prompts.user_prompt,'{message}')
                control.update_persona(4,PERSONA_DATA,CUSTOM)
                control.update_tuning(5,{**asdict(control.tuning),'temperature':.5})
                control.update_persona(6,PERSONA_DATA)
                restored=Control(store,'base:latest',PERSONA)
                self.assertEqual(asdict(restored.prompts),CUSTOM)
                with self.assertRaises(RevisionConflict):control.update_persona(4,PERSONA_DATA,CUSTOM)
            finally:store.close()


class PromptEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'data.sqlite3')
        self.control=Control(self.store,'base:latest',PERSONA)
        self.calls=[]
        async def generate(messages):
            self.calls.append(messages)
            return 'SYNTHETIC_REPLY'
        self.responder=Responder(self.store,SimpleNamespace(generate=generate),PERSONA,control=self.control)
        client=SimpleNamespace(control=self.control,store=self.store,responder=self.responder,
                               settings=SimpleNamespace(guild_ids=frozenset({1}),data_dir=Path(self.temp.name)))
        self.dashboard=Dashboard(client)
        self.app=self.dashboard.app();self.app.freeze();await self.app.startup()
    async def asyncTearDown(self):
        await self.app.cleanup();self.store.close();self.temp.cleanup()
    async def request(self,method,path,body=None,headers=None):
        values={'Host':'localhost:8765','X-Aku-Local':'1','Content-Type':'application/json'};values.update(headers or {})
        req=make_mocked_request(method,path,headers=values,app=self.app)
        if body is not None:req._read_bytes=json.dumps(body).encode()
        return await self.app._handle(req)
    async def test_renderer_matches_actual_model_messages_and_has_no_side_effects(self):
        draft={'persona':asdict(PERSONA),'prompts':CUSTOM,'message':'SYNTHETIC_INPUT'}
        self.store.add_examples(['SYNTHETIC_INPUT example'])
        before=self.control.document()
        response=await self.request('POST','/api/prompts/render',draft)
        self.assertEqual(response.status,200)
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        composed=json.loads(response.text)['messages']
        self.assertEqual(self.control.document(),before)
        self.assertFalse(self.calls)
        self.assertFalse(self.responder.memory.sessions)
        self.control.update_persona(0,PERSONA_DATA,CUSTOM)
        await self.responder.reply(Request(0,0,0,'SYNTHETIC_INPUT',source='preview'))
        self.assertEqual(self.calls[-1],composed)
        self.assertNotIn('SYNTHETIC_INPUT',json.dumps(self.responder.metrics.snapshot()))
    async def test_save_applies_to_next_reply_and_clears_existing_conversations(self):
        self.responder.memory.append((1,2,3),'old input','old output')
        response=await self.request('PUT','/api/persona',{'revision':0,'persona':asdict(PERSONA),'prompts':CUSTOM})
        self.assertEqual(response.status,200)
        self.assertEqual(json.loads(response.text)['prompts'],CUSTOM)
        self.assertFalse(self.responder.memory.sessions)
        await self.responder.reply(Request(0,0,0,'next',source='preview'))
        self.assertEqual(self.calls[-1][-1]['content'],'Reply to this message:\nnext')
        self.assertIn('Do not include video links.',self.calls[-1][0]['content'])
    async def test_render_endpoint_is_private_and_rejects_invalid_templates(self):
        draft={'persona':asdict(PERSONA),'prompts':CUSTOM,'message':'test'}
        self.assertEqual((await self.request('POST','/api/prompts/render',draft,{'Origin':'http://evil.example'})).status,403)
        self.assertEqual((await self.request('POST','/api/prompts/render',draft,{'X-Aku-Local':''})).status,403)
        draft['prompts']={**CUSTOM,'user_prompt':'missing incoming message'}
        response=await self.request('POST','/api/prompts/render',draft)
        self.assertEqual(response.status,400)
        self.assertIn('{message}',json.loads(response.text)['error'])
