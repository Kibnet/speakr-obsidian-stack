"""Canonical path preflight for direct native installer calls too."""
import sys
from pathlib import Path
import stack

target=stack.canonical(sys.argv[1])
manifest=stack.load(target/'stack-install.json')
cfg=stack.load(target/'automation/config.json')
if target==Path(target.anchor) or stack.overlap(target,stack.ROOT):
    raise ValueError('Runtime overlaps checkout/drive root')
for name in [cfg['vault_root'],cfg['vault'],*cfg['sources'],*manifest.get('external_inputs',[])]:
    if stack.overlap(target,name): raise ValueError('Runtime overlaps protected external input')
if target!=stack.canonical(manifest['target_root']): raise ValueError('Runtime manifest target mismatch')
print('Canonical runtime guard passed')
