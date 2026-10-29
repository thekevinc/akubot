"""Scale an adapter relative to its trained influence before merging."""
import math

def checked_strength(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0 <= value <= 2:
        raise ValueError('LoRA strength must be a finite number from 0 to 2.')
    return float(value)

def apply_strength(model,value):
    from peft.tuners.lora.layer import LoraLayer
    value=checked_strength(value)
    count=0
    for module in model.modules():
        if isinstance(module,LoraLayer) and 'default' in module.scaling:
            module.set_scale('default',value)
            count+=1
    if not count:raise ValueError('No matching LoRA layers were found.')
    return count
