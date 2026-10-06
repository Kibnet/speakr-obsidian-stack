"""Empty runtime -> partial owned Compose Up -> fresh-process Start -> native Install.

Actual pinned app; only the ASR/LLM fixtures are substituted, with no GPU access.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import urllib.request
import wave

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import stack
sys.path.insert(0,str(ROOT/'automation'))
from speakr_api import SpeakrAPI

def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0))
        return s.getsockname()[1]

def main():
    project='combined-'+uuid.uuid4().hex[:10]
    base=Path(tempfile.mkdtemp(prefix=project+'-')).resolve()
    for name in ('vault','audio','models'): (base/name).mkdir()
    (base/'audio/old.wav').write_bytes(b'baseline only')
    cfg=stack.load(ROOT/'config/stack.example.json')
    cfg.update(project=project,target_root=str(base/'runtime'),vault_root=str(base/'vault'),vault=str(base/'vault/notes'),
        sources=[str(base/'audio')],ollama_models=str(base/'models'),managed_ollama=False,
        app_port=port(),asr_port=port(),llm_port=port(),scan_seconds=1,stable_seconds=0,completed_poll_seconds=0)
    cfg=stack.validate(cfg,base/'input.json')
    stack.atomic_json(base/'input.json',cfg)
    secret=base/'secret.env'
    secret.write_text('ADMIN_EMAIL=fixture@example.com\nADMIN_PASSWORD=synthetic-only-password\nHF_TOKEN=hf_synthetic\n')
    stack.prepare(cfg,base/'input.json',secret)
    target=Path(cfg['target_root'])
    directory=target/'stack'
    bridge=target/'automation'
    deps=stack.load(ROOT/'config/dependencies.json')
    parameters='\n'.join(line.removeprefix('PARAMETER ') for line in (ROOT/'models/Modelfile').read_text().splitlines() if line.startswith('PARAMETER '))
    image='speakr-obsidian-stack:73ba1f9'
    llm_name=project+'-llm-fixture'
    installed=False
    def docker(*args,check=True):
        p=subprocess.run([cfg['docker'],*map(str,args)],capture_output=True,text=True,timeout=60)
        if check and p.returncode: raise RuntimeError(p.stderr[-1500:])
        return p.stdout.strip()
    def native(*args):
        try: result=stack.run(['powershell.exe','-NoProfile','-File',*args],capture_output=True,text=True,timeout=120)
        except subprocess.CalledProcessError as exc:
            print(exc.stdout,exc.stderr,flush=True);raise
        print(result.stdout,flush=True)
        if result.stderr: print(result.stderr,flush=True)
    original_compose=stack.compose
    try:
        shutil.copy2(ROOT/'tests/stub_services.py',directory/'stub.py')
        shutil.copy2(ROOT/'tests/app_network_probe.py',directory/'network_probe.py')
        # Controlled fixture env; no weights are loaded. Digest/checking logic is exercised.
        env=stack.env_file(directory/'.env')
        env.update(ASR_IMAGE=image,DISABLE_VOICE_EMBEDDING_CHECK='true',LLM_REQUEST_TIMEOUT='5',LLM_MAX_RETRIES='0',
                   HTTP_PROXY=f"http://host.docker.internal:{cfg['llm_port']}")
        stack.write_env(directory/'.env',env)
        conf=json.loads(original_compose(cfg,'config','--format','json',capture=True).stdout)
        service=conf['services']['whisperx-asr']
        service['image']=image
        service.pop('deploy',None)
        service['entrypoint']=['python','-S','/fixture.py']
        service['command']=[]
        service['volumes']=[{'type':'bind','source':str(directory/'stub.py'),'target':'/fixture.py','read_only':True}]
        conf['services']['app']['volumes'].append({'type':'bind','source':str(directory/'network_probe.py'),'target':'/network_probe.py','read_only':True})
        (directory/'compose.yaml').write_text(json.dumps(conf,indent=2),encoding='utf-8')
        m=stack.load(stack.manifest_path(cfg))
        for p in (directory/'.env',directory/'compose.yaml',directory/'stub.py',directory/'network_probe.py'):
            m['files'][str(p.relative_to(target))]=stack.sha256(p)
        stack.atomic_json(stack.manifest_path(cfg),m)
        stack.atomic_json(target/'model-state.json',{'alias_name':cfg['llm_model'],'alias_digest':'b'*64,'base_digest':stack.digest(deps['model']['digest']),'modelfile_sha256':stack.sha256(ROOT/'models/Modelfile')})
        docker('run','-d','--name',llm_name,'-p',f"127.0.0.1:{cfg['llm_port']}:9000",
               '-v',str(directory/'stub.py')+':/fixture.py:ro','-e','STUB_MODEL='+cfg['llm_model'],
               '-e','STUB_BASE_DIGEST='+stack.digest(deps['model']['digest']),'-e','STUB_PARAMETERS='+parameters,
               '--entrypoint','python',image,'-S','/fixture.py')
        for _ in range(20):
            try: stack.request(f"http://127.0.0.1:{cfg['llm_port']}/api/tags"); break
            except Exception: time.sleep(1)
        def partial(compose_cfg,*args,**kwargs):
            if args and args[0]=='up':
                original_compose(compose_cfg,'up','-d','--no-build','--no-deps','app')
                raise RuntimeError('Injected partial Up after own app creation')
            return original_compose(compose_cfg,*args,**kwargs)
        stack.compose=partial
        stack.hardware_preflight=lambda _: None  # fixture ASR has no GPU reservation/device.
        try: stack.up(cfg)
        except RuntimeError as exc:
            if 'Injected partial Up' not in str(exc): raise
        else: raise AssertionError('Partial Up injection missing')
        assert stack.load(stack.manifest_path(cfg))['up_phase']=='starting'
        stack.compose=original_compose
        # A NEW process reconciles persisted planned labels/images/mounts/ports, then creates missing ASR only.
        subprocess.run([sys.executable,ROOT/'scripts/stack.py','start','--config',base/'input.json'],check=True,timeout=120)
        assert stack.load(stack.manifest_path(cfg))['up_phase']=='ready'
        bc=stack.load(bridge/'config.json')
        api=SpeakrAPI(bc)
        for _ in range(90):
            try: api.login(); break
            except Exception: time.sleep(1)
        else: raise AssertionError('App not ready')
        native(ROOT/'scripts/install.ps1','-TargetRoot',target,'-Python',cfg['python'])
        installed=True
        assert stack.verify_owned(cfg)['up_phase']=='ready'
        original_config=stack.sha256(bridge/'config.json')
        subprocess.run([sys.executable,ROOT/'scripts/stack.py','start','--config',base/'input.json'],check=True,timeout=120)
        assert stack.sha256(bridge/'config.json')==original_config
        native(ROOT/'automation/recovery-control.ps1','-Root',bridge,'-Action','Resume')
        audio=base/'audio/new.wav'
        with wave.open(str(audio),'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(b'\0\0'*16000)
        limit=time.time()+180
        notes=[]
        while time.time()<limit:
            notes=list((base/'vault').rglob('*.md'))
            if any('Синтетическая проверка' in n.read_text(encoding='utf-8') and 'summary_status: "ready"' in n.read_text(encoding='utf-8') for n in notes): break
            time.sleep(1)
        else: raise AssertionError('Combined native worker did not publish a ready note')
        hashes={str(n):stack.sha256(n) for n in notes}
        time.sleep(3)
        assert all(stack.sha256(Path(n))==h for n,h in hashes.items())
        print(docker('exec','-e',f"HTTP_PROXY=http://host.docker.internal:{cfg['llm_port']}",project+'-app','python','/network_probe.py'))
        counts=stack.request(f"http://127.0.0.1:{cfg['llm_port']}/counts")
        assert counts['llm']>=1 and counts['proxy']==0,counts
        api.jobs()
        api.events(next(iter(api.recordings()))['id'])
        result={'status':'PASS','emptyRootPrepare':True,'partialUpIntent':True,'freshProcessRecovery':True,'normalNativeInstall':True,
            'nativeWorkerReadyNote':True,'immutableNote':True,'actualSpeakrNegativeRequests':10,'proxyForwardedRequests':0,'syntheticServices':True,'GPUUsed':False,'liveChanged':False}
        (ROOT/'.validation').mkdir(exist_ok=True)
        stack.atomic_json(ROOT/'.validation/combined.json',result)
        print(json.dumps(result,indent=2))
    finally:
        stack.compose=original_compose
        if installed:
            native(ROOT/'scripts/install.ps1','-TargetRoot',target,'-Python',cfg['python'],'-Action','Rollback')
        original_compose(cfg,'down','--remove-orphans')
        docker('rm','-f',llm_name,check=False)
        print('Synthetic fixture evidence directory:',base)

if __name__=='__main__': main()
