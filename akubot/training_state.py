"""Small file-based training controls. No Docker access or arbitrary paths."""
from __future__ import annotations
import json,math,os,time,uuid
from pathlib import Path

RUN_ID='llama-full-v1'
PHASES={'ready','preparing','loading','training','checkpointing','pausing','paused','validating','exporting','completed','failed','interrupted'}
NUMERIC={'step','total_steps','train_examples','validation_examples','prepared_examples','loss','validation_loss','base_validation_loss','active_seconds','eta_seconds','last_checkpoint_step','last_checkpoint_at','gpu_gb','truncated_messages','updated_at'}

def atomic_json(path:Path,value):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if path.is_symlink():raise ValueError('Symlinks are not permitted.')
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(value,stream,allow_nan=False);stream.write('\n');stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
        directory=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:temporary.unlink(missing_ok=True)

def read_json(path:Path,limit=65536):
    if path.is_symlink() or path.stat().st_size>limit:raise ValueError('Invalid training state file.')
    with path.open() as stream:return json.load(stream)

class TrainingControlError(ValueError):pass

class TrainingControl:
    def __init__(self,root:Path):self.root=root
    def desired(self):
        try:
            data=read_json(self.root/'command.json')
            return data['desired'] if data.get('version')==1 and data.get('desired') in {'running','paused'} else 'paused'
        except (OSError,ValueError,KeyError,TypeError,AttributeError):return 'paused'
    def status(self,now=None):
        now=time.time() if now is None else now
        result={'run_id':RUN_ID,'model_label':'Llama 3.1 8B','state':'not_started','worker_online':False,'step':0,'total_steps':0,'recent':[]}
        try:
            data=read_json(self.root/'status.json')
            if not isinstance(data,dict) or data.get('state') not in PHASES:raise ValueError()
            result['state']=data['state']
            for key in NUMERIC:
                value=data.get(key)
                if type(value) in {int,float} and math.isfinite(value) and value>=0:result[key]=value
            history=data.get('recent',[])
            if isinstance(history,list):
                result['recent']=[{k:v for k,v in item.items() if k in {'step','loss','validation_loss'} and type(v) in {int,float} and math.isfinite(v) and v>=0} for item in history[-60:] if isinstance(item,dict)]
            if isinstance(data.get('error_code'),str) and data['error_code'].isalnum() and len(data['error_code'])<=80:result['error_code']=data['error_code']
            heartbeat=read_json(self.root/'heartbeat.json')
            fresh=type(heartbeat.get('at')) in {int,float} and -5 <= now-heartbeat['at'] <= 30
            result['worker_online']=bool(fresh and heartbeat.get('alive') is True and data['state'] not in {'completed','failed','interrupted','ready'})
        except FileNotFoundError:pass
        except (OSError,ValueError,TypeError,AttributeError):result['state']='unavailable'
        result['desired']=self.desired()
        result['can_pause']=result['worker_online'] and result['state'] not in {'paused','pausing','exporting'} and result['desired']!='paused'
        result['can_resume']=result['worker_online'] and (result['state']=='paused' or result['desired']=='paused')
        return result
    def request(self,action):
        if action not in {'pause','resume'}:raise TrainingControlError('Choose Pause or Resume.')
        state=self.status()
        if not state['worker_online']:raise TrainingControlError('Start or recover the background worker from WSL first.')
        if action=='resume' and not state['can_resume']:raise TrainingControlError('Training is already running.')
        if action=='pause' and not state['can_pause']:raise TrainingControlError('Training is already paused or pausing.')
        self.set_desired('paused' if action=='pause' else 'running')
        return self.status()
    def set_desired(self,value):
        if value not in {'running','paused'}:raise ValueError('Unknown training command.')
        atomic_json(self.root/'command.json',{'version':1,'desired':value,'at':time.time()})
