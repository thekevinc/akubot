"""Small, offline LoRA writing-style experiment. Logs contain metrics only."""
from __future__ import annotations
import argparse, contextlib, json, logging, math, os, random, sys, time
from pathlib import Path
from strength import apply_strength, checked_strength

SYSTEM = "You are Aku, an explicitly labeled AI double in Discord. Write short, natural, casual replies in the owner's style. Do not invent personal facts or claim to be the human owner."
PROMPT = 'Write one short, casual Discord message in your usual style.'


def encode_reply(tokenizer, row, limit=512):
    # Only text is a model target. The source metadata never enters tokenization.
    prefix = [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': PROMPT}]
    before = tokenizer.apply_chat_template(prefix, tokenize=True, add_generation_prompt=True)
    complete = tokenizer.apply_chat_template(prefix + [{'role': 'assistant', 'content': row['text']}], tokenize=True)
    if complete[:len(before)] != before:
        raise ValueError('The chat template does not preserve the response boundary.')
    ids = complete[:limit]
    labels = [-100] * len(before) + ids[len(before):]
    if len(ids) != len(labels) or not any(value != -100 for value in labels):
        raise ValueError('No target tokens remain after truncation.')
    return ids, labels


def main():
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer
    logging.getLogger('transformers').setLevel(logging.ERROR)
    torch.manual_seed(42)
    random.seed(42)
    torch.set_num_threads(4)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, default=Path('/base'))
    parser.add_argument('--data', type=Path, default=Path('/dataset'))
    parser.add_argument('--output', type=Path, default=Path('/output'))
    parser.add_argument('--steps', type=int)
    parser.add_argument('--epochs', type=float, default=1.0)
    parser.add_argument('--batch-size', type=int, default=4)
    parser.add_argument('--accumulation', type=int, default=2)
    parser.add_argument('--learning-rate', type=float, default=1e-4)
    parser.add_argument('--merge-strength', type=float, default=1.0)
    args = parser.parse_args()
    checked_strength(args.merge_strength)
    if (args.steps is not None and not 1 <= args.steps <= 1000000) or not 1 <= args.batch_size <= 64 or not 1 <= args.accumulation <= 16 or not 0 < args.epochs <= 5:
        raise ValueError('Training bounds exceeded.')
    if not torch.cuda.is_available() or torch.cuda.get_device_capability(0) < (8, 0):
        raise ValueError('A compatible CUDA GPU is required.')
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (args.output / 'adapter').exists():
        raise ValueError('Use a fresh output directory.')
    metrics_file = args.output / 'metrics.jsonl'
    start = time.monotonic()
    def emit(**values):
        entry = {'elapsed_seconds': round(time.monotonic() - start, 2), **values}
        with metrics_file.open('a') as stream:
            stream.write(json.dumps(entry, allow_nan=False) + '\n')
        print(json.dumps(entry, allow_nan=False), flush=True)
    tokenizer = AutoTokenizer.from_pretrained(args.base, local_files_only=True, trust_remote_code=False)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    data = {}
    for split in ('train', 'validation'):
        rows = [json.loads(line) for line in (args.data / f'{split}.jsonl').read_text().splitlines() if line.strip()]
        data[split] = [encode_reply(tokenizer, row) for row in rows]
    if args.steps is None:
        args.steps = math.ceil(len(data['train']) * args.epochs / (args.batch_size * args.accumulation))
    emit(stage='loading', train_examples=len(data['train']), validation_examples=len(data['validation']))
    model = AutoModelForCausalLM.from_pretrained(args.base, torch_dtype=torch.bfloat16, local_files_only=True,
                                               trust_remote_code=False, attn_implementation='sdpa', device_map={'': 'cuda'}, low_cpu_mem_usage=True)
    model.config.use_cache = False
    model = get_peft_model(model, LoraConfig(r=8, lora_alpha=16, lora_dropout=0.05, bias='none', task_type='CAUSAL_LM',
                        target_modules=['q_proj','k_proj','v_proj','o_proj','gate_proj','up_proj','down_proj']))
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant': False})
    trainable = sum(parameter.numel() for parameter in model.parameters() if parameter.requires_grad)
    total = sum(parameter.numel() for parameter in model.parameters())
    optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=args.learning_rate, weight_decay=0.01)
    def batch(rows):
        longest = max(len(ids) for ids, _ in rows)
        inputs, labels, masks = [], [], []
        for ids, targets in rows:
            pad = longest - len(ids)
            inputs.append(ids + [tokenizer.pad_token_id] * pad)
            labels.append(targets + [-100] * pad)
            masks.append([1] * len(ids) + [0] * pad)
        return {name: torch.tensor(values, device='cuda') for name, values in [('input_ids', inputs), ('labels', labels), ('attention_mask', masks)]}
    def evaluate(adapter=True):
        model.eval()
        loss_sum = token_sum = 0
        manager = contextlib.nullcontext() if adapter else model.disable_adapter()
        with manager, torch.no_grad(), torch.autocast('cuda', dtype=torch.bfloat16):
            for offset in range(0, len(data['validation']), args.batch_size):
                payload = batch(data['validation'][offset:offset+args.batch_size])
                result = model(**payload)
                tokens = int((payload['labels'][:,1:] != -100).sum())
                loss_sum += float(result.loss) * tokens; token_sum += tokens
        return loss_sum / token_sum
    before_loss = evaluate(adapter=False)
    emit(stage='training', trainable_parameters=trainable, total_parameters=total, base_validation_loss=round(before_loss, 4), total_steps=args.steps)
    model.train()
    rng = random.Random(42)
    positions = list(range(len(data['train'])))
    rng.shuffle(positions)
    cursor = 0
    for step in range(1, args.steps + 1):
        optimizer.zero_grad(set_to_none=True)
        step_loss = 0
        lr = args.learning_rate * min(1.0, step / 10) * max(0.1, 1 - (step - 1) / args.steps)
        for group in optimizer.param_groups: group['lr'] = lr
        for _ in range(args.accumulation):
            chosen = []
            for _ in range(args.batch_size):
                if cursor >= len(positions): rng.shuffle(positions); cursor = 0
                chosen.append(data['train'][positions[cursor]]); cursor += 1
            with torch.autocast('cuda', dtype=torch.bfloat16):
                loss = model(**batch(chosen)).loss / args.accumulation
            if not bool(torch.isfinite(loss)): raise ValueError('Training loss is not finite.')
            loss.backward(); step_loss += float(loss.detach())
        torch.nn.utils.clip_grad_norm_([p for p in model.parameters() if p.requires_grad], 1.0)
        optimizer.step()
        if step == 1 or step % 10 == 0:
            emit(stage='training', step=step, total_steps=args.steps, loss=round(step_loss,4), learning_rate=lr,
                 peak_gpu_gb=round(torch.cuda.max_memory_allocated()/1024**3,2))
    after_loss = evaluate(adapter=True)
    model.save_pretrained(args.output / 'adapter', safe_serialization=True)
    tokenizer.save_pretrained(args.output / 'adapter')
    scaled_layers = apply_strength(model, args.merge_strength)
    scaled_loss = evaluate(adapter=True) if args.merge_strength != 1 else after_loss
    emit(stage='merging', trained_validation_loss=round(after_loss,4), scaled_validation_loss=round(scaled_loss,4), merge_strength=args.merge_strength)
    merged = model.merge_and_unload(safe_merge=True)
    merged.config.use_cache = True
    merged.save_pretrained(args.output / 'merged', safe_serialization=True, max_shard_size='4GB')
    tokenizer.save_pretrained(args.output / 'merged')
    report = {'base_validation_loss': before_loss, 'trained_validation_loss': after_loss, 'steps': args.steps,
              'rank': 8, 'trainable_parameters': trainable, 'total_parameters': total, 'seed': 42,
              'batch_size': args.batch_size, 'accumulation': args.accumulation, 'system_prompt': SYSTEM,
              'training_prompt': PROMPT, 'context_limitations': 'Standalone outgoing messages; no reconstructed conversation.',
              'model_format': 'BF16 LoRA adapter plus merged Safetensors',
              'base_source': json.loads((args.base/'source.json').read_text()),
              'merge_strength': args.merge_strength, 'scaled_validation_loss': scaled_loss, 'scaled_layers': scaled_layers,
              'elapsed_seconds': time.monotonic()-start, 'versions': {'torch':torch.__version__}}
    (args.output/'training-report.json').write_text(json.dumps(report,indent=2)+'\n')
    emit(stage='complete', trained_validation_loss=round(after_loss,4))

if __name__ == '__main__':
    os.umask(0o077)
    try:
        main()
    except KeyboardInterrupt:
        print('Training cancelled. No message text logged.', file=sys.stderr);raise SystemExit(130)
    except Exception as error:
        # Never print raw exceptions, since tokenizers can include input text.
        print(json.dumps({'stage':'failed','error_type':type(error).__name__}), file=sys.stderr);raise SystemExit(1)
