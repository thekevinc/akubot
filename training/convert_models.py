"""Convert the original and trained weights to Q8 GGUF without network access."""
from pathlib import Path
import argparse,json,os,subprocess,sys
REVISION='2e0d86bebc210947680e8f0d67daa79f3260c106'

def main():
 os.umask(0o077)
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--private-dir',type=Path,required=True)
 p.add_argument('--trial',default='style-trial-v1')
 a=p.parse_args()
 if not a.trial.replace('-','').replace('_','').isalnum():raise ValueError()
 root=a.private_dir.resolve();tools=root/'training/tools/llama.cpp';output=root/'training'/a.trial/'output'
 if not tools.exists():
  subprocess.run(['git','clone','--depth','1','https://github.com/ggml-org/llama.cpp.git',str(tools)],check=True)
 current=subprocess.check_output(['git','-C',str(tools),'rev-parse','HEAD'],text=True).strip()
 if current!=REVISION:
  subprocess.run(['git','-C',str(tools),'fetch','--depth','1','origin',REVISION],check=True)
  subprocess.run(['git','-C',str(tools),'checkout','--detach',REVISION],check=True)
 gguf=output/'gguf';gguf.mkdir(mode=0o700,exist_ok=True)
 for label,source in [('base',root/'training/base'),('trained',output/'merged')]:
  if (gguf/f'{label}.gguf').exists():raise ValueError('GGUF file already exists; choose a new trial.')
  command=['docker','run','--rm','--user',f'{os.getuid()}:{os.getgid()}','--network','none','--read-only',
   '--cap-drop','ALL','--security-opt','no-new-privileges','--tmpfs','/tmp:size=256m,mode=1777,nosuid',
   '--memory','10g','--cpus','4','--pids-limit','128','--log-driver','none',
   '--mount',f'type=bind,src={tools},dst=/tools,readonly',
   '--mount',f'type=bind,src={source},dst=/weights,readonly',
   '--mount',f'type=bind,src={gguf},dst=/output',
   '--entrypoint','python','akubot-trainer:local','/tools/convert_hf_to_gguf.py','/weights',
   '--outfile',f'/output/{label}.gguf','--outtype','q8_0']
  with (output/f'{label}-conversion.log').open('w') as log:
   result=subprocess.run(command,stdout=log,stderr=log)
  if result.returncode:
   print(json.dumps({'conversion_failed':label,'private_log':str(output/f'{label}-conversion.log')}))
   return 1
  print(json.dumps({'converted':label,'bytes':(gguf/f'{label}.gguf').stat().st_size}),flush=True)
 (output/'conversion-report.json').write_text(json.dumps({'llama_cpp_revision':REVISION,'format':'Q8_0 GGUF'},indent=2)+'\n')
 return 0
if __name__=='__main__':
 try:raise SystemExit(main())
 except Exception as e:print(json.dumps({'conversion_failed':True,'error_type':type(e).__name__}));raise SystemExit(1)
