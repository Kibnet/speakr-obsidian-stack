"""Fail before app import when local endpoints are absent or wrong."""
import os
from urllib.parse import urlsplit

def validate(env):
    allowed = {'host.docker.internal', '127.0.0.1', 'localhost', '::1'}
    for key in ('TEXT_MODEL_BASE_URL', 'CHAT_MODEL_BASE_URL'):
        url = urlsplit(env.get(key, ''))
        if url.scheme != 'http' or url.hostname not in allowed or url.username or url.password or url.query or url.fragment or url.path.rstrip('/') != '/v1':
            raise ValueError('Explicit local ' + key + ' required')
    url = urlsplit(env.get('ASR_BASE_URL', ''))
    if url.scheme != 'http' or url.hostname not in {'whisperx-asr', '127.0.0.1', 'localhost'} or url.username or url.password:
        raise ValueError('Explicit local ASR_BASE_URL required')
    for key in ('TEXT_MODEL_NAME','CHAT_MODEL_NAME'):
        if not env.get(key, '').strip():
            raise ValueError(key + ' required')

if __name__ == '__main__':
    import sys
    validate(os.environ)
    os.execvp('docker-entrypoint.sh', ['docker-entrypoint.sh', *sys.argv[1:]])
