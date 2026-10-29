"""Complete checkpoints, committed atomically and verified before loading."""
from __future__ import annotations
import hashlib,json,os,random,shutil,uuid
from pathlib import Path

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        while chunk:=stream.read(4*1024*1024):h.update(chunk)
    return h.hexdigest()

class Checkpoints:
    def __init__(self,root:Path,fingerprint:str,keep=3):
        self.root,self.fingerprint,self.keep=root,fingerprint,keep
        root.mkdir(mode=0o700,parents=True,exist_ok=True)
    def _valid(self,path):
        try:
            if path.is_symlink() or not path.is_dir():return None
            marker=path/'complete.json'
            if marker.is_symlink() or marker.stat().st_size>65536:return None
            data=json.loads(marker.read_text())
            if data.get('version')!=1 or data.get('fingerprint')!=self.fingerprint:return None
            files=data.get('files')
            if not isinstance(files,dict) or not {'state.pt','adapter/adapter_model.safetensors','adapter/adapter_config.json'}<=set(files):return None
            for name,checksum in files.items():
                relative=Path(name)
                if relative.is_absolute() or '..' in relative.parts or not isinstance(checksum,str):return None
                file=path/relative
                if file.is_symlink() or not file.resolve().is_relative_to(path.resolve()) or not file.is_file() or digest(file)!=checksum:return None
            if type(data.get('step')) is not int or data['step']<0:return None
            return data
        except (OSError,ValueError,TypeError,AttributeError):return None
    def latest(self):
        for path in sorted(self.root.glob('step-*'),reverse=True):
            if self._valid(path):return path
        return None
    def save(self,model,optimizer,progress):
        import torch
        step=progress['step'];target=self.root/f'step-{step:09d}'
        if target.exists():
            if self._valid(target):return target
            # An invalid previous write is retained for inspection, never loaded.
            target.rename(self.root/f'invalid-{target.name}-{uuid.uuid4().hex[:8]}')
        temporary=self.root/f'.writing-{uuid.uuid4().hex}';temporary.mkdir(mode=0o700)
        try:
            model.save_pretrained(temporary/'adapter',safe_serialization=True)
            state={'optimizer':optimizer.state_dict(),'progress':progress,'python_rng':random.getstate(),
                   'torch_rng':torch.get_rng_state(),'cuda_rng':torch.cuda.get_rng_state_all() if torch.cuda.is_available() else []}
            torch.save(state,temporary/'state.pt')
            files={}
            for file in temporary.rglob('*'):
                if file.is_file():
                    file.chmod(0o600)
                    with file.open('rb') as stream:os.fsync(stream.fileno())
                    files[str(file.relative_to(temporary))]=digest(file)
            marker={'version':1,'step':step,'fingerprint':self.fingerprint,'files':files}
            with (temporary/'complete.json').open('x') as stream:
                json.dump(marker,stream);stream.flush();os.fsync(stream.fileno())
            temporary.rename(target)
            directory=os.open(self.root,os.O_RDONLY)
            try:os.fsync(directory)
            finally:os.close(directory)
            valid=[p for p in sorted(self.root.glob('step-*'),reverse=True) if self._valid(p)]
            best=progress.get('best_step')
            for old in valid[self.keep:]:
                if not isinstance(best,int) or old.name != f'step-{best:09d}':
                    shutil.rmtree(old)
            return target
        except BaseException:
            # Incomplete hidden directories are ignored after a crash.
            raise
    def load(self,path,model,optimizer):
        import torch
        from peft import set_peft_model_state_dict
        from safetensors.torch import load_file
        if not self._valid(path):raise ValueError('Checkpoint is incomplete or does not match this job.')
        state=torch.load(path/'state.pt',map_location='cpu',weights_only=True)
        set_peft_model_state_dict(model,load_file(str(path/'adapter/adapter_model.safetensors')))
        optimizer.load_state_dict(state['optimizer'])
        random.setstate(state['python_rng']);torch.set_rng_state(state['torch_rng'])
        if state['cuda_rng']:torch.cuda.set_rng_state_all(state['cuda_rng'])
        return state['progress']
