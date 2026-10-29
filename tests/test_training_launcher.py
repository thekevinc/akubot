"""Synthetic controls and host launcher tests. Docker calls are mocked."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'training'))
from launcher import Launcher, install_launcher, unit_text
from run_full import CONTAINER, IMAGE
from akubot.training_state import RUN_ID, TrainingControl, TrainingControlError, atomic_json, read_json


def request(identifier='a'*32, **changes):
    return {'version': 1, 'id': identifier, 'action': 'start', 'at': time.time(), **changes}


class StartStopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.control = TrainingControl(self.root)
        atomic_json(self.root/'status.json', {'state':'ready','step':0,'total_steps':20})
    def tearDown(self):
        self.temp.cleanup()
    def launcher(self, **changes):
        atomic_json(self.root/'launcher.json', {'version':1,'at':time.time(),'alive':True,
                    'worker_state':'missing','job_prepared':True,**changes})
    def test_start_requires_fresh_launcher_and_fixed_job(self):
        with self.assertRaises(TrainingControlError):self.control.request('start')
        self.launcher(at=time.time()-60)
        self.assertFalse(self.control.status()['can_start'])
        self.launcher(job_prepared=False)
        self.assertFalse(self.control.status()['can_start'])
        self.launcher()
        state=self.control.request('start')
        self.assertTrue(state['launch_pending'])
        self.assertFalse(state['can_start'])
        saved=read_json(self.root/'launch-request.json')
        self.assertEqual(set(saved),{'version','id','action','at'})
        self.assertEqual(saved['action'],'start')
        with self.assertRaises(TrainingControlError):self.control.request('start')
        self.assertFalse((self.root/'command.json').exists())
    def test_completed_or_running_job_cannot_start_twice(self):
        self.launcher(worker_state='running')
        self.assertFalse(self.control.status()['can_start'])
        self.launcher()
        atomic_json(self.root/'status.json',{'state':'completed'})
        self.assertFalse(self.control.status()['can_start'])
    def test_stop_during_startup_does_not_require_worker_heartbeat(self):
        self.launcher(worker_state='running')
        self.assertTrue(self.control.status()['can_stop'])
        state=self.control.request('stop')
        self.assertEqual(state['desired'],'stopped')
        self.assertFalse(state['can_stop'])
        self.assertFalse(state['can_resume'])
    def test_stop_from_paused_and_export_protected(self):
        self.launcher(worker_state='running')
        atomic_json(self.root/'status.json',{'state':'paused','step':9})
        atomic_json(self.root/'heartbeat.json',{'at':time.time(),'alive':True})
        self.control.set_desired('paused')
        self.assertEqual(self.control.request('stop')['desired'],'stopped')
        self.control.set_desired('running')
        atomic_json(self.root/'status.json',{'state':'exporting','step':20})
        with self.assertRaises(TrainingControlError):self.control.request('stop')
    def test_failed_launch_is_visible_and_retryable(self):
        atomic_json(self.root/'launch-request.json',request())
        self.launcher(last_request_id='a'*32,last_result='failed',error_code='ImageMissing')
        state=self.control.status()
        self.assertEqual(state['launcher_error'],'ImageMissing')
        self.assertTrue(state['can_start'])


class HostLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        atomic_json(self.root/'training'/RUN_ID/'dataset/job.json',{'synthetic':True})
        self.launcher=Launcher(self.root)
    def tearDown(self):self.temp.cleanup()
    def test_idle_helper_never_launches_anything(self):
        with patch.object(self.launcher,'probe',return_value='missing'),patch('launcher.subprocess.run') as run:
            self.launcher.tick()
            run.assert_not_called()
        self.assertTrue(read_json(self.launcher.mailbox/'launcher.json')['alive'])
    def test_start_uses_only_fixed_arguments_and_is_not_replayed(self):
        with patch.object(self.launcher,'docker',return_value=SimpleNamespace(returncode=0)),patch('launcher.subprocess.run',return_value=SimpleNamespace(returncode=0)) as run:
            self.assertTrue(self.launcher.handle_request(request(),'missing'))
            args=run.call_args.args[0]
            self.assertEqual(args,[sys.executable,str(Path(__file__).resolve().parents[1]/'training/run_full.py'),'resume','--private-dir',str(self.root)])
            self.assertFalse(self.launcher.handle_request(request(),'missing'))
            self.assertEqual(run.call_count,1)
        recreated=Launcher(self.root)
        with patch('launcher.subprocess.run') as run:
            self.assertFalse(recreated.handle_request(request(),'missing'))
            run.assert_not_called()
    def test_arbitrary_commands_paths_and_stale_requests_are_ignored(self):
        for invalid in (request(action='stop'),request(command='rm -rf /'),request(path='/tmp/other'),request(at=time.time()-120),request(id='../../escape'),[],None):
            with patch('launcher.subprocess.run') as run:
                self.assertFalse(self.launcher.handle_request(invalid,'missing'))
                run.assert_not_called()
    def test_container_conflict_and_missing_image_do_not_launch(self):
        with patch('launcher.subprocess.run') as run:
            self.launcher.handle_request(request(),'conflict')
            run.assert_not_called()
        self.assertEqual(self.launcher.receipt['error_code'],'ContainerConflict')
        with patch.object(self.launcher,'docker',return_value=SimpleNamespace(returncode=1)),patch('launcher.subprocess.run') as run:
            self.launcher.handle_request(request('b'*32),'missing')
            run.assert_not_called()
        self.assertEqual(self.launcher.receipt['error_code'],'ImageMissing')
    def test_timeout_does_not_replay_a_possibly_successful_start(self):
        with patch.object(self.launcher,'docker',return_value=SimpleNamespace(returncode=0)),patch('launcher.subprocess.run',side_effect=subprocess.TimeoutExpired('fixed',45)):
            self.launcher.handle_request(request(),'missing')
        self.assertEqual(self.launcher.receipt['result'],'failed')
        self.assertFalse(Launcher(self.root).handle_request(request(),'missing'))
    def test_probe_rejects_other_containers(self):
        with patch.object(self.launcher,'docker',side_effect=[SimpleNamespace(returncode=0),SimpleNamespace(returncode=0,stdout='"exited"|null')]):
            self.assertEqual(self.launcher.probe(),'conflict')
    def test_service_has_no_http_listener_and_bounds_writable_paths(self):
        text=unit_text(Path('/home/example/workspace/akubot'),self.root)
        self.assertIn('RestrictAddressFamilies=AF_UNIX',text)
        self.assertIn('ProtectSystem=strict',text)
        self.assertIn('NoNewPrivileges=yes',text)
        self.assertNotIn('docker.sock',text)

    def test_install_falls_back_when_user_service_bus_is_unavailable(self):
        with patch('launcher.TrainingControl.status',return_value={'launcher_online':False}), \
             patch('launcher.subprocess.run',side_effect=subprocess.CalledProcessError(1,'systemctl')), \
             patch('launcher.subprocess.Popen',return_value=SimpleNamespace(pid=123)) as spawn, \
             patch('launcher.wait_for_launcher',return_value=True):
            mode=install_launcher(self.root,self.root/'units/launcher.service')
        self.assertEqual(mode,'background')
        self.assertTrue(spawn.call_args.kwargs['start_new_session'])
        self.assertEqual(read_json(self.root/'training/launcher/background.json')['pid'],123)

    def test_install_does_not_touch_an_already_online_helper(self):
        with patch('launcher.TrainingControl.status',return_value={'launcher_online':True}), \
             patch('launcher.subprocess.run') as run,patch('launcher.subprocess.Popen') as spawn:
            self.assertEqual(install_launcher(self.root,self.root/'units/launcher.service'),'online')
            run.assert_not_called()
            spawn.assert_not_called()
