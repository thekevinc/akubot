"""Offline one-epoch LoRA worker, with complete resumable checkpoints."""
from __future__ import annotations
import argparse, contextlib, fcntl, gc, hashlib, json, logging, math, os
from pathlib import Path
import random, shutil, signal, threading, time, uuid
from checkpoints import Checkpoints, digest
from job_state import TrainingControl, atomic_json, read_json


def fingerprint(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class StopRequested(Exception):
    pass


class Worker:
    def __init__(self, base, dataset, output, control, synthetic=False):
        self.base, self.dataset, self.output = base, dataset, output
        self.control = TrainingControl(control)
        self.config = read_json(dataset / 'job.json')
        self.fp = fingerprint(self.config)
        self.synthetic = synthetic
        if bool(self.config.get('synthetic', False)) != synthetic:
            raise ValueError('Synthetic mode must match the prepared job.')
        self.output.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.checkpoints = Checkpoints(output / 'checkpoints', self.fp)
        self.stop = False
        self.closed = threading.Event()
        self.state_lock = threading.Lock()
        self.values = {'state': 'preparing', 'step': 0, 'total_steps': self.config['total_steps'],
                       'train_examples': self.config['train_examples'],
                       'validation_examples': self.config['validation_examples'], 'recent': []}
        self.model = self.optimizer = self.tokenizer = None
        self.progress = None

    def emit(self, state=None, **values):
        with self.state_lock:
            if state:
                self.values['state'] = state
            self.values.update(values)
            self.values['updated_at'] = time.time()
            atomic_json(self.control.root / 'status.json', self.values)

    def heartbeat(self):
        while not self.closed.is_set():
            atomic_json(self.control.root / 'heartbeat.json', {'at': time.time(), 'alive': True})
            self.closed.wait(3)

    def signalled(self, *_):
        self.stop = True

    def wait_before_loading(self):
        if self.control.desired() == 'stopped':
            raise StopRequested()
        while self.control.desired() == 'paused':
            self.emit('paused')
            if self.stop or self.control.desired() == 'stopped':
                raise StopRequested()
            time.sleep(.5)
        if self.stop or self.control.desired() == 'stopped':
            raise StopRequested()

    def prepare(self):
        import torch
        from transformers import AutoTokenizer
        for name, checksum in self.config['data_files'].items():
            if digest(self.dataset / name) != checksum:
                raise ValueError('Prepared data changed. Refusing to change a running job.')
        for name, checksum in self.config.get('base_files', {}).items():
            if digest(self.base / name) != checksum:
                raise ValueError('Base weights changed.')
        self.wait_before_loading()
        if not self.synthetic:
            self.tokenizer = AutoTokenizer.from_pretrained(self.base, local_files_only=True, trust_remote_code=False)
            if self.tokenizer.pad_token_id is None:
                self.tokenizer.pad_token = self.tokenizer.eos_token
        cache = self.output / 'tokens.pt'
        marker = self.output / 'tokens.json'
        if cache.is_file() and marker.is_file():
            info = read_json(marker)
            if info.get('fingerprint') == self.fp and info.get('sha256') == digest(cache):
                data = torch.load(cache, weights_only=True, map_location='cpu')
                self.emit(truncated_messages=info['truncated_messages'], prepared_examples=sum(map(len, data.values())))
                return data
        from train import encode_reply
        data, truncated = {}, 0
        for split in ('train', 'validation'):
            data[split] = []
            with (self.dataset / f'{split}.jsonl').open() as stream:
                for line in stream:
                    if self.stop or self.control.desired() == 'stopped':
                        raise StopRequested()
                    if self.control.desired() == 'paused':
                        self.wait_before_loading()
                        self.emit('preparing')
                    row = json.loads(line)
                    if self.synthetic:
                        ids, labels = row['ids'], row['labels']
                    else:
                        # Count truncation while keeping the full original strings on disk.
                        ids, labels = encode_reply(self.tokenizer, row, limit=10**9)
                        truncated += int(len(ids) > self.config['max_length'])
                        ids, labels = ids[:self.config['max_length']], labels[:self.config['max_length']]
                    if len(ids) != len(labels) or not any(x != -100 for x in labels[1:]):
                        raise ValueError('No assistant target tokens.')
                    data[split].append((ids, labels))
                    if len(data[split]) % 2000 == 0:
                        self.emit('preparing', prepared_examples=sum(map(len, data.values())), truncated_messages=truncated)
            if len(data[split]) != self.config[f'{split}_examples']:
                raise ValueError('Prepared dataset count mismatch.')
        temporary = self.output / ('tokens.' + uuid.uuid4().hex + '.tmp')
        torch.save(data, temporary)
        with temporary.open('rb') as stream:
            os.fsync(stream.fileno())
        temporary.replace(cache)
        atomic_json(marker, {'fingerprint': self.fp, 'sha256': digest(cache), 'truncated_messages': truncated})
        self.emit(truncated_messages=truncated, prepared_examples=sum(map(len, data.values())))
        return data

    def load(self):
        import torch
        from peft import LoraConfig, get_peft_model
        from transformers import AutoModelForCausalLM, LlamaConfig, LlamaForCausalLM
        self.emit('loading')
        torch.manual_seed(self.config['seed'])
        random.seed(self.config['seed'])
        if self.synthetic:
            self.device = 'cpu'
            model = LlamaForCausalLM(LlamaConfig(vocab_size=64, hidden_size=32, intermediate_size=64,
                       num_hidden_layers=1, num_attention_heads=4, num_key_value_heads=2, max_position_embeddings=64))
        else:
            if not torch.cuda.is_available() or torch.cuda.get_device_capability(0) < (8, 0):
                raise ValueError('Compatible CUDA GPU required.')
            self.device = 'cuda'
            # Do not interrupt Ollama. Fail safely if the user has not freed enough VRAM.
            free, _ = torch.cuda.mem_get_info()
            if free < 19 * 1024**3:
                raise MemoryError('Training needs approximately 19 GB free GPU memory.')
            model = AutoModelForCausalLM.from_pretrained(self.base, torch_dtype=torch.bfloat16,
                        local_files_only=True, trust_remote_code=False, attn_implementation='sdpa',
                        device_map={'': 'cuda'}, low_cpu_mem_usage=True)
        model.config.use_cache = False
        self.model = get_peft_model(model, LoraConfig(r=self.config['rank'], lora_alpha=self.config['alpha'],
                    lora_dropout=self.config['dropout'], bias='none', task_type='CAUSAL_LM',
                    target_modules=['q_proj', 'k_proj', 'v_proj', 'o_proj', 'gate_proj', 'up_proj', 'down_proj']))
        self.model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant': False})
        self.optimizer = torch.optim.AdamW([p for p in self.model.parameters() if p.requires_grad],
                         lr=self.config['learning_rate'], weight_decay=.01)
        latest = self.checkpoints.latest()
        if latest:
            self.progress = self.checkpoints.load(latest, self.model, self.optimizer)
            self.emit(last_checkpoint_step=self.progress['step'], last_checkpoint_at=latest.stat().st_mtime)
        elif any(self.checkpoints.root.glob('step-*')):
            raise ValueError('No valid matching checkpoint remains. Preserve the job for recovery.')
        else:
            positions = list(range(self.config['train_examples']))
            random.Random(self.config['seed']).shuffle(positions)
            self.progress = {'step': 0, 'cursor': 0, 'positions': positions, 'active_seconds': 0.,
                             'recent': [], 'best_step': 0, 'best_loss': None, 'base_validation_loss': None}
        self.emit(step=self.progress['step'], active_seconds=self.progress['active_seconds'], recent=self.progress['recent'])

    def unload(self):
        import torch
        self.model = self.optimizer = None
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        self.emit(gpu_gb=0)

    def save(self, phase='checkpointing'):
        self.emit(phase)
        saved = self.checkpoints.save(self.model, self.optimizer, self.progress)
        self.emit(last_checkpoint_step=self.progress['step'], last_checkpoint_at=saved.stat().st_mtime)
        return saved

    def safe_point(self):
        desired = self.control.desired()
        if self.stop or desired in {'paused', 'stopped'}:
            self.save('stopping' if desired == 'stopped' else 'pausing')
            self.unload()
            if self.stop or self.control.desired() == 'stopped':
                raise StopRequested()
            self.wait_before_loading()
            self.load()

    def payload(self, rows):
        import torch
        longest = max(len(ids) for ids, _ in rows)
        pad = 0 if self.synthetic else self.tokenizer.pad_token_id
        result = {'input_ids': [], 'labels': [], 'attention_mask': []}
        for ids, labels in rows:
            n = longest - len(ids)
            result['input_ids'].append(ids + [pad] * n)
            result['labels'].append(labels + [-100] * n)
            result['attention_mask'].append([1] * len(ids) + [0] * n)
        return {k: torch.tensor(v, device=self.device) for k, v in result.items()}

    def autocast(self):
        import torch
        return torch.autocast('cuda', dtype=torch.bfloat16) if self.device == 'cuda' else contextlib.nullcontext()

    def evaluate(self, rows, baseline=False):
        import torch
        self.emit('validating')
        total = tokens = 0
        self.model.eval()
        with (self.model.disable_adapter() if baseline else contextlib.nullcontext()), torch.no_grad(), self.autocast():
            for offset in range(0, len(rows), self.config['batch_size']):
                if self.stop or self.control.desired() in {'paused', 'stopped'}:
                    self.model.train()
                    return None
                payload = self.payload(rows[offset:offset + self.config['batch_size']])
                count = int((payload['labels'][:, 1:] != -100).sum())
                loss = float(self.model(**payload).loss)
                if not math.isfinite(loss):
                    raise ValueError('Non-finite validation loss.')
                total += loss * count
                tokens += count
        self.model.train()
        return total / tokens

    def train(self, data):
        import torch
        self.wait_before_loading()
        self.load()
        self.safe_point()
        validation_sample = data['validation'][:self.config['evaluation_samples']]
        if self.progress['base_validation_loss'] is None:
            while self.progress['base_validation_loss'] is None:
                self.progress['base_validation_loss'] = self.evaluate(validation_sample, baseline=True)
                self.safe_point()
            self.save()
        self.emit(base_validation_loss=self.progress['base_validation_loss'])
        last_save = time.monotonic()
        self.model.train()
        while self.progress['step'] < self.config['total_steps']:
            self.safe_point()
            started = time.monotonic()
            self.emit('training')
            step = self.progress['step'] + 1
            self.optimizer.zero_grad(set_to_none=True)
            warmup = max(1, math.ceil(self.config['total_steps'] * self.config['warmup_ratio']))
            lr = self.config['learning_rate'] * min(1., step / warmup) * max(.1, 1 - (step - 1) / self.config['total_steps'])
            for group in self.optimizer.param_groups:
                group['lr'] = lr
            start = self.progress['cursor']
            end = min(len(data['train']), start + self.config['batch_size'] * self.config['accumulation'])
            indices = self.progress['positions'][start:end]
            if not indices:
                raise ValueError('Sampler exhausted before the planned final step.')
            microbatches = [indices[i:i + self.config['batch_size']] for i in range(0, len(indices), self.config['batch_size'])]
            total_loss = 0.
            for chosen in microbatches:
                with self.autocast():
                    loss = self.model(**self.payload([data['train'][i] for i in chosen])).loss / len(microbatches)
                if not bool(torch.isfinite(loss)):
                    raise ValueError('Non-finite loss.')
                loss.backward()
                total_loss += float(loss.detach())
            torch.nn.utils.clip_grad_norm_([p for p in self.model.parameters() if p.requires_grad], 1.)
            self.optimizer.step()
            # Cursor advances only once its whole optimizer step is committed in memory.
            self.progress['step'], self.progress['cursor'] = step, end
            self.progress['active_seconds'] += time.monotonic() - started
            recent = self.progress['recent']
            if step == 1 or step % self.config['log_every'] == 0:
                recent.append({'step': step, 'loss': round(total_loss, 5)})
                self.progress['recent'] = recent[-60:]
            self.emit('training', step=step, loss=total_loss, active_seconds=self.progress['active_seconds'],
                      eta_seconds=(self.config['total_steps'] - step) * self.progress['active_seconds'] / step,
                      gpu_gb=torch.cuda.memory_allocated() / 1024**3 if self.device == 'cuda' else 0,
                      recent=self.progress['recent'])
            if step % self.config['evaluate_every'] == 0:
                score = self.evaluate(validation_sample)
                if score is not None:
                    self.emit(validation_loss=score)
                    self.progress['recent'].append({'step': step, 'validation_loss': score})
                    self.progress['recent'] = self.progress['recent'][-60:]
                    if self.progress['best_loss'] is None or score < self.progress['best_loss']:
                        self.progress['best_loss'], self.progress['best_step'] = score, step
                    self.save()
                    last_save = time.monotonic()
            if step % self.config['checkpoint_every'] == 0 or time.monotonic() - last_save >= self.config['checkpoint_seconds']:
                self.save()
                last_save = time.monotonic()
        self.save()
        self.safe_point()
        score = None
        while score is None:
            score = self.evaluate(data['validation'])
            self.safe_point()
        self.emit(validation_loss=score)
        self.export(score)

    def export(self, score):
        self.emit('exporting')
        target = self.output / 'merged'
        marker = self.output / 'export.json'
        if not marker.exists():
            # A crash during export leaves the final training checkpoint intact.
            for name in ('adapter', 'merged'):
                path = self.output / name
                if path.exists():
                    path.rename(self.output / (name + '.incomplete.' + uuid.uuid4().hex[:8]))
            self.model.save_pretrained(self.output / 'adapter', safe_serialization=True)
            if self.tokenizer:
                self.tokenizer.save_pretrained(self.output / 'adapter')
            temporary = self.output / ('merged.' + uuid.uuid4().hex + '.tmp')
            merged = self.model.merge_and_unload(safe_merge=True)
            merged.config.use_cache = True
            merged.save_pretrained(temporary, safe_serialization=True, max_shard_size='4GB')
            if self.tokenizer:
                self.tokenizer.save_pretrained(temporary)
            temporary.rename(target)
            report = {'steps': self.progress['step'], 'train_examples': self.config['train_examples'],
                      'validation_examples': self.config['validation_examples'], 'trained_validation_loss': score,
                      'base_validation_loss': self.progress['base_validation_loss'], 'merge_strength': 1.0,
                      'rank': self.config['rank'], 'base_source': self.config.get('base_source'),
                      'fingerprint': self.fp, 'active_seconds': self.progress['active_seconds'],
                      'context_limitations': 'Standalone owner messages; no reconstructed conversation.',
                      'best_checkpoint_step': self.progress['best_step']}
            atomic_json(self.output / 'training-report.json', report)
            atomic_json(marker, {'fingerprint': self.fp, 'step': self.progress['step']})
        self.unload()
        self.emit('completed', step=self.config['total_steps'], eta_seconds=0)

    def run(self):
        import torch
        torch.set_num_threads(4 if not self.synthetic else 1)
        logging.getLogger('transformers').setLevel(logging.ERROR)
        lock = (self.output / 'worker.lock').open('a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self.signalled)
        self.emit('preparing')
        thread = threading.Thread(target=self.heartbeat, daemon=True)
        thread.start()
        try:
            if (self.output / 'export.json').is_file():
                if read_json(self.output / 'export.json').get('fingerprint') != self.fp:
                    raise ValueError('Completed output belongs to another job.')
                self.emit('completed', step=self.config['total_steps'], eta_seconds=0)
                return
            data = self.prepare()
            self.train(data)
        except StopRequested:
            self.emit('stopped' if self.control.desired() == 'stopped' else 'interrupted')
        except Exception as error:
            self.emit('failed', error_code=type(error).__name__)
            raise
        finally:
            self.closed.set()
            thread.join(timeout=5)
            atomic_json(self.control.root / 'heartbeat.json', {'at': time.time(), 'alive': False})
            lock.close()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('base', 'dataset', 'output', 'control'):
        parser.add_argument('--' + name, type=Path, default=Path('/' + name))
    parser.add_argument('--synthetic', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    Worker(args.base, args.dataset, args.output, args.control, args.synthetic).run()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Exception messages and tracebacks may contain personal training strings.
        print(json.dumps({'state': 'failed', 'error_code': type(error).__name__}), flush=True)
        raise SystemExit(1)
