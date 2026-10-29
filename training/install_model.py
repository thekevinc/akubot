"""Install the baseline and trial into the private Ollama cache, offline."""
from __future__ import annotations
import argparse,json,os,re,subprocess,sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from akubot.model_catalog import ModelCatalog

def register_models(root,trial,model,base_model):
    source=json.loads((root/'training/base/source.json').read_text())
    base_id='hf:'+source['repository']+'@'+source['revision']
    base_label=source['repository'].split('/')[-1].replace('-',' ')
    suffix=model.rsplit('v',1)[-1]
    label='Aku style '+suffix if suffix.isdecimal() else model
    catalog=ModelCatalog(root/'data/model-catalog.json')
    base=next((entry for entry in catalog.read()['bases'] if entry['id']==base_id),None)
    catalog.register(
        base or {'id':base_id,'label':base_label,'model':base_model+':latest'},
        {'id':model,'label':label,'base_id':base_id,'model':model+':latest'})

IMAGE='ollama/ollama:0.40.12@sha256:20d1bbfbabe1c97ba4b16301f08a20d8484bcf77047742a3cb71c613d8e12d0b'
TEMPLATE='''TEMPLATE """{{- if .System }}<|im_start|>system
{{ .System }}<|im_end|>
{{ end }}{{- range .Messages }}<|im_start|>{{ .Role }}
{{ .Content }}<|im_end|>
{{ end }}<|im_start|>assistant
"""
PARAMETER stop "<|im_end|>"
PARAMETER stop "<|im_start|>"
PARAMETER temperature 0.8
PARAMETER num_ctx 4096
'''
SCRIPT='''umask 077
ollama serve >/tmp/server.log 2>&1 &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true' EXIT INT TERM
attempt=0
until ollama list >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ] || ! kill -0 "$server_pid" 2>/dev/null; then
    echo "Temporary importer could not start."; exit 1
  fi
  sleep 1
done
if ! ollama create "$BASE_NAME" -f /import/base.Modelfile > /tmp/base-import.log 2>&1; then
  echo "Baseline conversion failed."; tail -c 1500 /tmp/base-import.log; exit 1
fi
if ! ollama create "$TRIAL_NAME" -f /import/trained.Modelfile > /tmp/trial-import.log 2>&1; then
  echo "Trained conversion failed."; tail -c 1500 /tmp/trial-import.log; exit 1
fi
echo "Baseline and trained model installed locally."
'''

def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private-dir',type=Path,required=True)
    parser.add_argument('--trial',default='style-trial-v1')
    parser.add_argument('--name',default='aku-style-v1')
    parser.add_argument('--base-name',default='aku-style-base')
    args=parser.parse_args()
    for value in [args.trial,args.name,args.base_name]:
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,70}',value): raise ValueError('Invalid trial/model name.')
    root=args.private_dir.resolve();output=root/'training'/args.trial/'output'
    if not (output/'training-report.json').is_file() or not (output/'gguf/trained.gguf').is_file():
        raise ValueError('Finish training before installing the model.')
    config=output/'install';config.mkdir(mode=0o700,exist_ok=True)
    for name,path in [('base','/gguf/base.gguf'),('trained','/gguf/trained.gguf')]:
        (config/f'{name}.Modelfile').write_text(f'FROM {path}\n'+TEMPLATE)
    for name in [args.name,args.base_name]:
        if (root/'models/manifests/registry.ollama.ai/library'/name/'latest').exists():
            raise ValueError('Model name already exists. Choose a fresh name.')
    command=['docker','run','--rm','--name','akubot-style-import','--user',f'{os.getuid()}:{os.getgid()}',
             '--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
             '--tmpfs','/tmp:size=512m,mode=1777,nosuid','--memory','8g','--cpus','4','--pids-limit','128','--log-driver','none',
             '--env','HOME=/tmp','--env','OLLAMA_HOST=127.0.0.1:11434','--env','OLLAMA_MODELS=/models',
             '--env','OLLAMA_NO_CLOUD=1','--env','OLLAMA_NOPRUNE=1',
             '--env',f'BASE_NAME={args.base_name}','--env',f'TRIAL_NAME={args.name}']
    for source,target,readonly in [(root/'models','/models',False),(output/'gguf','/gguf',True),(config,'/import',True)]:
        command+=['--mount',f'type=bind,src={source},dst={target}'+(',readonly' if readonly else '')]
    command+=['--entrypoint','/bin/sh',IMAGE,'-ec',SCRIPT]
    result=subprocess.run(command)
    if result.returncode: return result.returncode
    register_models(root,args.trial,args.name,args.base_name)
    report={'trained_model':args.name+':latest','baseline_model':args.base_name+':latest','imported_locally':True,
            'training_report':str(output/'training-report.json'),'live_bot_restarted':False}
    (output/'install-report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'trained_model':report['trained_model'],'baseline_model':report['baseline_model']}))
    return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'install_failed':True,'error_type':type(error).__name__}),file=sys.stderr);raise SystemExit(1)
