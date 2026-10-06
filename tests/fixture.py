"""Generate synthetic native installer inputs; no original-machine dependencies."""
import json
from pathlib import Path
import shutil
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import stack

def main():
    base=Path(sys.argv[1]).resolve()
    base.mkdir(parents=True,exist_ok=False)
    for name in ('vault','audio','models'): (base/name).mkdir()
    (base/'audio/old.wav').write_bytes(b'baseline-only-not-real-audio')
    cfg=stack.load(stack.ROOT/'config/stack.example.json')
    cfg.update(project='fixture-'+base.name[-8:].lower(),target_root=str(base/'runtime'),vault_root=str(base/'vault'),vault=str(base/'vault/notes'),sources=[str(base/'audio')],ollama_models=str(base/'models'),managed_ollama=False)
    for key in ('python','pythonw','docker','ffmpeg','ffprobe'):
        cfg[key]=shutil.which(cfg[key])
    # Native control fixture uses real executables but deliberately inaccessible endpoints.
    cfg.update(app_port=18181,asr_port=18182,llm_port=18183,scan_seconds=1,completed_poll_seconds=0)
    stack.atomic_json(base/'input.json',cfg)
    (base/'secret.env').write_text('ADMIN_EMAIL=fixture@example.test\nADMIN_PASSWORD=synthetic-only-password\nHF_TOKEN=hf_synthetic\n')
    stack.prepare(cfg,base/'input.json',base/'secret.env')
    print(str(base/'input.json'))

if __name__=='__main__': main()
