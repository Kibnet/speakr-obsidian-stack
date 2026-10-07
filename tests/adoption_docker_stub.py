"""Synthetic native fixture only; never invokes real Docker."""
import json
from pathlib import Path
import sys
base=Path(__file__).resolve().parent
args=sys.argv[1:]
if args[0]=='compose': print((base/'compose-response.json').read_text())
elif args[0]=='image': print(json.dumps([{'Id':'sha256:'+'a'*64}]))
elif args[0]=='inspect':
    data=json.loads((base/'containers-response.json').read_text())
    if '--format' in args:
        item=next(c for c in data if c['Name'].lstrip('/')==args[-1])
        labels=item['Config']['Labels']
        print(item['Image'],'true',labels['com.docker.compose.project'],labels['com.docker.compose.service'])
    else: print(json.dumps(data))
elif args[0]=='info': print('fixture')
elif args[0]=='ps': print('\n'.join(c['Name'].lstrip('/') for c in json.loads((base/'containers-response.json').read_text())))
else: raise SystemExit(2)
