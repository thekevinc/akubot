"""Offline synthetic recovery integration test. Never mounts Discord messages."""
import hashlib, json, os
from pathlib import Path
import random, signal, subprocess, sys, tempfile, time
import torch
from safetensors.torch import load_file
from checkpoints import Checkpoints, digest
from job_state import TrainingControl, atomic_json, read_json
from full_train import fingerprint


def wait_for(control, predicate, process, timeout=90):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        status=control.status()
        if predicate(status): return status
        if process.poll() is not None: raise AssertionError(f'Worker exited {process.returncode}: '+status['state'])
        time.sleep(.025)
    raise AssertionError('Synthetic worker timed out: '+control.status()['state'])


def prepare(root):
    data=root/'dataset';data.mkdir(parents=True)
    rng=random.Random(71)
    for split,count in [('train',640),('validation',16)]:
        with (data/f'{split}.jsonl').open('w') as stream:
            for i in range(count):
                ids=[rng.randint(1,63) for _ in range(20+(i%9))]
                stream.write(json.dumps({'ids':ids,'labels':[-100]*4+ids[4:]})+'\n')
    config={'version':1,'synthetic':True,'train_examples':640,'validation_examples':16,'total_steps':160,
      'data_files':{p.name:digest(p) for p in data.glob('*.jsonl')},'batch_size':2,'accumulation':2,
      'seed':42,'max_length':64,'rank':2,'alpha':4,'dropout':.05,'learning_rate':1e-3,'warmup_ratio':.03,
      'checkpoint_every':10,'checkpoint_seconds':300,'evaluate_every':40,'evaluation_samples':8,'log_every':1}
    atomic_json(data/'job.json',config)
    control=TrainingControl(root/'control');control.set_desired('running')
    return config,control


def launch(root):
    return subprocess.Popen([sys.executable,'/trainer/full_train.py','--synthetic','--base',str(root/'base'),
        '--dataset',str(root/'dataset'),'--output',str(root/'output'),'--control',str(root/'control')],
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


def main():
    torch.set_num_threads(1)
    with tempfile.TemporaryDirectory() as folder:
        root=Path(folder)
        baseline=root/'baseline';config,c1=prepare(baseline)
        first=launch(baseline)
        assert first.wait(timeout=120)==0,c1.status()
        assert c1.status()['state']=='completed'
        resumed=root/'resumed';config,c2=prepare(resumed)
        second=launch(resumed)
        wait_for(c2,lambda s:s['step']>=13 and s.get('last_checkpoint_step',0)>=10,second)
        second.kill();second.wait(timeout=10)
        checkpoints=Checkpoints(resumed/'output/checkpoints',fingerprint(config))
        checkpoint=checkpoints.latest();assert checkpoint
        saved_step=int(checkpoint.name.split('-')[1])
        # An incomplete higher-numbered checkpoint must be ignored.
        (checkpoints.root/'step-999999999').mkdir()
        (checkpoints.root/'.writing-interrupted').mkdir()
        assert checkpoints.latest()==checkpoint
        # A damaged newer checkpoint must fall back to an earlier complete one.
        import shutil
        corrupt=checkpoints.root/'step-999999998';shutil.copytree(checkpoint,corrupt)
        with (corrupt/'state.pt').open('ab') as stream:stream.write(b'corrupt')
        assert checkpoints.latest()==checkpoint
        assert Checkpoints(checkpoints.root,'mismatched-fingerprint').latest() is None
        began=time.time()
        third=launch(resumed)
        wait_for(c2,lambda s:(resumed/'control/heartbeat.json').exists() and read_json(resumed/'control/heartbeat.json')['at']>=began and s['state']=='training' and s['step']>=saved_step+3,third)
        c2.request('pause')
        paused=wait_for(c2,lambda s:s['state']=='paused',third)
        assert paused['last_checkpoint_step']==paused['step']
        assert paused['gpu_gb']==0
        time.sleep(.2);assert c2.status()['step']==paused['step']
        c2.request('resume')
        wait_for(c2,lambda s:s['state']=='training' and s['step']>=paused['step']+2,third)
        c2.request('stop')
        assert third.wait(timeout=30)==0,c2.status()
        assert c2.status()['state']=='stopped'
        assert c2.status()['last_checkpoint_step']==c2.status()['step']
        c2.set_desired('running')
        fourth=launch(resumed)
        assert fourth.wait(timeout=120)==0,c2.status()
        assert c2.status()['state']=='completed'
        a=load_file(str(baseline/'output/adapter/adapter_model.safetensors'))
        b=load_file(str(resumed/'output/adapter/adapter_model.safetensors'))
        assert set(a)==set(b)
        for key in a: assert torch.equal(a[key],b[key]),'Resumed adapter differs: '+key
        for run in (baseline,resumed):
            latest=Checkpoints(run/'output/checkpoints',fingerprint(config)).latest()
            state=torch.load(latest/'state.pt',map_location='cpu',weights_only=True)
            assert state['progress']['cursor']==640 and state['progress']['step']==160
            assert len([p for p in (run/'output/checkpoints').glob('step-*') if Checkpoints(run/'output/checkpoints',fingerprint(config))._valid(p)])<=4
        print(json.dumps({'synthetic_recovery':'passed','exact_adapter_match':True,'tested':['SIGKILL recovery','safe pause/resume','dashboard Stop and restart','partial checkpoint ignored','corrupt checkpoint fallback','fingerprint mismatch rejected','retention bounded']}))


if __name__=='__main__':
    os.umask(0o077)
    main()
