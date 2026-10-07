"""Explicit adoption of a legacy runtime. Plans/backups contain private machine data."""
import argparse
from contextlib import contextmanager, closing
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

PACKAGE = Path(__file__).resolve().parents[1]
EXTENSIONS = {'.py', '.pyw', '.ps1'}
PY_GUARD = b"raise RuntimeError('Runtime migration in progress; use adopt Rollback')\n"
PS_GUARD = b"throw 'Runtime migration in progress; use adopt Rollback'\r\n"
HOLD_DDL = '''CREATE TABLE recovery_holds(job_id TEXT PRIMARY KEY, recording_id INTEGER,
 reason TEXT NOT NULL,created_at REAL NOT NULL,migration_id TEXT NOT NULL);
 CREATE INDEX recovery_hold_recording ON recovery_holds(recording_id);'''

def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def put(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex)
    with tmp.open('x', encoding='utf-8',newline='\n') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def copy_bytes(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex)
    with tmp.open('xb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def command(args):
    p = subprocess.run(args, capture_output=True, timeout=30,
                       creationflags=0x08000000 if os.name == 'nt' else 0)
    if p.returncode:
        raise ValueError(Path(str(args[0])).name + ' exit '+str(p.returncode)+'; output withheld')
    return p.stdout.decode('utf-8-sig')

def private_path(path, sid):
    if os.name=='nt':
        if not re.fullmatch(r'S-1-[0-9-]+',sid): raise ValueError('Invalid private owner SID')
        rights=':(OI)(CI)(F)' if Path(path).is_dir() else ':(F)'
        p=subprocess.run(['icacls',str(path),'/inheritance:r','/grant:r','*'+sid+rights,'*S-1-5-18'+rights,'*S-1-5-32-544'+rights],capture_output=True,timeout=30,creationflags=0x08000000)
        if p.returncode: raise ValueError('Private ACL setup failed; output withheld')
    else:
        os.chmod(path,0o700 if Path(path).is_dir() else 0o600)

@contextmanager
def connection(root, readonly=False):
    db=sqlite3.connect((Path(root)/'state.sqlite3').as_uri() + '?mode=' + ('ro' if readonly else 'rw'), uri=True)
    try:
        with db: yield db
    finally: db.close()

def baseline(root):
    with connection(root, True) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Queue integrity failed')
        tables = [x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name!='recovery_holds' ORDER BY name")]
        rows = {name: sorted(db.execute('SELECT * FROM "'+name.replace('"','""')+'"').fetchall(),key=lambda r:json.dumps(r,ensure_ascii=False)) for name in tables}
        paths = [r[0] for r in db.execute("SELECT DISTINCT path FROM publications WHERE state='published'")]
    logical = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    notes = {p: digest(p) if Path(p).is_file() else None for p in paths}
    return {'logical': logical, 'counts': {k: len(v) for k,v in rows.items()}, 'notes': notes}

def legacy_holds(root, cfg):
    ids = cfg.get('legacy_recovery_jobs', [])
    if not isinstance(ids, list) or len(set(ids)) != len(ids) or any(not re.fullmatch('[0-9a-f]{32}',x) for x in ids):
        raise ValueError('Invalid legacy job list')
    approval = Path(root)/'backlog-approved.json'
    approved = load(approval).get('job_ids') if approval.exists() else []
    if not isinstance(approved, list) or len(set(approved)) != len(approved) or set(approved)-set(ids):
        raise ValueError('Invalid historical approval')
    with connection(root, True) as db:
        if db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() != ('1',):
            raise ValueError('Unsupported queue schema')
        jobs = {row[0]: row for row in db.execute('SELECT id,recording_id,state,stage FROM jobs')}
        if set(ids)-set(jobs):
            raise ValueError('Unknown legacy job')
        held = [jobs[x] for x in ids if x not in approved]
        protected = {r[1] for r in held if r[1] is not None}
        if any(r[2] in ('accepted','submitting','reconciliation_required') and r[1] in protected for r in jobs.values()):
            raise ValueError('Protected recording has active/ambiguous processing')
    return [{'job_id': r[0], 'recording_id': r[1]} for r in held]

def ollama_settings(launcher, cfg):
    text = Path(launcher).read_text(encoding='utf-8-sig')
    values = dict(re.findall(r"\$env:(\w+)\s*=\s*'([^']*)'", text))
    exe = re.search(r"\$ollamaExecutable\s*=\s*'([^']+)'",text)
    required = {'OLLAMA_HOST','OLLAMA_MODELS','OLLAMA_NUM_PARALLEL','OLLAMA_MAX_LOADED_MODELS','OLLAMA_CONTEXT_LENGTH','OLLAMA_KEEP_ALIVE','NO_PROXY'}
    tune = {'OLLAMA_FLASH_ATTENTION','OLLAMA_KV_CACHE_TYPE','OLLAMA_GPU_OVERHEAD','OLLAMA_LLM_LIBRARY','OLLAMA_VULKAN','CUDA_VISIBLE_DEVICES'}
    allowed = required | tune | {'OLLAMA_NO_CLOUD','OLLAMA_NOPRUNE'}
    if not exe or required-set(values) or set(values)-allowed:
        raise ValueError('Unsupported old Ollama launcher; review effective settings')
    port = urlsplit(cfg['llm_url']).port
    if values['OLLAMA_HOST'] != '127.0.0.1:'+str(port):
        raise ValueError('Ollama endpoint differs')
    for key,expected in [('OLLAMA_NUM_PARALLEL','1'),('OLLAMA_MAX_LOADED_MODELS','1'),('OLLAMA_CONTEXT_LENGTH','16384'),('OLLAMA_KEEP_ALIVE','60s'),('OLLAMA_NO_CLOUD','1')]:
        if values.get(key) != expected:
            raise ValueError('Unsupported Ollama setting: '+key)
    if not Path(exe[1]).is_file() or not Path(values['OLLAMA_MODELS']).is_dir():
        raise ValueError('Ollama executable/models missing')
    return dict(ollama=exe[1], ollama_models=values['OLLAMA_MODELS'], llm_port=port,
                managed_ollama=True, ollama_tuning={k:v for k,v in values.items() if k in tune},
                ollama_noprune=values.get('OLLAMA_NOPRUNE')=='1',ollama_no_proxy=values['NO_PROXY'])

def identities(cfg):
    names = list(cfg['runtime_manifest']['containers'])
    if len(names) != 2:
        raise ValueError('Exactly two configured containers required')
    env = dict(os.environ)
    for key in list(env):
        if key.startswith('COMPOSE_'): del env[key]
    args = [cfg['docker'],'compose','--env-file',cfg['env_file'],'-f',cfg['compose_file'],'config','--format','json']
    p = subprocess.run(args,capture_output=True,timeout=30,env=env,creationflags=0x08000000 if os.name=='nt' else 0)
    if p.returncode: raise ValueError('Effective Compose inspection failed')
    compose = json.loads(p.stdout)
    data = json.loads(command([cfg['docker'],'inspect',*names]))
    result = {}
    project = None
    for c in data:
        name=c['Name'].lstrip('/')
        labels=c['Config']['Labels']
        service=labels.get('com.docker.compose.service')
        cp=labels.get('com.docker.compose.project')
        if not cp or service not in ('app','whisperx-asr') or not c['State']['Running']:
            raise ValueError('Container identity/running state unsupported')
        expected=compose['services'].get(service,{})
        if expected.get('container_name') != name or compose.get('name') != cp or (project and project!=cp):
            raise ValueError('Compose container/project differs')
        image=json.loads(command([cfg['docker'],'image','inspect',expected['image']]))[0]['Id']
        if image!=c['Image']: raise ValueError('Compose image differs from running image')
        mounts={m['Destination']:(m['Type'],os.path.normcase(os.path.normpath(m['Source'])),bool(m['RW'])) for m in c['Mounts']}
        for m in expected.get('volumes',[]):
            wanted=(m['type'],os.path.normcase(os.path.normpath(m['source'])),not m.get('read_only',False))
            if mounts.get(m['target'])!=wanted: raise ValueError('Container mount differs from Compose')
        ports=c['NetworkSettings']['Ports']
        for item in expected.get('ports',[]):
            actual=ports.get(str(item['target'])+'/'+item.get('protocol','tcp')) or []
            if not actual or any(x['HostIp']!='127.0.0.1' or x['HostPort']!=str(item['published']) for x in actual):
                raise ValueError('Container port differs/not loopback')
        result[name]={'service':service,'project':cp,'image_id':image,'image_ref':expected['image']}
        project=cp
    if {v['service'] for v in result.values()}!={'app','whisperx-asr'}: raise ValueError('Service identities incomplete')
    return {'compose_sha256':digest(cfg['compose_file']),'env_sha256':digest(cfg['env_file']),
            'containers':result}, project

def inspect(root, native, plan_path):
    root=Path(root).resolve()
    if root==PACKAGE or PACKAGE in root.parents or root in PACKAGE.parents or root==Path(root.anchor) or root.name.lower()!='automation' or root.is_symlink():
        raise ValueError('Canonical automation runtime required')
    if (root/'adoption-state.json').exists():
        state=load(root/'adoption-state.json')
        if state.get('phase')=='installed':
            verify(root)
            return {'status':'owned_version_verified','changed':False}
        raise ValueError('Interrupted adoption; use Rollback')
    if (root/'install-state.json').exists(): raise ValueError('Already owned; use Upgrade')
    cfg=load(root/'config.json')
    if Path(cfg['pythonw']).name.lower() not in ('python.exe','pythonw.exe') or not Path(cfg['pythonw']).is_file():
        raise ValueError('Unsupported legacy Python executable')
    for x in [cfg['vault_root'],cfg['vault'],*cfg['sources']]:
        p=Path(x).resolve()
        if p==root or root in p.parents or p in root.parents: raise ValueError('Protected input overlaps runtime')
    names=[cfg[x] for x in ('bridge_task','watchdog_task','ollama_task')]
    tasks=native['tasks']
    if set(tasks)!=set(names): raise ValueError('Task snapshot mismatch')
    sid=native['sid']
    for name in names:
        t=tasks[name]
        if t['sid']!=sid or len(t['actions'])!=1: raise ValueError('Task principal/action unsupported')
    bridge=tasks[cfg['bridge_task']]['actions'][0]
    watcher=tasks[cfg['watchdog_task']]['actions'][0]
    if bridge!={'execute':cfg['pythonw'],'arguments':'"'+str(root/'bridge.py')+'" --loop','working_directory':str(root)}:
        raise ValueError('Bridge task differs from known legacy action')
    if watcher!={'execute':cfg['pythonw'],'arguments':'"'+str(root/'watchdog.py')+'"','working_directory':str(root)}:
        raise ValueError('Watchdog task differs from known legacy action')
    ollama=tasks[cfg['ollama_task']]['actions'][0]
    match=re.fullmatch(r'-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "([^"]+)"',ollama['arguments'])
    if not match or Path(ollama['execute']).name.lower()!='powershell.exe': raise ValueError('Unsupported old Ollama task')
    launcher=Path(match[1]).resolve()
    if launcher.parent!=root.parent/'llm' or ollama['working_directory']!=str(launcher.parent): raise ValueError('Old launcher location differs')
    settings=ollama_settings(launcher,cfg)
    manifest,project=identities(cfg)
    incoming={p.name:digest(p) for p in (PACKAGE/'automation').iterdir() if p.suffix in EXTENSIONS}
    originals={p.name:base64.b64encode(p.read_bytes()).decode() for p in root.iterdir() if p.is_file() and p.suffix in EXTENSIONS}
    if not {'bridge.py','common.py','recovery.py','watchdog.py','enqueue.pyw','runtime-control.ps1','recovery-control.ps1'}<=originals.keys():
        raise ValueError('Incomplete legacy package')
    if set(originals)-set(incoming): raise ValueError('Unknown legacy executable module')
    new=dict(cfg)
    new.pop('legacy_recovery_jobs',None)
    new.update(settings,ollama_launcher=str(root/'start-ollama.ps1'),container_names=list(manifest['containers']),runtime_manifest=manifest)
    plan={'version':1,'root':str(root),'package':str(PACKAGE),'incoming':incoming,'originals':originals,
          'config':base64.b64encode((root/'config.json').read_bytes()).decode(),'config_hash':digest(root/'config.json'),
          'maintenance':base64.b64encode((root/'maintenance.json').read_bytes()).decode() if (root/'maintenance.json').exists() else None,
          'new_config':new,'holds':legacy_holds(root,cfg),'native':native,'project':project,
          'launcher':str(launcher),'launcher_hash':digest(launcher),
          'approval_hash':digest(root/'backlog-approved.json') if (root/'backlog-approved.json').exists() else None,
          'commit':command(['git','-C',str(PACKAGE),'rev-parse','HEAD']).strip()}
    put(plan_path,plan)
    private_path(plan_path,sid)
    return {'status':'planned','modules':len(incoming),'holds':len(plan['holds']),'services_retained':True}

def validate(plan):
    root=Path(plan['root'])
    if digest(root/'config.json')!=plan['config_hash']: raise ValueError('Config drift')
    if (root/'config.json').read_bytes()!=base64.b64decode(plan['config']): raise ValueError('Original config snapshot differs')
    actual_modules={p.name for p in root.iterdir() if p.is_file() and p.suffix in EXTENSIONS}
    if actual_modules!=set(plan['originals']): raise ValueError('Legacy module inventory drift')
    package_modules={p.name:digest(p) for p in (PACKAGE/'automation').iterdir() if p.suffix in EXTENSIONS}
    if package_modules!=plan['incoming']: raise ValueError('Incomplete/different incoming package')
    for name,data in plan['originals'].items():
        if (root/name).read_bytes()!=base64.b64decode(data): raise ValueError('Legacy code drift')
    for name,sha in plan['incoming'].items():
        if digest(PACKAGE/'automation'/name)!=sha: raise ValueError('Incoming package drift')
    cfg=load(root/'config.json')
    if legacy_holds(root,cfg)!=plan['holds']: raise ValueError('Historical approval drift')
    sha=digest(root/'backlog-approved.json') if (root/'backlog-approved.json').exists() else None
    if sha!=plan['approval_hash'] or digest(plan['launcher'])!=plan['launcher_hash']: raise ValueError('Legacy input drift')
    manifest,project=identities(cfg)
    if manifest!=plan['new_config']['runtime_manifest'] or project!=plan['project']: raise ValueError('Service drift')
    xml=ET.fromstring(plan['native']['tasks'][cfg['ollama_task']]['xml'])
    arguments=xml.findtext('{*}Actions/{*}Exec/{*}Arguments')
    match=re.fullmatch(r'-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "([^"]+)"',arguments or '')
    if not match or Path(match[1]).resolve()!=Path(plan['launcher']): raise ValueError('Launcher plan differs from task XML')
    expected=dict(cfg)
    expected.pop('legacy_recovery_jobs',None)
    expected.update(ollama_settings(plan['launcher'],cfg),ollama_launcher=str(root/'start-ollama.ps1'),
                    container_names=list(manifest['containers']),runtime_manifest=manifest)
    if expected!=plan['new_config']: raise ValueError('Planned config differs from verified legacy settings')

def begin(plan):
    validate(plan)
    root=Path(plan['root'])
    if (root/'install-state.json').exists() or (root.parent/'stack-install.json').exists(): raise ValueError('Ownership journal collision')
    backup=root/'backups'/('adoption-'+uuid.uuid4().hex)
    backup.mkdir(parents=True)
    private_path(backup,plan['native']['sid'])
    for name,data in plan['originals'].items(): (backup/name).write_bytes(base64.b64decode(data))
    (backup/'config.original.json').write_bytes(base64.b64decode(plan['config']))
    approval=root/'backlog-approved.json'
    if approval.exists(): shutil.copy2(approval,backup/approval.name)
    put(backup/'plan.json',plan)
    state={'phase':'preparing','backup':str(backup),'root':str(root),'incoming':plan['incoming'],'progress':[],
           'expected_holds':[(h['job_id'],h['recording_id'],backup.name) for h in plan['holds']]}
    put(root/'adoption-state.json',state)
    return state

def guards(root):
    root=Path(root)
    state=load(root/'adoption-state.json')
    plan=load(Path(state['backup'])/'plan.json')
    if (root/'config.json').exists(): raise ValueError('Durable config barrier absent')
    state['phase']='guarding'
    put(root/'adoption-state.json',state)
    for name in plan['originals']:
        install_guard(root,name,state)
    return {'status':'guarded'}

def installer_guard(root):
    root=Path(root)
    state=load(root/'adoption-state.json')
    plan=load(Path(state['backup'])/'plan.json')
    if 'install.ps1' in plan['originals']:
        # This obsolete installer does not read config. Persist its barrier before withdrawal.
        install_guard(root,'install.ps1',state)
    return {'status':'legacy_installer_guarded'}

def install_guard(root,name,state):
    # MoveFileEx replacement requests access denied by the deny-read lease. A rename
    # followed by creating the guard keeps the launch path either absent or refused.
    withdrawn=Path(state['backup'])/('withdrawn-'+name)
    if not withdrawn.exists(): os.rename(Path(root)/name,withdrawn)
    copy_bytes(Path(root)/name,PS_GUARD if Path(name).suffix=='.ps1' else PY_GUARD)

def stage(root, fail=None):
    root=Path(root)
    state=load(root/'adoption-state.json')
    backup=Path(state['backup'])
    plan=load(backup/'plan.json')
    if (root/'config.json').exists(): raise ValueError('Durable config barrier absent')
    if (backup/'config.withdrawn.json').read_bytes()!=base64.b64decode(plan['config']): raise ValueError('Withdrawn config drift')
    state['phase']='staging'
    state['baseline']=baseline(root)
    put(root/'adoption-state.json',state)
    with connection(root,True) as db, closing(sqlite3.connect(backup/'state.sqlite3')) as dest:
        db.backup(dest)
        if dest.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise ValueError('Backup integrity failed')
    with connection(root) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='recovery_holds'").fetchone(): raise ValueError('Unexpected existing holds')
        db.execute('BEGIN IMMEDIATE')
        for statement in HOLD_DDL.split(';'):
            if statement.strip(): db.execute(statement)
        db.executemany('INSERT INTO recovery_holds VALUES(?,?,?,?,?)',[(h['job_id'],h['recording_id'],'Historical approval missing; identity reconciliation required',time.time(),backup.name) for h in plan['holds']])
    state['progress'].append('holds')
    put(root/'adoption-state.json',state)
    if fail=='AfterHolds': os._exit(97)
    for number,(name,sha) in enumerate(plan['incoming'].items()):
        if digest(PACKAGE/'automation'/name)!=sha: raise ValueError('Incoming package drift')
        copy_bytes(root/name,(PACKAGE/'automation'/name).read_bytes())
        state['progress'].append(name)
        put(root/'adoption-state.json',state)
        if fail=='MiddleCopy' and number==2: os._exit(97)
    put(backup/'config.new.json',plan['new_config'])
    if baseline(root)!=state['baseline']: raise ValueError('Data changed during adoption')
    return {'status':'staged','modules':len(plan['incoming'])}

def commit(root, native):
    root=Path(root)
    state=load(root/'adoption-state.json')
    plan=load(Path(state['backup'])/'plan.json')
    if baseline(root)!=state['baseline']: raise ValueError('Data drift before commit')
    for name,sha in plan['incoming'].items():
        if digest(root/name)!=sha: raise ValueError('Installed package mismatch')
    config_path=Path(state['backup'])/'config.new.json'
    config_hash=digest(config_path)
    install={'phase':'installed','mode':'adopted','target':str(root.parent),'files':{k:v.upper() for k,v in plan['incoming'].items()},
             'configHash':config_hash.upper(),'tasks':{k:v['xml'] for k,v in native['tasks'].items()},'upgradePending':None,'commit':plan['commit']}
    sm={'phase':'installed','mode':'adopted','target_root':str(root.parent),'root':str(root),'project':plan['project'],
        'external_files':{p:digest(p) for p in (plan['new_config']['compose_file'],plan['new_config']['env_file'])},'files':{'automation\\config.json':config_hash}}
    state['metadata_expected']={str(root/'install-state.json'):hashlib.sha256(json.dumps(install,ensure_ascii=False,indent=2).encode()).hexdigest(),
                                str(root.parent/'stack-install.json'):hashlib.sha256(json.dumps(sm,ensure_ascii=False,indent=2).encode()).hexdigest()}
    put(root/'adoption-state.json',state)
    put(root/'install-state.json',install)
    put(root.parent/'stack-install.json',sm)
    state['phase']='installed'
    state['tasks']=native['tasks']
    put(root/'adoption-state.json',state)
    copy_bytes(root/'config.json',config_path.read_bytes())
    verify(root)
    return {'status':'installed','commit':plan['commit'],'queue_rewound':False}

def verify(root):
    root=Path(root)
    state=load(root/'install-state.json')
    adoption=load(root/'adoption-state.json')
    if state['phase']!='installed' or adoption['phase']!='installed' or state.get('upgradePending'): raise ValueError('Partial runtime')
    with connection(root,True) as db:
        actual=sorted(db.execute('SELECT job_id,recording_id,migration_id FROM recovery_holds').fetchall())
    if actual!=sorted(tuple(x) for x in adoption['expected_holds']): raise ValueError('Historical holds drift')
    if digest(root/'config.json').upper()!=state['configHash']: raise ValueError('Config drift')
    for name,sha in state['files'].items():
        if digest(root/name).upper()!=sha: raise ValueError('Code drift')
    sm=load(root.parent/'stack-install.json')
    for path,sha in sm['external_files'].items():
        if digest(path)!=sha: raise ValueError('External file drift')
    return {'status':'verified','modules':len(state['files']),'commit':state.get('commit')}

def rollback_check(root):
    root=Path(root)
    state=load(root/'adoption-state.json')
    plan=load(Path(state['backup'])/'plan.json')
    installed=state['phase']=='installed' or (state['phase']=='rollback_preparing' and state.get('rollbackFrom')=='installed')
    for path in (root/'install-state.json',root.parent/'stack-install.json'):
        expected=state.get('metadata_expected',{}).get(str(path))
        if installed and not path.exists(): raise ValueError('Missing ownership metadata; rollback refused')
        if path.exists() and (not expected or digest(path)!=expected): raise ValueError('Ownership metadata drift; rollback refused')
    for path,key in ((plan['new_config']['compose_file'],'compose_sha256'),(plan['new_config']['env_file'],'env_sha256')):
        if digest(path)!=plan['new_config']['runtime_manifest'][key]: raise ValueError('External file drift; rollback refused')
    if digest(plan['launcher'])!=plan['launcher_hash']: raise ValueError('Legacy launcher drift; rollback refused')
    if state.get('baseline') and baseline(root)!=state['baseline']: raise ValueError('Data drift; no automatic rollback/rewind')
    for name,sha in plan['incoming'].items():
        path=root/name
        if not path.exists():
            if installed: raise ValueError('Missing installed code; rollback refused')
            continue
        actual=digest(path)
        original=hashlib.sha256(base64.b64decode(plan['originals'][name])).hexdigest() if name in plan['originals'] else None
        guard=hashlib.sha256(PS_GUARD if path.suffix=='.ps1' else PY_GUARD).hexdigest()
        if actual not in (sha,original,guard): raise ValueError('Code drift; rollback refused')
    cfg=root/'config.json'
    expected=[plan['config_hash']]
    new=Path(state['backup'])/'config.new.json'
    if new.exists(): expected.append(digest(new))
    if cfg.exists() and digest(cfg) not in expected: raise ValueError('Config drift; rollback refused')
    with connection(root,True) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='recovery_holds'").fetchone():
            if any(r[0]!=(Path(state['backup']).name) for r in db.execute('SELECT migration_id FROM recovery_holds')): raise ValueError('Foreign holds; rollback refused')
            if installed and sorted(db.execute('SELECT job_id,recording_id,migration_id FROM recovery_holds'))!=sorted(tuple(x) for x in state['expected_holds']): raise ValueError('Historical holds drift; rollback refused')
        elif installed: raise ValueError('Missing installed holds; rollback refused')
    return state,plan

def rollback(root):
    root=Path(root)
    state,plan=rollback_check(root)
    cfg=root/'config.json'
    state['phase']='rolling_back'
    put(root/'adoption-state.json',state)
    if cfg.exists(): os.replace(cfg,Path(state['backup'])/'config.rollback-withdrawn.json')
    with connection(root) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='recovery_holds'").fetchone():
            if any(r[0]!=(Path(state['backup']).name) for r in db.execute('SELECT migration_id FROM recovery_holds')): raise ValueError('Foreign holds; rollback refused')
            db.execute('DROP TABLE recovery_holds')
    for name,data in plan['originals'].items():
        # The legacy installer has no config gate. Keep its guard until old tasks
        # and configuration are coherent, including a crash during rollback.
        copy_bytes(root/name,PS_GUARD if name=='install.ps1' else base64.b64decode(data))
    for name in set(plan['incoming'])-set(plan['originals']): (root/name).unlink(missing_ok=True)
    for path in (root/'install-state.json',root.parent/'stack-install.json'): path.unlink(missing_ok=True)
    # Orchestrator restores tasks/maintenance before the final old-config release.
    return {'status':'rollback_staged','queue_rewound':False}

def release_old(root):
    root=Path(root)
    state=load(root/'adoption-state.json')
    plan=load(Path(state['backup'])/'plan.json')
    if state['phase']!='rolling_back': raise ValueError('Rollback not staged')
    copy_bytes(root/'config.json',base64.b64decode(plan['config']))
    if 'install.ps1' in plan['originals']: copy_bytes(root/'install.ps1',base64.b64decode(plan['originals']['install.ps1']))
    (root/'adoption-state.json').unlink()
    return {'status':'rolled_back'}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('inspect','validate','begin','installer-guard','guards','stage','commit','verify','rollback-check','rollback','release-old'))
    p.add_argument('--root',required=True)
    p.add_argument('--plan')
    p.add_argument('--native')
    p.add_argument('--fail')
    a=p.parse_args()
    if a.action=='inspect': result=inspect(a.root,load(a.native),a.plan)
    elif a.action=='validate': validate(load(a.plan)); result={'status':'validated'}
    elif a.action=='begin': result=begin(load(a.plan))
    elif a.action=='guards': result=guards(a.root)
    elif a.action=='installer-guard': result=installer_guard(a.root)
    elif a.action=='stage': result=stage(a.root,a.fail)
    elif a.action=='commit': result=commit(a.root,load(a.native))
    elif a.action=='verify': result=verify(a.root)
    elif a.action=='rollback-check': rollback_check(a.root); result={'status':'rollback_preflight_passed'}
    elif a.action=='release-old': result=release_old(a.root)
    else: result=rollback(a.root)
    print(json.dumps(result))

if __name__=='__main__': main()
