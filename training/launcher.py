"""WSL-only helper. Accepts one fixed start request, with no HTTP or shell input."""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from akubot.training_state import RUN_ID, atomic_json, read_json, valid_launch_request
from run_full import CONTAINER, IMAGE


class LauncherFailure(Exception):
    pass


class Launcher:
    def __init__(self, private_dir):
        self.root = private_dir.resolve()
        self.mailbox = self.root / 'data/training-control' / RUN_ID
        self.private = self.root / 'training/launcher'
        self.private.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.stopping = False
        self.receipt = {}
        with contextlib.suppress(OSError, ValueError, TypeError):
            value = read_json(self.private / 'receipt.json')
            if isinstance(value, dict):
                self.receipt = value

    def docker(self, *args):
        return subprocess.run(['docker', *args], capture_output=True, text=True, timeout=10)

    def probe(self):
        if self.docker('info', '--format', '{{.ServerVersion}}').returncode:
            raise LauncherFailure('DockerUnavailable')
        result = self.docker('inspect', '--format', '{{json .State.Status}}|{{json (index .Config.Labels "io.akubot.training.run")}}', CONTAINER)
        if result.returncode:
            return 'missing'
        status, label = result.stdout.strip().split('|', 1)
        if json.loads(label) != RUN_ID:
            return 'conflict'
        state = json.loads(status)
        return state if state in {'running', 'exited', 'dead', 'created', 'paused', 'restarting', 'removing'} else 'unknown'

    def publish(self, state='unknown', error=None, alive=True):
        value = {'version': 1, 'at': time.time(), 'alive': alive, 'worker_state': state,
                 'job_prepared': (self.root / 'training' / RUN_ID / 'dataset/job.json').is_file(),
                 'last_request_id': self.receipt.get('id'), 'last_result': self.receipt.get('result')}
        if error or self.receipt.get('error_code'):
            value['error_code'] = error or self.receipt['error_code']
        atomic_json(self.mailbox / 'launcher.json', value)

    def handle_request(self, request, state):
        if not valid_launch_request(request, time.time()) or request['id'] == self.receipt.get('id'):
            return False
        # Persist acceptance before invoking Docker. A helper crash cannot replay a start.
        self.receipt = {'id': request['id'], 'result': 'accepted'}
        atomic_json(self.private / 'receipt.json', self.receipt)
        self.publish(state)
        try:
            if not (self.root / 'training' / RUN_ID / 'dataset/job.json').is_file():
                raise LauncherFailure('JobMissing')
            if state not in {'running', 'exited', 'dead', 'created', 'missing'}:
                raise LauncherFailure('ContainerConflict')
            if self.docker('image', 'inspect', IMAGE).returncode:
                raise LauncherFailure('ImageMissing')
            # All executable names, arguments, container settings, and mount paths
            # are trusted local code. Browser requests cannot supply any of them.
            result = subprocess.run([sys.executable, str(REPO / 'training/run_full.py'), 'resume',
                                     '--private-dir', str(self.root)], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, timeout=45)
            if result.returncode:
                raise LauncherFailure('LaunchFailed')
            self.receipt['result'] = 'started'
        except LauncherFailure as error:
            self.receipt.update(result='failed', error_code=str(error))
        except (OSError, ValueError, subprocess.SubprocessError):
            self.receipt.update(result='failed', error_code='LaunchFailed')
        atomic_json(self.private / 'receipt.json', self.receipt)
        return True

    def tick(self):
        try:
            state = self.probe()
        except (OSError, ValueError, subprocess.SubprocessError, LauncherFailure):
            self.publish(error='DockerUnavailable')
            return
        request = None
        with contextlib.suppress(OSError, ValueError, TypeError):
            request = read_json(self.mailbox / 'launch-request.json')
        if self.handle_request(request, state):
            try:
                state = self.probe()
            except (OSError, ValueError, subprocess.SubprocessError, LauncherFailure):
                state = 'unknown'
        self.publish(state)

    def run(self):
        lock = (self.private / 'launcher.lock').open('a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def stop(*_):
            self.stopping = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            while not self.stopping:
                self.tick()
                for _ in range(20):
                    if self.stopping:
                        break
                    time.sleep(.1)
        finally:
            self.publish(alive=False)
            lock.close()


def unit_text(repo, root):
    def quoted(value):
        return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
    return f'''[Unit]
Description=Akubot training launcher (fixed local job only)

[Service]
Type=simple
ExecStart=/usr/bin/python3 {quoted(repo / 'training/launcher.py')} --private-dir {quoted(root)}
Restart=on-failure
RestartSec=5
UMask=0077
Environment=PYTHONDONTWRITEBYTECODE=1
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={quoted(root / 'training/launcher')} {quoted(root / 'data/training-control' / RUN_ID)}
RestrictAddressFamilies=AF_UNIX
StandardOutput=null
StandardError=null

[Install]
WantedBy=default.target
'''


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private-dir', type=Path, default=Path.home() / '.local/share/akubot')
    parser.add_argument('--install', action='store_true', help='Install and enable the user service. Does not start training.')
    args = parser.parse_args()
    root = args.private_dir.expanduser().resolve()
    if str(root).startswith('/mnt/'):
        raise ValueError('Private training storage must stay in WSL.')
    if args.install:
        (root / 'training/launcher').mkdir(mode=0o700, parents=True, exist_ok=True)
        (root / 'data/training-control' / RUN_ID).mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = Path.home() / '.config/systemd/user/akubot-training-launcher.service'
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.write_text(unit_text(REPO, root))
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', '--user', 'enable', '--now', 'akubot-training-launcher.service'], check=True)
        print('Local launcher enabled. Training has not been started.')
    else:
        Launcher(root).run()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'launcher_failed': type(error).__name__}))
        raise SystemExit(1)
