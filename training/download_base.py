from pathlib import Path
import argparse,urllib.request,json,hashlib,os,time
os.umask(0o077)
def sha256_file(path):
 digest=hashlib.sha256()
 with path.open('rb') as stream:
  while chunk:=stream.read(4*1024*1024):digest.update(chunk)
 return digest.hexdigest()

repo='HuggingFaceTB/SmolLM2-1.7B-Instruct';rev='31b70e2e869a7173562077fd711b654946d38674'
parser=argparse.ArgumentParser(description='Download the pinned public base model; no Discord data is read.')
parser.add_argument('--destination',type=Path,required=True)
root=parser.parse_args().destination
root.mkdir(parents=True,mode=0o700,exist_ok=True)
with urllib.request.urlopen(f'https://huggingface.co/api/models/{repo}/tree/{rev}',timeout=30) as r:files=json.load(r)
wanted={'config.json','generation_config.json','model.safetensors','special_tokens_map.json','tokenizer.json','tokenizer_config.json','vocab.json','merges.txt'}
for item in files:
 name=item['path']
 if name not in wanted:continue
 target=root/name;size=item['size'];expected=item.get('lfs',{}).get('oid')
 if target.exists() and target.stat().st_size==size and (not expected or sha256_file(target)==expected):
  print('Already downloaded:',name,flush=True);continue
 temporary=target.with_suffix(target.suffix+'.partial');start=time.monotonic()
 with urllib.request.urlopen(f'https://huggingface.co/{repo}/resolve/{rev}/{name}',timeout=120) as incoming,temporary.open('wb') as output:
  digest=hashlib.sha256();received=0;last=time.monotonic()
  while True:
   chunk=incoming.read(4*1024*1024)
   if not chunk:break
   output.write(chunk);digest.update(chunk);received+=len(chunk)
   if time.monotonic()-last>20:
    print(name,round(received/1024**3,2),'GB',flush=True);last=time.monotonic()
 if received!=size or expected and digest.hexdigest()!=expected:raise SystemExit('Public model checksum mismatch.')
 temporary.replace(target);target.chmod(0o600)
 print('Downloaded:',name,'bytes=',received,flush=True)
(root/'source.json').write_text(json.dumps({'repository':repo,'revision':rev},indent=2)+'\n')
