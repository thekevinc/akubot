"""Re-export an existing adapter at a chosen strength, offline."""
import argparse,json,logging,os,sys,time
from pathlib import Path
from strength import apply_strength,checked_strength

def main():
    import torch
    from transformers import AutoModelForCausalLM,AutoTokenizer
    from peft import PeftModel
    os.umask(0o077);torch.set_num_threads(4)
    logging.getLogger('transformers').setLevel(logging.ERROR)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base',type=Path,default=Path('/base'))
    parser.add_argument('--adapter',type=Path,default=Path('/adapter'))
    parser.add_argument('--output',type=Path,default=Path('/output'))
    parser.add_argument('--strength',type=float,default=1.5)
    args=parser.parse_args();strength=checked_strength(args.strength)
    if (args.output/'merged').exists():raise ValueError('Use a fresh output directory.')
    args.output.mkdir(mode=0o700,parents=True,exist_ok=True)
    started=time.monotonic()
    model=AutoModelForCausalLM.from_pretrained(args.base,torch_dtype=torch.bfloat16,local_files_only=True,
        trust_remote_code=False,attn_implementation='sdpa',device_map={'':'cuda'},low_cpu_mem_usage=True)
    model=PeftModel.from_pretrained(model,args.adapter,local_files_only=True,is_trainable=False)
    count=apply_strength(model,strength)
    merged=model.merge_and_unload(safe_merge=True)
    merged.save_pretrained(args.output/'merged',safe_serialization=True,max_shard_size='4GB')
    tokenizer=AutoTokenizer.from_pretrained(args.base,local_files_only=True,trust_remote_code=False)
    tokenizer.save_pretrained(args.output/'merged')
    source=json.loads((args.base/'source.json').read_text())
    report={'base_source':source,'merge_strength':strength,'scaled_layers':count,'reused_existing_adapter':True,
            'context_limitations':'Original random sample; standalone outgoing messages.',
            'elapsed_seconds':time.monotonic()-started}
    (args.output/'training-report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'stage':'rebuilt','strength':strength,'scaled_layers':count,'seconds':round(report['elapsed_seconds'],2)}))

if __name__=='__main__':
    try:main()
    except Exception as error:
        print(json.dumps({'stage':'failed','error_type':type(error).__name__}));raise SystemExit(1)
