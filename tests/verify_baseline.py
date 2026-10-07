"""Read-only assertions for the synthetic native installer fixture."""
import argparse
from pathlib import Path
import sqlite3

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--root', required=True)
a = p.parse_args()
uri = (Path(a.root) / 'state.sqlite3').resolve().as_uri() + '?mode=ro'
with sqlite3.connect(uri, uri=True) as db:
    assert db.execute('SELECT COUNT(*) FROM observed WHERE kind=?', ('baseline',)).fetchone()[0] == 1
    assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
