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
from akubot.prompts import PromptSettings,PromptValidationError,build_messages,default_prompts
from akubot.service import Request,Responder
from akubot.storage import Store

PERSONA=Persona('Aku','Synthetic owner style',('Do not include video links.',),('Synthetic public fact.',))
PERSONA_DATA=json.loads(json.dumps(asdict(PERSONA)))
CUSTOM={'system_prompt':'You are akubot. No links. JSON {"literal": true}.',
        'user_prompt':'Answer in one short sentence.'}


class PromptTests(unittest.TestCase):
    def test_defaults_are_plain_and_carry_existing_preferences_into_visible_text(self):
        settings=default_prompts(PERSONA)
        self.assertIn('You are akubot',settings.system_prompt)
        self.assertIn('Do not include video links.',settings.system_prompt)
        self.assertIn('Synthetic public fact.',settings.system_prompt)
        self.assertNotIn('{style_rules}',settings.system_prompt)
        self.assertNotIn('{message}',settings.user_prompt)
        self.assertTrue(settings.user_prompt.strip())
    def test_incoming_message_is_appended_and_history_is_not_rewritten(self):
        history=[{'role':'user','content':'first'},{'role':'assistant','content':'answer'}]
        messages=build_messages(PromptSettings.from_dict(CUSTOM),'{name} second',history)
        self.assertEqual(messages[0]['content'],CUSTOM['system_prompt'])
        self.assertEqual(messages[1]['content'],'first')
        self.assertEqual(messages[2]['content'],'answer')
        self.assertEqual(messages[3]['content'],'Answer in one short sentence.\n\nMessage:\n{name} second')
        self.assertEqual(history[0]['content'],'first')
    def test_plain_text_validation_and_empty_user_instruction(self):
        for changes in ({'system_prompt':''},{'system_prompt':'x'*16001},{'user_prompt':True},{'user_prompt':'x'*4001}):
            with self.assertRaises(PromptValidationError):PromptSettings.from_dict({**CUSTOM,**changes})
        settings=PromptSettings.from_dict({'system_prompt':'Literal {name.__class__} {unknown}', 'user_prompt':''})
        self.assertEqual(build_messages(settings,'incoming')[-1]['content'],'incoming')
        self.assertEqual(build_messages(settings,'incoming')[0]['content'],'Literal {name.__class__} {unknown}')
    def test_old_templates_migrate_once_and_plain_prompt_edits_persist(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'data.sqlite3')
            try:
                store.write_control({'revision':4,'tuning':asdict(Tuning('base:latest')),'persona':asdict(PERSONA),
                                     'prompts':{'system_prompt':'Old template {style_rules}','user_prompt':'{message}'}})
                control=Control(store,'base:latest',PERSONA)
                self.assertEqual(control.persona.name,'akubot')
                self.assertIn('Do not include video links.',control.prompts.system_prompt)
                self.assertNotIn('{message}',control.prompts.user_prompt)
                control.update_prompts(4,CUSTOM)
                control.update_tuning(5,{**asdict(control.tuning),'temperature':.5})
                restored=Control(store,'base:latest',PERSONA)
                self.assertEqual(asdict(restored.prompts),CUSTOM)
                self.assertEqual(restored.document()['prompt_version'],2)
                with self.assertRaises(RevisionConflict):control.update_prompts(4,CUSTOM)
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
    async def test_only_two_prompt_fields_are_returned_and_editor_has_no_extra_panels(self):
        response=await self.request('GET','/api/persona')
        self.assertEqual(set(json.loads(response.text)),{'revision','prompts'})
        self.assertEqual(set(json.loads(response.text)['prompts']),{'system_prompt','user_prompt'})
        page=(await self.request('GET','/')).text
        for removed in ('id="render-prompts"','name="style_rules"','name="public_facts"','id="restore-prompts"'):
            self.assertNotIn(removed,page)
        self.assertEqual((await self.request('POST','/api/prompts/render',{})).status,404)
    async def test_saving_plain_prompts_applies_exact_text_and_clears_memory(self):
        self.store.add_examples(['HIDDEN_EXAMPLE_CANARY'])
        self.responder.memory.append((1,2,3),'old input','old output')
        response=await self.request('PUT','/api/persona',{'revision':0,'prompts':CUSTOM})
        self.assertEqual(response.status,200)
        self.assertEqual(json.loads(response.text)['prompts'],CUSTOM)
        self.assertFalse(self.responder.memory.sessions)
        await self.responder.reply(Request(0,0,0,'SYNTHETIC_INPUT',source='preview'))
        self.assertEqual(self.calls[-1][0]['content'],CUSTOM['system_prompt'])
        self.assertEqual(self.calls[-1][-1]['content'],'Answer in one short sentence.\n\nMessage:\nSYNTHETIC_INPUT')
        self.assertNotIn('HIDDEN_EXAMPLE_CANARY',json.dumps(self.calls))
        self.assertNotIn('SYNTHETIC_INPUT',json.dumps(self.responder.metrics.snapshot()))
    async def test_saving_is_private_and_invalid_prompts_are_rejected(self):
        draft={'revision':0,'prompts':CUSTOM}
        self.assertEqual((await self.request('PUT','/api/persona',draft,{'Origin':'http://evil.example'})).status,403)
        self.assertEqual((await self.request('PUT','/api/persona',draft,{'X-Aku-Local':''})).status,403)
        draft['prompts']={**CUSTOM,'system_prompt':''}
        self.assertEqual((await self.request('PUT','/api/persona',draft)).status,400)
