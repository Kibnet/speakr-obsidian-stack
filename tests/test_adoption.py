import base64
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'automation'))
import adoption
from bridge import Bridge
from common import atomic_json, migration_guard, recovery_held
import test_bridge as fixtures

class HoldTests(unittest.TestCase):
    setUp=fixtures.BridgeTests.setUp
    tearDown=fixtures.BridgeTests.tearDown
    run_media=fixtures.BridgeTests.run_media
    file=fixtures.BridgeTests.file
    cycle_ready=fixtures.BridgeTests.cycle_ready
    rows=fixtures.BridgeTests.rows
    notes=fixtures.BridgeTests.notes
    def test_protected_remote_alias_blocks_all_effects(self):
        from common import enqueue
        enqueue([self.file(self.outside)],self.root)
        self.cycle_ready()
        row=self.rows()[0]
        before=[dict(r) for r in self.rows()]
        notes={str(p):p.read_bytes() for p in self.notes()}
        self.bridge.db.executescript(adoption.HOLD_DDL)
        self.bridge.db.execute('INSERT INTO recovery_holds VALUES(?,?,?,?,?)',('0'*32,row['recording_id'],'hold',0,'test'))
        self.bridge.db.commit()
        self.assertTrue(recovery_held(self.bridge.db,row))
        self.bridge.poll()
        self.bridge.publish(row,dict(self.api.rows[row['recording_id']],summary='changed'),{'raw':'changed','segments':[]})
        self.bridge.reconcile(row)
        self.bridge.db.execute("UPDATE jobs SET state='retry' WHERE id=?",(row['id'],))
        self.bridge.db.commit()
        self.api.retry=lambda *a,**kw: self.fail('held retry reached API')
        self.bridge.submit()
        for action in ('retry','re-export'):
            atomic_json(self.root/'controls'/('held-'+action+'.json'),{'job_id':row['id'],'action':action})
        self.bridge.controls()
        self.assertEqual({str(p):p.read_bytes() for p in self.notes()},notes)
        self.assertEqual(self.rows()[0]['state'],'retry')
        self.assertTrue(all(json.loads(p.read_text())['status']=='failed' for p in (self.root/'controls').glob('held-*.json')))
        self.assertEqual(before[0]['recording_id'],self.rows()[0]['recording_id'])
        enqueue([self.file(self.outside,'next.m4a',b'next audio')],self.root)
        self.cycle_ready()
        self.assertEqual(len(self.rows()),2)
        self.assertEqual(self.rows()[0]['state'],'retry')
        self.assertEqual(self.rows()[1]['state'],'completed')
        self.assertEqual(len(self.notes()),len(notes)+1)
        self.bridge.close()
        self.bridge=Bridge(self.cfg,self.root,self.api,self.clock)
        self.assertTrue(recovery_held(self.bridge.db,self.rows()[0]))

class AdoptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        base=Path(self.tmp.name)
        self.root=base/'runtime'/'automation'
        self.root.mkdir(parents=True)
        (base/'vault').mkdir(); (base/'audio').mkdir()
        self.cfg={'sources':[str(base/'audio')],'vault_root':str(base/'vault'),'vault':str(base/'vault'),
                  'speakr_url':'http://127.0.0.1:1','stable_seconds':1,'auto_start_docker':False,
                  'exact_two_speaker_roots':[str(base/'audio')],'acr_timestamp_roots':[str(base/'audio')]}
        b=Bridge(self.cfg,self.root,fixtures.API(),fixtures.Clock())
        b.db.execute("INSERT INTO jobs(id,sha256,state,recording_id) VALUES(?,?,?,?)",('a'*32,'b'*64,'failed',7))
        b.db.execute("INSERT INTO jobs(id,sha256,state,recording_id) VALUES(?,?,?,?)",('c'*32,'d'*64,'summary_pending',7))
        b.db.commit(); b.close()
        self.cfg['legacy_recovery_jobs']=['a'*32,'c'*32]
        atomic_json(self.root/'config.json',self.cfg)
        atomic_json(self.root/'backlog-approved.json',{'job_ids':['c'*32]})
        (self.root/'bridge.py').write_bytes(b'# synthetic old entrypoint\n')
        (self.root/'install.ps1').write_bytes(b'# synthetic old installer\r\n')
        self.addCleanup(self.tmp.cleanup)

    def plan(self):
        incoming={p.name:adoption.digest(p) for p in (adoption.PACKAGE/'automation').iterdir() if p.suffix in adoption.EXTENSIONS}
        backup=self.root/'backups'/'adoption-test'; backup.mkdir(parents=True)
        cfg=dict(self.cfg); cfg.pop('legacy_recovery_jobs')
        for name in ('compose_file','env_file'):
            path=self.root.parent/name; path.write_text('synthetic fixture')
            cfg[name]=str(path)
        cfg['runtime_manifest']={'compose_sha256':adoption.digest(cfg['compose_file']),'env_sha256':adoption.digest(cfg['env_file'])}
        launcher=self.root.parent/'old-launcher.ps1'; launcher.write_text('# fixture')
        plan={'incoming':incoming,'originals':{p.name:base64.b64encode(p.read_bytes()).decode() for p in self.root.iterdir() if p.suffix in adoption.EXTENSIONS},
              'config':base64.b64encode((self.root/'config.json').read_bytes()).decode(),
              'config_hash':adoption.digest(self.root/'config.json'),'new_config':cfg,'holds':adoption.legacy_holds(self.root,self.cfg),
              'launcher':str(launcher),'launcher_hash':adoption.digest(launcher),'project':'fixture','commit':'fixture-commit'}
        adoption.put(backup/'plan.json',plan)
        adoption.put(self.root/'adoption-state.json',{'phase':'preparing','backup':str(backup),'progress':[],
                     'expected_holds':[['a'*32,7,backup.name]]})
        (self.root/'config.json').rename(backup/'config.withdrawn.json')
        return backup

    def test_missing_approval_does_not_authorize(self):
        self.assertEqual(adoption.legacy_holds(self.root,self.cfg),[{'job_id':'a'*32,'recording_id':7}])
        atomic_json(self.root/'backlog-approved.json',{'job_ids':['e'*32]})
        with self.assertRaises(ValueError): adoption.legacy_holds(self.root,self.cfg)

    def test_committed_metadata_matches_rollback_intent_bytes(self):
        self.plan(); adoption.guards(self.root); adoption.stage(self.root)
        adoption.commit(self.root,{'tasks':{}})
        self.assertEqual(adoption.verify(self.root)['status'],'verified')
        adoption.rollback_check(self.root)

    def test_active_protected_alias_refuses_migration(self):
        with adoption.connection(self.root) as db: db.execute("UPDATE jobs SET state='accepted' WHERE id=?",('c'*32,))
        with self.assertRaisesRegex(ValueError,'active'): adoption.legacy_holds(self.root,self.cfg)

    def test_partial_guards_before_db_and_rollback_preserves_data(self):
        before=adoption.baseline(self.root)
        backup=self.plan()
        with self.assertRaises(RuntimeError): migration_guard(self.root)
        with self.assertRaises(RuntimeError): Bridge(self.cfg,self.root)
        self.assertEqual(adoption.baseline(self.root),before)
        adoption.guards(self.root)
        adoption.stage(self.root)
        self.assertEqual(adoption.baseline(self.root),before)
        with adoption.connection(self.root,True) as db:
            self.assertEqual(db.execute('SELECT job_id,recording_id FROM recovery_holds').fetchall(),[('a'*32,7)])
        adoption.rollback(self.root)
        self.assertEqual((self.root/'bridge.py').read_bytes(),b'# synthetic old entrypoint\n')
        self.assertEqual((self.root/'install.ps1').read_bytes(),adoption.PS_GUARD)
        self.assertFalse((self.root/'config.json').exists())
        self.assertEqual(adoption.baseline(self.root),before)
        with closing(sqlite3.connect(backup/'state.sqlite3')) as db: self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        adoption.release_old(self.root)
        self.assertEqual((self.root/'install.ps1').read_bytes(),b'# synthetic old installer\r\n')
        self.assertEqual(adoption.load(self.root/'config.json'),self.cfg)

    def test_rollback_refuses_new_rows_and_code_drift(self):
        self.plan(); adoption.guards(self.root); adoption.stage(self.root)
        (self.root/'bridge.py').write_text('# foreign code')
        with self.assertRaisesRegex(ValueError,'Code drift'): adoption.rollback(self.root)
        (self.root/'bridge.py').write_bytes((adoption.PACKAGE/'automation'/'bridge.py').read_bytes())
        with adoption.connection(self.root) as db: db.execute("UPDATE jobs SET error='changed'")
        with self.assertRaisesRegex(ValueError,'Data drift'): adoption.rollback(self.root)

    def test_malformed_journal_fails_before_queue_write(self):
        before=adoption.baseline(self.root)
        (self.root/'adoption-state.json').write_text('{')
        with self.assertRaises(ValueError): Bridge(self.cfg,self.root)
        self.assertEqual(adoption.baseline(self.root),before)

    def test_hold_ddl_and_rows_rollback_together(self):
        from unittest.mock import patch
        self.plan(); adoption.guards(self.root)
        with patch.object(adoption.time,'time',side_effect=RuntimeError('interrupt between DDL and rows')):
            with self.assertRaises(RuntimeError): adoption.stage(self.root)
        with adoption.connection(self.root,True) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='recovery_holds'").fetchone())

    def test_installed_hold_drift_refused_before_constructor_write(self):
        self.plan(); adoption.guards(self.root); adoption.stage(self.root)
        state=adoption.load(self.root/'adoption-state.json')
        state.update(phase='installed',expected_holds=[['a'*32,7,Path(state['backup']).name]])
        adoption.put(self.root/'adoption-state.json',state)
        migration_guard(self.root)
        with adoption.connection(self.root) as db: db.execute('DELETE FROM recovery_holds')
        before=adoption.baseline(self.root)
        with self.assertRaisesRegex(RuntimeError,'holds drift'): Bridge(self.cfg,self.root)
        self.assertEqual(adoption.baseline(self.root),before)

    def test_rollback_refuses_foreign_metadata_and_external_drift(self):
        self.plan(); adoption.guards(self.root); adoption.stage(self.root)
        adoption.put(self.root/'install-state.json',{'foreign':True})
        with self.assertRaisesRegex(ValueError,'metadata drift'): adoption.rollback_check(self.root)
        (self.root/'install-state.json').unlink()
        (self.root.parent/'compose_file').write_text('foreign compose')
        with self.assertRaisesRegex(ValueError,'External file drift'): adoption.rollback_check(self.root)

    def test_installed_rollback_refuses_missing_each_manifest(self):
        self.plan(); adoption.guards(self.root); adoption.stage(self.root)
        state=adoption.load(self.root/'adoption-state.json')
        state['phase']='installed'; state['metadata_expected']={}
        paths=[self.root/'install-state.json',self.root.parent/'stack-install.json']
        for path in paths:
            adoption.put(path,{'fixture':True}); state['metadata_expected'][str(path)]=adoption.digest(path)
        adoption.put(self.root/'adoption-state.json',state)
        for path in paths:
            data=path.read_bytes(); path.unlink()
            with self.assertRaisesRegex(ValueError,'Missing ownership metadata'): adoption.rollback_check(self.root)
            self.assertFalse((self.root/'maintenance.json').exists())
            path.write_bytes(data)

if __name__=='__main__': unittest.main()
