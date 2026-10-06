"""Deployment control. All mutations need an explicit subcommand; preflight is read-only."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'automation'))
from common import atomic_json, sha256, CREATE_NO_WINDOW, powershell_env

def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def run(args, **kw):
    if Path(str(args[0])).name.lower()=='powershell.exe' and 'env' not in kw:
        kw['env']=powershell_env()
    return subprocess.run([str(x) for x in args],check=True,creationflags=CREATE_NO_WINDOW,**kw)

def canonical(path):
    p=Path(path).resolve(strict=False)
    # strict resolution of existing ancestors catches broken/inaccessible junctions.
    ancestor=p
    while not ancestor.exists() and ancestor.parent != ancestor:
        ancestor=ancestor.parent
    ancestor.resolve(strict=True)
    return p

def overlap(a,b):
    a,b=canonical(a),canonical(b)
    return a==b or a in b.parents or b in a.parents

def executable(value):
    found=shutil.which(value)
    if not found or not Path(found).is_file():
        raise ValueError('Executable not found: '+value)
    return str(Path(found).resolve())

def validate(cfg, config_path=None, binaries=True):
    if not re.fullmatch(r'[a-z][a-z0-9-]{2,35}', cfg.get('project','')):
        raise ValueError('project: use 3–36 lowercase letters/digits/hyphens')
    target=canonical(cfg['target_root'])
    if target==Path(target.anchor) or overlap(target,ROOT):
        raise ValueError('Runtime root overlaps repository or drive root')
    protected=[cfg['vault_root'],cfg['vault'],*cfg['sources']]
    if config_path: protected.append(str(config_path))
    if cfg.get('secrets_file'): protected.append(cfg['secrets_file'])
    for item in protected:
        if overlap(target,item):
            raise ValueError('Runtime root overlaps protected external input')
    vault,root=canonical(cfg['vault']),canonical(cfg['vault_root'])
    if root not in vault.parents:
        raise ValueError('vault must be a dedicated subdirectory of vault_root')
    if not root.is_dir() or any(not canonical(s).is_dir() for s in cfg['sources']):
        raise ValueError('Create vault_root and source directories before setup')
    if overlap(target,cfg['ollama_models']) or overlap(ROOT,cfg['ollama_models']):
        raise ValueError('Ollama models must be outside repository/runtime root')
    ports=[cfg[k] for k in ('app_port','asr_port','llm_port')]
    if any(type(p)!=int or not 1024 <= p <= 65535 for p in ports) or len(set(ports))!=3:
        raise ValueError('Use three distinct nonprivileged ports')
    if not re.fullmatch(r'[a-zA-Z0-9_.:/-]+',cfg['llm_model']):
        raise ValueError('Invalid model name')
    if ':' not in cfg['llm_model'].split('/')[-1] or not cfg['llm_model'].split(':')[-1]:
        raise ValueError('Use an explicit model alias tag, e.g. my-summary:v1')
    if cfg['hardware_profile'] not in ('blackwell-5070ti','nvidia'):
        raise ValueError('Unsupported hardware profile')
    result=dict(cfg)
    if binaries:
        for key in ('python','pythonw','docker','ffmpeg','ffprobe'):
            result[key]=executable(cfg[key])
        if cfg.get('managed_ollama'): result['ollama']=executable(cfg['ollama'])
        if not Path(cfg['docker_desktop']).is_file():
            raise ValueError('Docker Desktop executable missing')
    return result

def env_file(path):
    result={}
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        if not line.strip() or line.lstrip().startswith('#'): continue
        key,value=line.split('=',1)
        if '\x00' in value or '\n' in value or '\r' in value: raise ValueError('Invalid env value')
        value=value.strip()
        if len(value)>=2 and value[0]==value[-1] and value[0] in ("'",'"'):
            value=value[1:-1]
        result[key.strip()]=value
    return result

def check_credentials(env):
    for key in ('ADMIN_EMAIL','ADMIN_PASSWORD','HF_TOKEN'):
        value=env.get(key,'')
        if not value or 'replace-' in value or value.lower() in ('changeme','admin@example.com'):
            raise ValueError('Set your own '+key+' in the external secrets file')
    if len(env['ADMIN_PASSWORD'])<16 or not env['HF_TOKEN'].startswith('hf_'):
        raise ValueError('Use a 16+ character admin password and a HF token')

def write_env(path, env):
    # Compose interpolation must never reinterpret a password containing dollars.
    if any("'" in str(v) for v in env.values()):
        raise ValueError('Single quotes unsupported in env inputs; use other password characters')
    Path(path).write_text(''.join(f"{k}='{v}'\n" for k,v in env.items()),encoding='utf-8')

def manifest_path(cfg): return Path(cfg['target_root'])/'stack-install.json'

def verify_owned(cfg):
    m=load(manifest_path(cfg))
    required={'stack/.env','stack/hf-token.env','stack/compose.yaml','automation/config.json'}
    if m.get('phase')!='installed' or not required.issubset({n.replace('\\','/') for n in m['files']}):
        raise ValueError('Preparation incomplete; preserve the root and use a fresh runtime or explicit audited cleanup')
    if m['project']!=cfg['project'] or canonical(m['target_root'])!=canonical(cfg['target_root']):
        raise ValueError('Runtime ownership mismatch')
    for name,digest in m['files'].items():
        p=Path(cfg['target_root'])/name
        planned=m.get('up_intent',{}).get('planned_config_hash') if m.get('up_phase')=='starting' and name.replace('\\','/')=='automation/config.json' else None
        if not p.is_file() or sha256(p) not in (digest,planned): raise ValueError('Runtime file drift: '+name)
    return m

def prepare(cfg, config_path, secret_path):
    cfg=validate(dict(cfg,secrets_file=str(secret_path)),config_path)
    env=env_file(secret_path)
    check_credentials(env)
    target=canonical(cfg['target_root'])
    if target.exists() and any(target.iterdir()):
        raise ValueError('Fresh prepare requires an empty root; use owned upgrade for existing runtime')
    target.mkdir(parents=True,exist_ok=True)
    m={'project':cfg['project'],'target_root':str(target),'phase':'prepared','files':{},'external_inputs':[str(config_path),str(secret_path)]}
    atomic_json(manifest_path(cfg),m)
    stack=target/'stack'
    bridge=target/'automation'
    stack.mkdir()
    bridge.mkdir()
    deps=load(ROOT/'config/dependencies.json')
    profile=load(ROOT/'config'/ (cfg['hardware_profile']+'.json'))
    runtime_env={
        'STACK_PROJECT':cfg['project'],'APP_IMAGE':'speakr-obsidian-stack:73ba1f9',
        'ASR_IMAGE':deps['asr']['image'],'APP_PORT':cfg['app_port'],'ASR_PORT':cfg['asr_port'],
        'ASR_BATCH_SIZE':profile['asr']['BATCH_SIZE'],
        'SECRET_KEY':secrets.token_hex(32),'ADMIN_EMAIL':env['ADMIN_EMAIL'],
        'ADMIN_USERNAME':'admin','ADMIN_PASSWORD':env['ADMIN_PASSWORD'],
        'TEXT_MODEL_BASE_URL':f"http://host.docker.internal:{cfg['llm_port']}/v1",
        'CHAT_MODEL_BASE_URL':f"http://host.docker.internal:{cfg['llm_port']}/v1",
        'TEXT_MODEL_API_KEY':'local-only','CHAT_MODEL_API_KEY':'local-only',
        'TEXT_MODEL_NAME':cfg['llm_model'],'CHAT_MODEL_NAME':cfg['llm_model'],
        'ASR_BASE_URL':'http://whisperx-asr:9000','USE_ASR_ENDPOINT':'true',
        'ASR_DIARIZE':'true','ASR_RETURN_SPEAKER_EMBEDDINGS':'true',
        'JOB_QUEUE_WORKERS':'1','SUMMARY_QUEUE_WORKERS':'1','ALLOW_REGISTRATION':'false',
        'ENABLE_STREAM_OPTIONS':'false','LLM_REQUEST_TIMEOUT':'600','LLM_MAX_RETRIES':'0',
        'AUDIO_CODEC':'mp3','AUDIO_COMPRESS_UPLOADS':'true',
    }
    write_env(stack/'.env',runtime_env)
    write_env(stack/'hf-token.env',{'HF_TOKEN':env['HF_TOKEN']})
    shutil.copy2(ROOT/'compose.yaml',stack/'compose.yaml')
    bridge_cfg={k:cfg[k] for k in ('sources','vault_root','vault','pythonw','ffmpeg','ffprobe','docker','docker_desktop','scan_seconds','stable_seconds','completed_poll_seconds','max_audio_bytes')}
    bridge_cfg.update(speakr_url=f"http://127.0.0.1:{cfg['app_port']}", env_file=str(stack/'.env'),
        state_dir=str(bridge),compose_file=str(stack/'compose.yaml'),bridge_task=cfg['project']+' Bridge',
        ollama_task=cfg['project']+' Ollama',watchdog_task=cfg['project']+' Watchdog',
        llm_url=f"http://127.0.0.1:{cfg['llm_port']}",llm_model=cfg['llm_model'],
        speakr_health_url=f"http://127.0.0.1:{cfg['app_port']}/login",asr_health_url=f"http://127.0.0.1:{cfg['asr_port']}/health",
        recovery_enabled=True,legacy_recovery_jobs=[],auto_start_docker=cfg.get('auto_start_docker',False),
        managed_ollama=cfg['managed_ollama'],ollama_launcher=str(bridge/'start-ollama.ps1'),
        container_names=[cfg['project']+'-app',cfg['project']+'-asr'],
        ollama=cfg['ollama'],ollama_models=cfg['ollama_models'],llm_port=cfg['llm_port'],
        ollama_tuning=profile['ollama'],supervision_candidate=False)
    atomic_json(bridge/'config.json',bridge_cfg)
    for p in (stack/'.env',stack/'hf-token.env',stack/'compose.yaml',bridge/'config.json'):
        m['files'][str(p.relative_to(target))]=sha256(p)
    m['phase']='installed'
    atomic_json(manifest_path(cfg),m)
    print('Prepared owned stack, bridge not installed/active. Next: build, model, up, install, Resume.')

def compose(cfg,*args,capture=False):
    directory=Path(cfg['target_root'])/'stack'
    inputs=env_file(directory/'.env')
    environment=dict(os.environ)
    for key in inputs: environment.pop(key,None)
    for key in list(environment):
        if key.startswith('COMPOSE_'): environment.pop(key,None)
    environment.update({k:str(v) for k,v in inputs.items()})
    return run([cfg['docker'],'compose','--project-name',cfg['project'],'--project-directory',directory,
        '--env-file',directory/'.env','-f',directory/'compose.yaml',*args],
        capture_output=capture,text=True,env=environment)

def validate_compose(cfg):
    conf=json.loads(compose(cfg,'config','--format','json',capture=True).stdout)
    expected=env_file(Path(cfg['target_root'])/'stack/.env')
    if conf['name']!=cfg['project'] or set(conf['services'])!={'app','whisperx-asr'}:
        raise ValueError('Compose project/services differ')
    for service,suffix,port_key in (('app','app','app_port'),('whisperx-asr','asr','asr_port')):
        value=conf['services'][service]
        image=expected['APP_IMAGE' if service=='app' else 'ASR_IMAGE']
        if value['container_name']!=cfg['project']+'-'+suffix or value['image']!=image:
            raise ValueError('Compose service identity differs')
        ports=value.get('ports',[])
        if len(ports)!=1 or ports[0].get('host_ip')!='127.0.0.1' or int(ports[0]['published'])!=cfg[port_key]:
            raise ValueError('Compose loopback ports differ')
        for volume in value.get('volumes',[]):
            p=canonical(volume['source'])
            if canonical(cfg['target_root']) not in p.parents:
                raise ValueError('Compose volume outside owned runtime')
    return conf

def hardware_preflight(cfg):
    if os.name!='nt': raise ValueError('Deployment control supports Windows only')
    tool=executable('nvidia-smi.exe')
    gpu=run([tool,'--query-gpu=name,driver_version','--format=csv,noheader'],capture_output=True,text=True).stdout
    if not gpu.strip(): raise ValueError('No NVIDIA GPU/driver detected')
    if cfg['hardware_profile']=='blackwell-5070ti' and '5070ti' not in re.sub(r'\s+','',gpu).lower():
        raise ValueError('Reference profile requires RTX 5070 Ti; choose and validate another profile')
    platform=run([cfg['docker'],'info','--format','{{.OSType}}/{{.Architecture}}'],capture_output=True,text=True).stdout.strip()
    if platform not in ('linux/x86_64','linux/amd64'): raise ValueError('Use Docker Linux/amd64 engine for the pinned ASR image')

def identities(cfg):
    m=verify_owned(cfg)
    conf=validate_compose(cfg)
    if conf['name']!=cfg['project']: raise ValueError('Compose project identity differs')
    result={}
    for service,suffix in (('app','app'),('whisperx-asr','asr')):
        name=cfg['project']+'-'+suffix
        rows=json.loads(run([cfg['docker'],'inspect',name],capture_output=True,text=True).stdout)
        row=rows[0]
        labels=row['Config']['Labels']
        ref=conf['services'][service]['image']
        image=run([cfg['docker'],'image','inspect',ref,'--format','{{.Id}}'],capture_output=True,text=True).stdout.strip()
        if labels.get('com.docker.compose.project')!=cfg['project'] or labels.get('com.docker.compose.service')!=service or image!=row['Image']:
            raise ValueError('Unowned or drifted container')
        result[name]={'service':service,'project':cfg['project'],'image_id':image,'image_ref':ref}
    return {'compose_sha256':sha256(Path(cfg['target_root'])/'stack/compose.yaml'),
            'env_sha256':sha256(Path(cfg['target_root'])/'stack/.env'),'containers':result}

def verify_model(cfg, deps, tags):
    model=next((m for m in tags.get('models',[]) if model_key(m.get('name',''))==model_key(deps['model']['name'])),None)
    if not model or digest(model.get('digest',''))!=digest(deps['model']['digest']):
        raise ValueError('Base model digest mismatch. Review dependency refresh; no inference/alias creation.')

def digest(value):
    value=value.removeprefix('sha256:')
    if not re.fullmatch('[0-9a-f]{64}',value): raise ValueError('Invalid model digest')
    return value

def model_key(name):
    # A registry host port is not a model tag. Ollama versions may shorten :latest.
    tail=name.rsplit('/',1)[-1]
    return name if ':' in tail else name+':latest'

def alias_identity(cfg,tags):
    path=Path(cfg['target_root'])/'model-state.json'
    state=load(path)
    alias=next((m for m in tags.get('models',[]) if model_key(m.get('name',''))==model_key(cfg['llm_model'])),None)
    if not alias or digest(alias.get('digest',''))!=state['alias_digest'] or state['alias_name']!=cfg['llm_model'] or state['modelfile_sha256']!=sha256(ROOT/'models/Modelfile'):
        raise ValueError('Alias identity differs from owned model recipe')
    verify_model(cfg,load(ROOT/'config/dependencies.json'),tags)
    verify_parameters(request(f"http://127.0.0.1:{cfg['llm_port']}/api/show",{'model':cfg['llm_model']}))

def verify_parameters(show):
    actual={}
    for line in show.get('parameters','').splitlines():
        items=line.split()
        if len(items)==2: actual[items[0]]=items[1]
    for line in (ROOT/'models/Modelfile').read_text().splitlines():
        if line.startswith('PARAMETER '):
            _,key,value=line.split()
            if key not in actual or float(actual[key])!=float(value):
                raise ValueError('Alias parameter mismatch: '+key)

def request(url, data=None):
    op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    raw=None if data is None else json.dumps(data).encode()
    with op.open(urllib.request.Request(url,raw,{'Content-Type':'application/json'}),timeout=600) as r:
        return json.load(r)

def model(cfg):
    verify_owned(cfg)
    url=f"http://127.0.0.1:{cfg['llm_port']}"
    deps=load(ROOT/'config/dependencies.json')
    if model_key(cfg['llm_model'])==model_key(deps['model']['name']):
        raise ValueError('Alias must differ from public base model name')
    tags=request(url+'/api/tags')
    if any(model_key(m.get('name',''))==model_key(cfg['llm_model']) for m in tags.get('models',[])):
        alias_identity(cfg,tags)
        print('Owned model alias already verified; unchanged')
        return
    request(url+'/api/pull',{'model':deps['model']['name'],'stream':False})
    tags=request(url+'/api/tags')
    verify_model(cfg,deps,tags)
    # Use the versioned Modelfile via Ollama's CLI against the chosen local server.
    env=dict(os.environ,OLLAMA_HOST=url)
    run([cfg['ollama'],'create',cfg['llm_model'],'-f',ROOT/'models/Modelfile'],env=env)
    tags=request(url+'/api/tags')
    verify_model(cfg,deps,tags)
    alias=next(m for m in tags['models'] if model_key(m['name'])==model_key(cfg['llm_model']))
    verify_parameters(request(url+'/api/show',{'model':cfg['llm_model']}))
    atomic_json(Path(cfg['target_root'])/'model-state.json',{'alias_name':cfg['llm_model'],'alias_digest':digest(alias['digest']),'base_digest':digest(deps['model']['digest']),'modelfile_sha256':sha256(ROOT/'models/Modelfile')})

def up(cfg):
    verify_owned(cfg)
    hardware_preflight(cfg)
    validate_compose(cfg)  # effective interpolation/identity checked BEFORE any up effect.
    image=run([cfg['docker'],'image','inspect','speakr-obsidian-stack:73ba1f9','--format','{{json .Config.Labels}}'],capture_output=True,text=True)
    from build import policy_digest
    labels=json.loads(image.stdout)
    if labels.get('org.speakr-obsidian-stack.source')!=load(ROOT/'config/dependencies.json')['speakr']['commit'] or labels.get('org.speakr-obsidian-stack.policy-sha256')!=policy_digest():
        raise ValueError('Built app image provenance differs')
    # No adoption of existing containers: inspect labels before Compose mutates anything.
    for suffix in ('app','asr'):
        name=cfg['project']+'-'+suffix
        p=subprocess.run([cfg['docker'],'inspect',name],capture_output=True,text=True,creationflags=CREATE_NO_WINDOW)
        if p.returncode==0:
            raise ValueError('Container already exists; use owned start, never adopt via up: '+name)
        if 'no such' not in p.stderr.lower():
            raise ValueError('Cannot inspect container collision: '+name)
    import socket
    for key in ('app_port','asr_port'):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',cfg[key]))
    check_credentials(env_file(Path(cfg['target_root'])/'stack/.env') | env_file(Path(cfg['target_root'])/'stack/hf-token.env'))
    tags=request(f"http://127.0.0.1:{cfg['llm_port']}/api/tags")
    alias_identity(cfg,tags)
    conf=validate_compose(cfg)
    planned={}
    for service in ('app','whisperx-asr'):
        spec=conf['services'][service]
        ref=spec['image']
        probe=subprocess.run([cfg['docker'],'image','inspect',ref,'--format','{{.Id}}'],capture_output=True,text=True,creationflags=CREATE_NO_WINDOW)
        if probe.returncode:
            run([cfg['docker'],'pull',ref])  # explicit Up download, before container effects.
            image_id=run([cfg['docker'],'image','inspect',ref,'--format','{{.Id}}'],capture_output=True,text=True).stdout.strip()
        else: image_id=probe.stdout.strip()
        planned[spec['container_name']]={'project':cfg['project'],'service':service,'image_ref':ref,'image_id':image_id,
            'volumes':spec.get('volumes',[]),'ports':spec.get('ports',[])}
    m=load(manifest_path(cfg))
    m['up_phase']='starting'
    m['up_intent']={'containers':planned}
    atomic_json(manifest_path(cfg),m)  # durable identity intent BEFORE compose up.
    compose(cfg,'up','-d','--no-build')
    finish_up(cfg)

def mount_key(value):
    value=value.replace('\\','/')
    match=re.match(r'^/run/desktop/mnt/host/([a-z])/(.*)$',value)
    if match: value=match[1]+':/'+match[2]
    return value.rstrip('/').lower()

def check_created(cfg,name,expected,row):
    labels=row['Config'].get('Labels') or {}
    if row['Image']!=expected['image_id'] or labels.get('com.docker.compose.project')!=cfg['project'] or labels.get('com.docker.compose.service')!=expected['service']:
        raise ValueError('Created container identity drift: '+name)
    mounts={(mount_key(v['Source']),v['Destination']) for v in row.get('Mounts',[]) if v['Type']=='bind'}
    wanted={(mount_key(v['source']),v['target']) for v in expected['volumes'] if v['type']=='bind'}
    if mounts!=wanted: raise ValueError('Container mount drift: '+name)
    ports=row['HostConfig'].get('PortBindings') or {}
    wanted_ports={str(v['target'])+'/tcp':[{'HostIp':'127.0.0.1','HostPort':str(v['published'])}] for v in expected['ports']}
    if ports!=wanted_ports: raise ValueError('Container port drift: '+name)

def recover_start(cfg):
    m=verify_owned(cfg)
    validate_compose(cfg)
    planned=m.get('up_intent',{}).get('containers')
    if not planned: raise ValueError('No owned Up intent. Run up first; nothing was started')
    run([cfg['docker'],'info','--format','{{.ServerVersion}}'],capture_output=True,text=True)
    # Global drift preflight before first start/recreate effect.
    actions=[]
    for name,expected in planned.items():
        image=run([cfg['docker'],'image','inspect',expected['image_ref'],'--format','{{.Id}}'],capture_output=True,text=True).stdout.strip()
        if image!=expected['image_id']: raise ValueError('Planned image drift')
        p=subprocess.run([cfg['docker'],'inspect',name],capture_output=True,text=True,creationflags=CREATE_NO_WINDOW)
        if p.returncode:
            if 'no such' not in p.stderr.lower(): raise ValueError('Cannot inspect planned container')
            actions.append(('create',expected['service']))
        else:
            row=json.loads(p.stdout)[0]
            check_created(cfg,name,expected,row)
            if not row['State']['Running']: actions.append(('start',name))
    for action,value in actions:
        if action=='start': run([cfg['docker'],'start',value])
        else: compose(cfg,'up','-d','--no-build','--pull','never','--no-recreate','--no-deps',value)
    finish_up(cfg)

def finish_up(cfg):
    m=verify_owned(cfg)
    for name,expected in m['up_intent']['containers'].items():
        row=json.loads(run([cfg['docker'],'inspect',name],capture_output=True,text=True).stdout)[0]
        check_created(cfg,name,expected,row)
    runtime=identities(cfg)
    cfgpath=Path(cfg['target_root'])/'automation/config.json'
    bc=load(cfgpath)
    m['up_phase']='starting'
    atomic_json(manifest_path(cfg),m)
    if bc.get('runtime_manifest')!=runtime:
        if (cfgpath.parent/'install-state.json').exists():
            raise ValueError('Installed runtime identity changed; explicit migration required, config preserved')
        bc['runtime_manifest']=runtime
        encoded=json.dumps(bc,ensure_ascii=False,indent=2).encode('utf-8')
        m['up_intent']['planned_config_hash']=hashlib.sha256(encoded).hexdigest()
        atomic_json(manifest_path(cfg),m)
        atomic_json(cfgpath,bc)
        m['files'][str(cfgpath.relative_to(Path(cfg['target_root'])))]=sha256(cfgpath)
        atomic_json(manifest_path(cfg),m)
    # Probe FROM container. Failure never opens host listener to LAN.
    run([cfg['docker'],'exec',cfg['project']+'-app','python','-c',
         "import urllib.request; urllib.request.urlopen('http://host.docker.internal:"+str(cfg['llm_port'])+"/api/tags',timeout=10).read()"])
    m['runtime_manifest']=runtime
    m['up_phase']='ready'
    atomic_json(manifest_path(cfg),m)
    print('Stack started, identities recorded; bridge remains inactive. Check status, then install.')

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['preflight','prepare','build','model','up','start','status','install','pause','resume','rollback','backup','upgrade'])
    p.add_argument('--config',required=True)
    p.add_argument('--secrets')
    p.add_argument('--cold',action='store_true')
    p.add_argument('--candidate',action='store_true',help='Isolated control validation only; no container control')
    a=p.parse_args()
    cfg=validate(load(a.config),a.config)
    if a.action=='preflight':
        if a.secrets: check_credentials(env_file(a.secrets))
        print(json.dumps({'project':cfg['project'],'runtime_root':cfg['target_root'],'mutations':False}))
    elif a.action=='prepare':
        if not a.secrets: p.error('--secrets required')
        prepare(cfg,a.config,a.secrets)
    elif a.action=='build':
        from build import build
        build(cfg['docker'],'speakr-obsidian-stack:73ba1f9',a.cold)
    elif a.action=='model': model(cfg)
    elif a.action=='up': up(cfg)
    elif a.action=='start':
        recover_start(cfg)
    elif a.action=='status':
        verify_owned(cfg)
        compose(cfg,'ps')
        run(['powershell.exe','-NoProfile','-File',ROOT/'automation/recovery-control.ps1','-Root',Path(cfg['target_root'])/'automation','-Action','Status'])
    elif a.action in ('install','rollback','upgrade'):
        if a.action!='rollback': verify_owned(cfg)
        args=['powershell.exe','-NoProfile','-File',ROOT/'scripts/install.ps1','-TargetRoot',cfg['target_root'],'-Action',a.action.capitalize(),'-Python',cfg['python']]
        if a.candidate: args+=['-Candidate']
        run(args)
    elif a.action in ('pause','resume'):
        verify_owned(cfg)
        run(['powershell.exe','-NoProfile','-File',ROOT/'automation/recovery-control.ps1','-Root',Path(cfg['target_root'])/'automation','-Action',a.action.capitalize()])
    elif a.action=='backup':
        verify_owned(cfg)
        bridge=Path(cfg['target_root'])/'automation'
        dest=Path(cfg['target_root'])/'backups'/secrets.token_hex(8)
        dest.mkdir(parents=True)
        run([cfg['python'],ROOT/'automation/backup-state.py','--root',bridge,'--destination',dest/'state.sqlite3'])
        print('Bridge queue online snapshot saved. Speakr DB backup: see docs/operations.md.')

if __name__=='__main__':
    try: main()
    except Exception as exc:
        print(type(exc).__name__+': '+str(exc),file=sys.stderr)
        sys.exit(1)
