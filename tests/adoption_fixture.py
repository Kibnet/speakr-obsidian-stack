"""Private synthetic old-layout fixture; copies executable code, never personal data."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import shutil
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'automation'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import adoption
from bridge import Bridge

def create(base, legacy, port):
    root=base/'runtime'/'automation'
    root.mkdir(parents=True)
    for name in ('vault','audio','models'): (base/name).mkdir()
    (root.parent/'llm').mkdir()
    for f in legacy.iterdir():
        if f.is_file() and f.suffix in adoption.EXTENSIONS: shutil.copy2(f,root/f.name)
    prefix='adopt-'+base.name[-8:]
    cfg={'sources':[str(base/'audio')],'vault_root':str(base/'vault'),'vault':str(base/'vault'/'notes'),
         'docker':str(base/'docker.cmd'),'pythonw':str(Path(sys.executable).with_name('pythonw.exe')),
         'ffmpeg':shutil.which('ffmpeg'),'ffprobe':shutil.which('ffprobe'),
         'speakr_url':f'http://127.0.0.1:{port}','speakr_health_url':f'http://127.0.0.1:{port}/health',
         'asr_health_url':f'http://127.0.0.1:{port}/health','llm_url':f'http://127.0.0.1:{port}','llm_model':'fixture-model',
         'compose_file':str(root.parent/'compose.yaml'),'env_file':str(root.parent/'.env'),
         'bridge_task':prefix+' Bridge','watchdog_task':prefix+' Watchdog','ollama_task':prefix+' Ollama',
         'stable_seconds':1,'scan_seconds':1,'completed_poll_seconds':0,'auto_start_docker':False,
         'recovery_enabled':True,'supervision_candidate':False,
         'exact_two_speaker_roots':[str(base/'audio')],'acr_timestamp_roots':[str(base/'audio')]}
    # Seed schema using the tested current constructor, then run only the copied old package.
    b=Bridge(cfg,root)
    b.db.execute('UPDATE sources SET initialized=1')
    b.db.execute("INSERT INTO jobs(id,sha256,state,recording_id,stage) VALUES(?,?,?,?,?)",('a'*32,'a'*64,'failed',7,str(root/'staging'/'old.m4a')))
    b.db.execute("INSERT INTO jobs(id,sha256,state,recording_id,stage) VALUES(?,?,?,?,?)",('b'*32,'b'*64,'summary_pending',7,str(root/'staging'/'new.m4a')))
    b.db.execute("INSERT INTO recovery_jobs(job_id,transcript_status,summary_status) VALUES(?,?,?)",('b'*32,'completed','manual_required'))
    b.db.commit(); b.close()
    cfg['legacy_recovery_jobs']=['a'*32,'b'*32]
    containers={prefix+'-app':{'service':'app','image_id':'sha256:'+'a'*64,'image_ref':'fixture-image'},prefix+'-asr':{'service':'whisperx-asr','image_id':'sha256:'+'a'*64,'image_ref':'fixture-image'}}
    cfg['runtime_manifest']={'compose_sha256':'old','containers':containers}
    adoption.put(root/'config.json',cfg)
    adoption.put(root/'maintenance.json',{'paused':True})
    adoption.put(root/'backlog-approved.json',{'job_ids':['b'*32]})
    (root.parent/'compose.yaml').write_text('# fixture effective config comes from docker.exe\n')
    (root.parent/'.env').write_text('ADMIN_EMAIL=fixture@example.test\nADMIN_PASSWORD=synthetic-only\n')
    launcher=(legacy.parent/'llm'/'start-ollama.ps1').read_text(encoding='utf-8-sig')
    import re
    launcher=re.sub(r"\$ollamaExecutable\s*=\s*'[^']+'",lambda _:"$ollamaExecutable = '"+str(base/'ollama.exe')+"'",launcher)
    launcher=re.sub(r"\$env:OLLAMA_HOST\s*=\s*'[^']+'",lambda _:"$env:OLLAMA_HOST = '127.0.0.1:"+str(port)+"'",launcher)
    launcher=re.sub(r"\$env:OLLAMA_MODELS\s*=\s*'[^']+'",lambda _:"$env:OLLAMA_MODELS = '"+str(base/'models')+"'",launcher)
    launcher="[IO.File]::WriteAllText('"+str(base/'launcher-entered.txt')+"','entered')\n"+launcher
    (root.parent/'llm'/'start-ollama.ps1').write_text(launcher,encoding='utf-8-sig')
    compose={'name':prefix,'services':{x['service']:{'container_name':n,'image':'fixture-image','volumes':[],'ports':[]} for n,x in containers.items()}}
    observed=[{'Name':'/'+n,'Config':{'Labels':{'com.docker.compose.project':prefix,'com.docker.compose.service':x['service']}},'State':{'Running':True},'Image':x['image_id'],'Mounts':[],'NetworkSettings':{'Ports':{}}} for n,x in containers.items()]
    adoption.put(base/'compose-response.json',compose)
    adoption.put(base/'containers-response.json',observed)
    adoption.put(base/'baseline.json',adoption.baseline(root))
    return cfg

def check(base):
    root=base/'runtime'/'automation'
    if adoption.baseline(root)!=adoption.load(base/'baseline.json'): raise AssertionError('Fixture queue/note drift')
    print('Synthetic queue/notes/config preserved')

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('action',choices=('create','check')); p.add_argument('--base',required=True);p.add_argument('--legacy');p.add_argument('--port',type=int,default=19385);a=p.parse_args()
    if a.action=='create': print(json.dumps(create(Path(a.base),Path(a.legacy),a.port)))
    else: check(Path(a.base))
