"""Trusted local registry of installed base models and compatible LoRA builds."""
from __future__ import annotations
import json,re,os
from pathlib import Path

class ModelSelectionError(ValueError):
    pass


def valid_name(value):
    return isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./:@-]{0,149}',value) and 'cloud' not in value.lower()


class ModelCatalog:
    def __init__(self,path:Path):
        self.path=path

    def read(self):
        try:
            if not self.path.exists():return {'version':1,'bases':[],'adapters':[]}
            if self.path.is_symlink() or self.path.stat().st_size>65536:raise ValueError()
            data=json.loads(self.path.read_text())
            if not isinstance(data,dict) or set(data)!={'version','bases','adapters'} or data['version']!=1:raise ValueError()
            if not isinstance(data['bases'],list) or not isinstance(data['adapters'],list) or len(data['bases'])+len(data['adapters'])>200:raise ValueError()
            ids=set();models=set()
            for kind,fields in [('bases',{'id','label','model'}),('adapters',{'id','label','model','base_id'})]:
                for entry in data[kind]:
                    if not isinstance(entry,dict) or set(entry)!=fields:raise ValueError()
                    if not valid_name(entry['id']) or not valid_name(entry['model']):raise ValueError()
                    if not isinstance(entry['label'],str) or not 1<=len(entry['label'])<=100:raise ValueError()
                    if entry['id'] in ids or entry['model'] in models:raise ValueError()
                    ids.add(entry['id']);models.add(entry['model'])
            bases={entry['id'] for entry in data['bases']}
            if any(entry['base_id'] not in bases for entry in data['adapters']):raise ValueError()
            return data
        except (OSError,ValueError,TypeError,KeyError):
            raise ModelSelectionError('The local model registry needs repair before selecting a model.') from None

    def options(self,installed,current):
        data=self.read()
        bases=[dict(entry) for entry in data['bases']]
        adapters=[dict(entry) for entry in data['adapters']]
        known={entry['model'] for entry in bases+adapters}
        installed={name for name in installed if valid_name(name)}
        for name in sorted(installed|{current}):
            if name not in known and valid_name(name):
                label='Llama 3.1 8B' if name=='llama3.1:8b' else name
                bases.append({'id':'ollama:'+name,'label':label,'model':name})
        for entry in bases+adapters:entry['available']=entry['model'] in installed
        selection=None
        for entry in adapters:
            if entry['model']==current:selection={'base_id':entry['base_id'],'adapter_id':entry['id']}
        for entry in bases:
            if entry['model']==current:selection={'base_id':entry['id'],'adapter_id':None}
        return {'bases':bases,'adapters':adapters,'selection':selection}

    def resolve(self,selection,installed,current):
        if not isinstance(selection,dict) or set(selection)!={'base_id','adapter_id'}:
            raise ModelSelectionError('Choose a base model and a compatible LoRA style.')
        if not isinstance(selection['base_id'],str) or (selection['adapter_id'] is not None and not isinstance(selection['adapter_id'],str)):
            raise ModelSelectionError('Choose a base model and a compatible LoRA style.')
        options=self.options(installed,current)
        base=next((entry for entry in options['bases'] if entry['id']==selection['base_id']),None)
        if base is None:raise ModelSelectionError('Choose an installed base model.')
        target=base
        if selection['adapter_id'] is not None:
            target=next((entry for entry in options['adapters'] if entry['id']==selection['adapter_id']),None)
            if target is None or target['base_id']!=base['id']:
                raise ModelSelectionError('This LoRA was trained for a different base model. Choose a matching style or None.')
        if not target['available'] and target['model']!=current:
            raise ModelSelectionError('This model combination is not installed on this computer.')
        return target['model']

    def register(self,base,adapter):
        data=self.read()
        for kind,entry in [('bases',base),('adapters',adapter)]:
            previous=next((row for row in data[kind] if row['id']==entry['id']),None)
            if previous and previous!=entry:
                raise ModelSelectionError('A model registration already uses this ID with different details.')
            if not previous:data[kind].append(entry)
        self.path.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
        # Validate the proposed data before replacing a known-good registry.
        temporary=self.path.with_suffix('.tmp')
        descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            with os.fdopen(descriptor,'w') as stream:stream.write(json.dumps(data,indent=2)+'\n')
            ModelCatalog(temporary).read()
            temporary.replace(self.path)
        finally:temporary.unlink(missing_ok=True)
