"""Explicit local scan only, including under maintenance; no uploads/API calls."""
import argparse
from pathlib import Path
from bridge import Bridge, WorkerLock
from common import config

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',required=True)
    a=p.parse_args()
    root=Path(a.root)
    with WorkerLock(root):
        b=Bridge(config(root/'config.json'),root)
        try: b.scan_sources()
        finally: b.close()
