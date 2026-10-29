"""Download public base weights only. Private training data is never read."""
from pathlib import Path
import argparse,concurrent.futures,hashlib,json,os,re,time,urllib.request

def sha256_file(path):
 digest=hashlib.sha256()
 with path.open('rb') as stream:
  while chunk:=stream.read(4*1024*1024):digest.update(chunk)
 return digest.hexdigest()

def main():
 os.umask(0o077)
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--destination',type=Path,required=True)
 parser.add_argument('--repository',default='HuggingFaceTB/SmolLM2-1.7B-Instruct')
 parser.add_argument('--revision',default='31b70e2e869a7173562077fd711b654946d38674')
 args=parser.parse_args();repo=args.repository;rev=args.revision;root=args.destination
 if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo) or not re.fullmatch(r'[a-f0-9]{40}',rev):raise ValueError('Use a public repository and pinned commit.')
 root.mkdir(parents=True,mode=0o700,exist_ok=True)
 with urllib.request.urlopen(f'https://huggingface.co/api/models/{repo}/tree/{rev}',timeout=30) as response:files=json.load(response)
 names={'config.json','generation_config.json','special_tokens_map.json','tokenizer.json','tokenizer_config.json','vocab.json','merges.txt','tokenizer.model','model.safetensors.index.json','LICENSE','LICENSE.txt'}
 selected=[item for item in files if item['path'] in names or re.fullmatch(r'model(?:-\d+-of-\d+)?\.safetensors',item['path'])]
 if not any(item['path'].endswith('.safetensors') for item in selected):raise ValueError('No public safetensors weights found.')
 def download(item):
  name=item['path'];target=root/name;size=item['size'];expected=item.get('lfs',{}).get('oid')
  if target.exists() and target.stat().st_size==size and (not expected or sha256_file(target)==expected):
   print('Verified cached:',name,flush=True);return
  temporary=target.with_suffix(target.suffix+'.partial');received=0;digest=hashlib.sha256();last=time.monotonic()
  with urllib.request.urlopen(f'https://huggingface.co/{repo}/resolve/{rev}/{name}?download=true',timeout=180) as incoming,temporary.open('wb') as output:
   while True:
    chunk=incoming.read(4*1024*1024)
    if not chunk:break
    output.write(chunk);digest.update(chunk);received+=len(chunk)
    if time.monotonic()-last>30:
     print(name,round(received/1024**3,2),'GB',flush=True);last=time.monotonic()
  if received!=size or expected and digest.hexdigest()!=expected:raise ValueError('Public model checksum mismatch.')
  temporary.replace(target);target.chmod(0o600)
  print('Downloaded:',name,'bytes=',received,flush=True)
 with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(download,selected))
 (root/'source.json').write_text(json.dumps({'repository':repo,'revision':rev},indent=2)+'\n')
 print(json.dumps({'public_model_ready':repo,'bytes':sum(item['size'] for item in selected)}),flush=True)

if __name__=='__main__':main()
