"""Prepare, launch, and recover the private full-dataset Llama training job."""
from __future__ import annotations
import argparse, hashlib, json, math, os
from pathlib import Path
import shutil, subprocess, sys, time, uuid

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from akubot.training_state import RUN_ID, TrainingControl, atomic_json, read_json
sys.path.insert(0, str(REPO / 'training'))
from checkpoints import digest

IMAGE = 'akubot-full-trainer:local'
CONTAINER = 'akubot-llama-full-training'
SOURCE = {'repository': 'unsloth/Meta-Llama-3.1-8B-Instruct',
          'revision': 'a2856192dd7c25b842431f39c179a6c2c2f627d1'}


def checked(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def container_state():
    result = subprocess.run(['docker', 'inspect', '--format', '{{json .State.Status}}|{{json (index .Config.Labels "io.akubot.training.run")}}', CONTAINER],
                            capture_output=True, text=True)
    if result.returncode:
        return None
    status, label = result.stdout.strip().split('|', 1)
    if json.loads(label) != RUN_ID:
        raise ValueError('A different container uses the training name. It was left unchanged.')
    return json.loads(status)


def prepare(root):
    run = root / 'training' / RUN_ID
    mailbox = root / 'data/training-control' / RUN_ID
    dataset = run / 'dataset'
    if (dataset / 'job.json').exists():
        config = read_json(dataset / 'job.json')
        for name, checksum in config['data_files'].items():
            if digest(dataset / name) != checksum:
                raise ValueError('Prepared dataset changed; preserve it for recovery.')
        print('Job already prepared. No training started.')
        return config
    if run.exists() and any(run.iterdir()):
        raise ValueError('Incomplete preparation exists. Preserve it and inspect before preparing again.')
    source = root / 'training/style-full-v1/dataset'
    base = root / 'training/base-llama31'
    if read_json(base / 'source.json') != SOURCE:
        raise ValueError('Expected pinned Llama base revision is not available.')
    report = read_json(source / 'dataset-report.json')
    policy = read_json(root / 'config/channel-policy.json')
    excluded = policy.get('excluded_guild_ids')
    if not isinstance(excluded, list) or not excluded or not all(isinstance(x, str) and x.isdecimal() for x in excluded):
        raise ValueError('Private server exclusion policy is required.')
    if not set(excluded) <= set(report.get('excluded_guild_ids', [])):
        raise ValueError('The dataset must retain every excluded server.')
    run.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = run / ('dataset.' + uuid.uuid4().hex + '.tmp')
    temporary.mkdir(mode=0o700)
    counts, hashes, seen = {}, {}, set()
    try:
        for split in ('train', 'validation'):
            path = temporary / f'{split}.jsonl'
            counts[split] = 0
            with (source / f'{split}.jsonl').open() as incoming, path.open('x') as outgoing:
                for line in incoming:
                    row = json.loads(line)
                    guild = str(row.get('source', {}).get('guild_id', ''))
                    if not guild.isdecimal() or int(guild) == 0 or guild in excluded:
                        raise ValueError('Excluded or unscoped source in dataset.')
                    if not isinstance(row.get('text'), str) or not row['text'].strip():
                        raise ValueError('Invalid target message.')
                    key = hashlib.sha256(row['text'].casefold().encode()).digest()
                    if key in seen:
                        raise ValueError('Duplicate or overlapping train/validation message.')
                    seen.add(key)
                    outgoing.write(line if line.endswith('\n') else line + '\n')
                    counts[split] += 1
                outgoing.flush()
                os.fsync(outgoing.fileno())
            hashes[path.name] = digest(path)
            if counts[split] != report['counts'][f'{split}_examples']:
                raise ValueError('Source dataset count mismatch.')
        if not counts['train'] or not counts['validation']:
            raise ValueError('Empty dataset.')
        print('Checking local base weights and recording recovery fingerprints...', flush=True)
        base_files = {p.name: digest(p) for p in sorted(base.iterdir())
                      if p.is_file() and (p.suffix in {'.json', '.safetensors', '.model'} or p.name.startswith('tokenizer'))}
        if not any(name.endswith('.safetensors') for name in base_files):
            raise ValueError('Base weights are missing.')
        config = {'version': 1, 'run_id': RUN_ID, 'base_source': SOURCE, 'base_files': base_files,
                  'data_files': hashes, 'train_examples': counts['train'], 'validation_examples': counts['validation'],
                  'total_steps': math.ceil(counts['train'] / 8), 'batch_size': 2, 'accumulation': 4,
                  'seed': 42, 'max_length': 512, 'rank': 8, 'alpha': 16, 'dropout': .05,
                  'learning_rate': 2e-5, 'warmup_ratio': .03, 'checkpoint_every': 250,
                  'checkpoint_seconds': 300, 'evaluate_every': 2000, 'evaluation_samples': 256,
                  'log_every': 10, 'epochs': 1, 'excluded_guild_ids': sorted(excluded), 'dms_included': False,
                  'target_format': 'Exact message text; outer metadata excluded',
                  'training_prompt_version': 1}
        atomic_json(temporary / 'job.json', config)
        shutil.copyfile(source / 'dataset-report.json', temporary / 'dataset-report.json')
        for file in temporary.iterdir():
            file.chmod(0o600)
        temporary.rename(dataset)
        (run / 'output').mkdir(mode=0o700)
        mailbox.mkdir(mode=0o700, parents=True, exist_ok=True)
        TrainingControl(mailbox).set_desired('paused')
        atomic_json(mailbox / 'status.json', {'state': 'ready', 'step': 0, 'total_steps': config['total_steps'],
                    'train_examples': counts['train'], 'validation_examples': counts['validation'], 'updated_at': time.time()})
        print(json.dumps({'prepared': RUN_ID, **counts, 'total_steps': config['total_steps'], 'training_started': False}))
        return config
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def start(root, recover=False):
    run = root / 'training' / RUN_ID
    mailbox = root / 'data/training-control' / RUN_ID
    if not (run / 'dataset/job.json').is_file():
        raise ValueError('Run prepare first.')
    control = TrainingControl(mailbox)
    if control.status()['state'] == 'completed':
        print('Training already completed. Existing output was retained.')
        return
    state = container_state()
    if state in {'running', 'restarting', 'paused'}:
        if not recover:
            raise ValueError('Worker already exists. Use resume or pause.')
        if state != 'running':
            raise ValueError('Worker is not ready. Check Docker before recovering.')
        control.set_desired('running')
        print('Resume requested. The dashboard will show loading, then training.')
        return
    if state:
        checked(['docker', 'rm', CONTAINER], stdout=subprocess.DEVNULL)
    if subprocess.run(['docker', 'image', 'inspect', IMAGE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        raise ValueError('Build the training image first: python3 training/run_full.py build')
    # A clean Docker restart is explicitly recovered, never an automatic restart loop.
    control.set_desired('running')
    command = ['docker', 'run', '-d', '--name', CONTAINER, '--user', f'{os.getuid()}:{os.getgid()}',
               '--label', 'io.akubot.training.run=' + RUN_ID,
               '--network', 'none', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
               '--init', '--restart', 'no', '--stop-timeout', '180', '--gpus', '1',
               '--tmpfs', '/tmp:size=1g,mode=1777,nosuid', '--shm-size', '512m', '--memory', '12g',
               '--cpus', '8', '--pids-limit', '256', '--log-driver', 'none',
               '--mount', f'type=bind,src={root / "training/base-llama31"},dst=/base,readonly',
               '--mount', f'type=bind,src={run / "dataset"},dst=/dataset,readonly',
               '--mount', f'type=bind,src={run / "output"},dst=/output',
               '--mount', f'type=bind,src={mailbox},dst=/control', IMAGE]
    checked(command, stdout=subprocess.DEVNULL)
    print('Training worker launched in the background. You can close this terminal.')
    print('Progress and Pause/Resume: http://localhost:8765/#training')
    print('If Docker or WSL stops, run: python3 training/run_full.py resume')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'build', 'start', 'pause', 'resume', 'stop', 'status'])
    parser.add_argument('--private-dir', type=Path, default=Path.home() / '.local/share/akubot')
    args = parser.parse_args()
    root = args.private_dir.expanduser().resolve()
    if str(root).startswith('/mnt/'):
        raise ValueError('Keep training data on the WSL filesystem.')
    if args.action == 'prepare':
        prepare(root)
    elif args.action == 'build':
        checked(['docker', 'build', '-f', 'training/Dockerfile.full', '-t', IMAGE, '.'], cwd=REPO)
    elif args.action in {'start', 'resume'}:
        start(root, recover=args.action == 'resume')
    else:
        control = TrainingControl(root / 'data/training-control' / RUN_ID)
        if args.action in {'pause', 'stop'}:
            control.request(args.action)
            print('Request saved. Wait for Paused or Stopped in the dashboard before stopping Docker.')
        else:
            result = control.status()
            result['container_state'] = container_state()
            print(json.dumps(result, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        # Do not print dataset records or raw command exceptions.
        print('Could not complete the training command (' + type(error).__name__ + ').', file=sys.stderr)
        if isinstance(error, ValueError):
            print(str(error), file=sys.stderr)
        raise SystemExit(1)
