"""Read-only real API prerequisites before bridge installation."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'automation'))
from common import config
from health import probe_http,probe_llm
from speakr_api import SpeakrAPI
cfg=config(Path(sys.argv[1])/'config.json')
for name,result in [('app',probe_http(cfg['speakr_health_url'])),('ASR',probe_http(cfg['asr_health_url'])),('Ollama model',probe_llm(cfg))]:
    if not result['ready']: raise ValueError(name+' unavailable; wait for model loading/check status and local endpoints')
api=SpeakrAPI(cfg)
api.login()
api.jobs()
print('Configured app auth/jobs, ASR and model readiness verified')
