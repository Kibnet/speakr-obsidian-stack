"""Actual pinned Speakr image + synthetic ASR/LLM -> bridge -> immutable note.

Creates only uniquely named containers/network/temporary data, uses no GPU.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import urllib.request
import wave

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'automation'))
from bridge import Bridge
from common import enqueue,atomic_json,sha256

def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0))
        return s.getsockname()[1]

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',default='speakr-obsidian-stack:73ba1f9')
    parser.add_argument('--docker',default='docker.exe')
    a=parser.parse_args()
    prefix='stacktest-'+uuid.uuid4().hex[:10]
    network=prefix+'-net'
    app=prefix+'-app'
    stub=prefix+'-stub'
    appport,stubport=port(),port()
    base=Path(tempfile.mkdtemp(prefix=prefix+'-')).resolve()
    for name in ('uploads','instance','bridge','vault','audio'): (base/name).mkdir()
    def docker(*args,check=True):
        p=subprocess.run([a.docker,*map(str,args)],capture_output=True,text=True,timeout=60)
        if check and p.returncode: raise RuntimeError('Docker fixture command failed: '+p.stderr[-1500:])
        return (p.stdout+p.stderr).strip() if args and args[0]=='logs' else p.stdout.strip()
    env={'ADMIN_EMAIL':'fixture@example.com','ADMIN_PASSWORD':'synthetic-only-password','ADMIN_USERNAME':'admin',
         'SECRET_KEY':uuid.uuid4().hex,'ALLOW_REGISTRATION':'false',
         'TEXT_MODEL_BASE_URL':f'http://host.docker.internal:{stubport}/v1','CHAT_MODEL_BASE_URL':f'http://host.docker.internal:{stubport}/v1',
         'TEXT_MODEL_NAME':'synthetic-model','CHAT_MODEL_NAME':'synthetic-model','TEXT_MODEL_API_KEY':'synthetic','CHAT_MODEL_API_KEY':'synthetic',
         'ASR_BASE_URL':'http://whisperx-asr:9000','USE_ASR_ENDPOINT':'true','ASR_DIARIZE':'true','ASR_RETURN_SPEAKER_EMBEDDINGS':'false',
         'JOB_QUEUE_WORKERS':'1','SUMMARY_QUEUE_WORKERS':'1','DISABLE_VOICE_EMBEDDING_CHECK':'true',
         'LLM_MAX_RETRIES':'0','LLM_REQUEST_TIMEOUT':'10','LLM_CONNECT_TIMEOUT':'2','ENABLE_STREAM_OPTIONS':'false'}
    envpath=base/'.env'
    envpath.write_text(''.join(f'{k}={v}\n' for k,v in env.items()),encoding='utf-8')
    cfg={'speakr_url':f'http://127.0.0.1:{appport}','env_file':str(envpath),'sources':[str(base/'audio')],
         'vault_root':str(base/'vault'),'vault':str(base/'vault/notes'),'scan_seconds':1,'stable_seconds':0,
         'completed_poll_seconds':0,'max_audio_bytes':990000000,'ffprobe':'ffprobe.exe','ffmpeg':'ffmpeg.exe',
         'auto_start_docker':False,'recovery_enabled':True,'llm_ready':True,'legacy_recovery_jobs':[]}
    bridge=None
    try:
        docker('network','create',network)
        docker('run','-d','--name',stub,'--network',network,'--network-alias','whisperx-asr',
               '-p',f'127.0.0.1:{stubport}:9000','-v',str(ROOT/'tests/stub_services.py')+':/fixture.py:ro',
               '--entrypoint','python',a.image,'-S','/fixture.py')
        docker('run','-d','--name',app,'--network',network,'--add-host','host.docker.internal:host-gateway',
               '-p',f'127.0.0.1:{appport}:8899','--env-file',envpath,
               '-v',str(base/'uploads')+':/data/uploads','-v',str(base/'instance')+':/data/instance',
               '-v',str(ROOT/'tests/app_network_probe.py')+':/network_probe.py:ro',a.image)
        limit=time.time()+180
        while time.time()<limit:
            state=docker('inspect',app,'--format','{{.State.Running}}')
            if state!='true': raise RuntimeError('App exited: '+docker('logs','--tail','35',app))
            try:
                urllib.request.urlopen(cfg['speakr_url']+'/login',timeout=3).read()
                break
            except Exception: time.sleep(2)
        else:
            raise RuntimeError('App startup failed: '+docker('logs','--tail','35',app))
        print(docker('exec',app,'python','/network_probe.py'))
        bridge=Bridge(cfg,base/'bridge')
        bridge.scan_sources()
        audio=base/'audio/synthetic.wav'
        with wave.open(str(audio),'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
            wav.writeframes(b'\0\0'*16000)
        enqueue([audio],base/'bridge')
        limit=time.time()+180
        while time.time()<limit:
            bridge.cycle()
            notes=list((base/'vault').rglob('*.md'))
            if any('Синтетическая проверка' in n.read_text(encoding='utf-8') and 'summary_status: "ready"' in n.read_text(encoding='utf-8') for n in notes):
                break
            time.sleep(2)
        else:
            print('Bridge state:',json.dumps(bridge.status(),ensure_ascii=False))
            print('App logs:',docker('logs','--tail','75',app))
            raise RuntimeError('Actual app pipeline did not publish a ready synthetic note')
        hashes={str(n):sha256(n) for n in notes}
        for _ in range(3): bridge.cycle()
        assert all(sha256(Path(n))==digest for n,digest in hashes.items()),'Immutable note overwritten'
        counts=json.load(urllib.request.urlopen(f'http://127.0.0.1:{stubport}/counts'))
        assert counts['asr']>=1 and counts['llm']>=1,counts
        bridge.api.jobs()
        row=next(iter(bridge.api.recordings()))
        bridge.api.events(row['id'])
        result={'status':'PASS','actualBuiltApp':True,'syntheticASR':counts['asr'],'syntheticLLM':counts['llm'],
                'readyNote':True,'immutableNote':True,'authJobsEvents':True,'externalNetworkProbe':'blocked','GPUUsed':False,'liveChanged':False}
        (ROOT/'.validation').mkdir(exist_ok=True)
        atomic_json(ROOT/'.validation/integration.json',result)
        print(json.dumps(result,indent=2))
    finally:
        if bridge: bridge.close()
        docker('rm','-f',app,stub,check=False)
        docker('network','rm',network,check=False)
        print('Synthetic local evidence directory:',base)

if __name__=='__main__': main()
