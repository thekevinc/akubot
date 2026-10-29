"""Fixed training commands and sanitized status, shared through private files."""
from __future__ import annotations
import json
import math
import os
from pathlib import Path
import time
import uuid

RUN_ID = 'llama-full-v1'
PHASES = {'ready', 'preparing', 'loading', 'training', 'checkpointing', 'pausing', 'paused',
          'validating', 'exporting', 'completed', 'failed', 'interrupted', 'stopping', 'stopped'}
NUMERIC = {'step', 'total_steps', 'train_examples', 'validation_examples', 'prepared_examples',
           'loss', 'validation_loss', 'base_validation_loss', 'active_seconds', 'eta_seconds',
           'last_checkpoint_step', 'last_checkpoint_at', 'gpu_gb', 'truncated_messages', 'updated_at'}
LAUNCH_ERRORS = {'DockerUnavailable', 'ImageMissing', 'LaunchFailed', 'ContainerConflict', 'JobMissing'}


def atomic_json(path: Path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('Symlinks are not permitted.')
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path, limit=65536):
    if path.is_symlink() or path.stat().st_size > limit:
        raise ValueError('Invalid training state file.')
    with path.open() as stream:
        return json.load(stream)


def fresh(value, now, seconds=30):
    return type(value) in {int, float} and math.isfinite(value) and -5 <= now - value <= seconds


def valid_launch_request(data, now):
    return (isinstance(data, dict) and set(data) == {'version', 'id', 'action', 'at'}
            and data['version'] == 1 and data['action'] == 'start'
            and isinstance(data['id'], str) and len(data['id']) == 32
            and all(c in '0123456789abcdef' for c in data['id']) and fresh(data['at'], now, 60))


class TrainingControlError(ValueError):
    pass


class TrainingControl:
    def __init__(self, root: Path):
        self.root = root

    def desired(self):
        try:
            data = read_json(self.root / 'command.json')
            if data.get('version') == 1 and data.get('desired') in {'running', 'paused', 'stopped'}:
                return data['desired']
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        return 'paused'

    def status(self, now=None):
        now = time.time() if now is None else now
        result = {'run_id': RUN_ID, 'model_label': 'Llama 3.1 8B', 'state': 'not_started',
                  'worker_online': False, 'step': 0, 'total_steps': 0, 'recent': [],
                  'launcher_online': False, 'launch_pending': False, 'container_running': False}
        try:
            data = read_json(self.root / 'status.json')
            if not isinstance(data, dict) or data.get('state') not in PHASES:
                raise ValueError()
            result['state'] = data['state']
            for key in NUMERIC:
                value = data.get(key)
                if type(value) in {int, float} and math.isfinite(value) and value >= 0:
                    result[key] = value
            history = data.get('recent', [])
            if isinstance(history, list):
                result['recent'] = [{k: v for k, v in item.items()
                                     if k in {'step', 'loss', 'validation_loss'}
                                     and type(v) in {int, float} and math.isfinite(v) and v >= 0}
                                    for item in history[-60:] if isinstance(item, dict)]
            if isinstance(data.get('error_code'), str) and data['error_code'].isalnum() and len(data['error_code']) <= 80:
                result['error_code'] = data['error_code']
            heartbeat = read_json(self.root / 'heartbeat.json')
            result['worker_online'] = bool(fresh(heartbeat.get('at'), now)
                and heartbeat.get('alive') is True
                and data['state'] not in {'completed', 'failed', 'interrupted', 'ready', 'stopped'})
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError):
            result['state'] = 'unavailable'
        launcher = {}
        try:
            launcher = read_json(self.root / 'launcher.json')
            result['launcher_online'] = bool(launcher.get('version') == 1
                and fresh(launcher.get('at'), now, 15) and launcher.get('alive') is True)
            result['container_running'] = bool(result['launcher_online'] and launcher.get('worker_state') == 'running')
            if result['launcher_online'] and launcher.get('error_code') in LAUNCH_ERRORS:
                result['launcher_error'] = launcher['error_code']
            if result['launcher_online'] and launcher.get('worker_state') in {'missing', 'exited', 'dead', 'created', 'conflict'}:
                result['worker_online'] = False
            request = read_json(self.root / 'launch-request.json')
            if valid_launch_request(request, now):
                acknowledged = launcher.get('last_request_id') == request['id']
                result['launch_pending'] = not acknowledged or launcher.get('last_result') == 'accepted'
                if acknowledged and launcher.get('last_result') == 'failed' and launcher.get('error_code') in LAUNCH_ERRORS:
                    result['launcher_error'] = launcher['error_code']
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        result['desired'] = self.desired()
        stopping = result['desired'] == 'stopped'
        busy = result['worker_online'] or result['container_running']
        result['can_start'] = bool(result['launcher_online'] and launcher.get('job_prepared') is True
            and launcher.get('worker_state') in {'missing', 'exited', 'dead', 'created'}
            and not busy and not result['launch_pending'] and result['state'] != 'completed')
        result['can_pause'] = bool(result['worker_online'] and result['state'] not in {'paused', 'pausing', 'exporting', 'stopping'}
                                  and result['desired'] == 'running')
        result['can_resume'] = bool(result['worker_online'] and not stopping
                                   and (result['state'] == 'paused' or result['desired'] == 'paused'))
        result['can_stop'] = bool(busy and not stopping and result['state'] not in {'completed', 'exporting', 'stopped'})
        return result

    def request(self, action):
        if action not in {'start', 'stop', 'pause', 'resume'}:
            raise TrainingControlError('Choose Start, Stop, Pause, or Resume.')
        state = self.status()
        if not state['can_' + action]:
            if action == 'start':
                raise TrainingControlError('Starting is unavailable. Check that the local launcher is online and the worker is stopped.')
            if action == 'stop':
                raise TrainingControlError('The worker is already stopped, stopping, or finishing its export.')
            if not state['worker_online']:
                raise TrainingControlError('Use Start training to recover a stopped worker.')
            raise TrainingControlError('Training is already running, paused, or stopping. Check its current status.')
        if action == 'start':
            atomic_json(self.root / 'launch-request.json', {'version': 1, 'id': uuid.uuid4().hex,
                                                          'action': 'start', 'at': time.time()})
        else:
            self.set_desired({'pause': 'paused', 'resume': 'running', 'stop': 'stopped'}[action])
        return self.status()

    def set_desired(self, value):
        if value not in {'running', 'paused', 'stopped'}:
            raise ValueError('Unknown training command.')
        atomic_json(self.root / 'command.json', {'version': 1, 'desired': value, 'at': time.time()})
