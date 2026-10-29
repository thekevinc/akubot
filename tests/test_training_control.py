from __future__ import annotations
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from aiohttp.test_utils import make_mocked_request
from akubot.dashboard import Dashboard
from akubot.training_state import TrainingControl, TrainingControlError, atomic_json


class TrainingControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.control = TrainingControl(self.root)
    def tearDown(self):
        self.temp.cleanup()
    def running(self, phase='training'):
        atomic_json(self.root/'status.json', {'state': phase, 'step': 10, 'total_steps': 50,
                    'prompt': 'PRIVATE_CANARY', 'recent': [{'step': 10, 'loss': 2.4, 'text': 'PRIVATE_CANARY'}]})
        atomic_json(self.root/'heartbeat.json', {'at': time.time(), 'alive': True})
        self.control.set_desired('running')
    def test_missing_worker_cannot_be_started_from_dashboard(self):
        self.assertEqual(self.control.status()['state'], 'not_started')
        with self.assertRaises(TrainingControlError): self.control.request('resume')
        self.assertFalse((self.root/'command.json').exists())
    def test_pause_is_a_request_until_checkpoint_is_saved(self):
        self.running()
        paused = self.control.request('pause')
        self.assertEqual(paused['state'], 'training')
        self.assertEqual(paused['desired'], 'paused')
        self.assertFalse(paused['can_pause'])
        self.assertTrue(paused['can_resume'])
        self.control.request('resume')
        self.assertEqual(self.control.desired(), 'running')
    def test_stale_heartbeat_blocks_controls(self):
        self.running()
        atomic_json(self.root/'heartbeat.json', {'at': time.time()-60, 'alive': True})
        self.assertFalse(self.control.status()['worker_online'])
        with self.assertRaises(TrainingControlError): self.control.request('pause')
    def test_state_is_metrics_only_and_sanitized(self):
        self.running()
        self.assertNotIn('PRIVATE_CANARY', json.dumps(self.control.status()))
        (self.root/'status.json').write_text('{"state":"training","loss":NaN,"step":true}')
        self.assertNotIn('loss', self.control.status())
        self.assertEqual(self.control.status()['step'], 0)
    def test_corrupt_command_fails_to_paused_and_symlinks_blocked(self):
        (self.root/'command.json').write_text('{invalid')
        self.assertEqual(self.control.desired(), 'paused')
        (self.root/'command.json').unlink()
        (self.root/'command.json').symlink_to(self.root/'elsewhere')
        with self.assertRaises(ValueError): self.control.set_desired('running')
    def test_terminal_and_export_states_are_not_pausable(self):
        for phase in ('exporting','completed','failed','interrupted','ready'):
            self.running(phase)
            self.assertFalse(self.control.status()['can_pause'])


class TrainingEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dashboard = Dashboard(SimpleNamespace(settings=SimpleNamespace(data_dir=Path(self.temp.name))))
        self.app = self.dashboard.app()
        self.app.freeze()
        await self.app.startup()
    async def asyncTearDown(self):
        await self.app.cleanup()
        self.temp.cleanup()
    async def request(self, method, path, body=None, headers=None):
        values={'Host':'localhost:8765','X-Aku-Local':'1','Content-Type':'application/json'}
        values.update(headers or {})
        req=make_mocked_request(method,path,headers=values,app=self.app)
        if body is not None: req._read_bytes=json.dumps(body).encode()
        return await self.app._handle(req)
    async def test_local_boundary_and_only_fixed_actions(self):
        self.assertEqual((await self.request('GET','/api/training')).status,200)
        for headers in ({'Origin':'http://evil.example'},{'Host':'evil.example'},{'X-Aku-Local':''}):
            self.assertEqual((await self.request('POST','/api/training/control',{'action':'pause'},headers)).status,403)
        for body in ({'action':'start'},{'action':'resume','path':'/tmp/evil'},{'action':[]},[]):
            self.assertEqual((await self.request('POST','/api/training/control',body)).status,400)
        response=await self.request('POST','/api/training/control',{'action':'resume'})
        self.assertEqual(response.status,409)
        self.assertEqual(response.headers['Cache-Control'],'no-store')
    async def test_pause_and_resume_only_write_mailbox(self):
        control=self.dashboard.training
        atomic_json(control.root/'status.json',{'state':'training','step':3,'total_steps':10})
        atomic_json(control.root/'heartbeat.json',{'at':time.time(),'alive':True})
        control.set_desired('running')
        response=await self.request('POST','/api/training/control',{'action':'pause'})
        self.assertEqual(response.status,200)
        self.assertEqual(control.desired(),'paused')
        self.assertEqual(json.loads(response.text)['state'],'training')
        response=await self.request('POST','/api/training/control',{'action':'resume'})
        self.assertEqual(response.status,200)
        self.assertEqual(control.desired(),'running')
