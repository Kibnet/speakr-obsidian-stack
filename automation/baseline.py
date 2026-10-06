"""Explicit local scan only, including under maintenance; no uploads/API calls."""
import argparse
from pathlib import Path
import sqlite3
from bridge import Bridge, WorkerLock
from common import config

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',required=True)
    p.add_argument('--verify-fixture',action='store_true')
    a=p.parse_args()
    root=Path(a.root)
    if a.verify_fixture:
        with sqlite3.connect(root/'state.sqlite3') as db:
            assert db.execute('SELECT COUNT(*) FROM observed WHERE kind=?',('baseline',)).fetchone()[0]==1
            assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==0
    else:
        with WorkerLock(root):
            b=Bridge(config(root/'config.json'),root)
            try: b.scan_sources()
            finally: b.close()
