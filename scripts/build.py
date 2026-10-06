"""Explicit pinned-source build; never uses an installed image as its input."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import urllib.request
import io
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]

def policy_digest():
    return hashlib.sha256(b''.join((ROOT/'docker'/name).read_bytes() for name in ('local_entrypoint.py','sitecustomize.py'))).hexdigest()

def command(args, **kwargs):
    subprocess.run([str(x) for x in args], check=True, **kwargs)

def archive_tree(repository,revision,context):
    archive=subprocess.check_output(['git','-C',str(repository),'archive','--format=zip',revision])
    context.mkdir()
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        for item in z.infolist():
            dest=(context/item.filename).resolve()
            if context.resolve() not in dest.parents:
                raise ValueError('Archive path escapes context')
        z.extractall(context)

def dockerfile(original, deps):
    start = original.index('FROM python:3.11-slim AS ffmpeg-stage')
    end = original.index('###############################################################################', start)
    ff = deps['ffmpeg']
    stage = f'''FROM python:3.11-slim AS ffmpeg-stage
# Durable official release source, checksum pinned in config/dependencies.json.
# Static native decoders + libmp3lame: audio compression works without runtime shared libs.
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev make pkg-config libmp3lame-dev wget xz-utils \\
    && wget -q {ff['url']} -O /tmp/ff.tar.xz \\
    && echo '{ff['sha256']}  /tmp/ff.tar.xz' | sha256sum -c - \\
    && mkdir /tmp/ff-src && tar xf /tmp/ff.tar.xz -C /tmp/ff-src --strip-components=1 \\
    && cd /tmp/ff-src \\
    && ./configure --disable-autodetect --disable-x86asm --disable-doc --disable-debug --disable-shared --enable-static --enable-gpl --enable-libmp3lame --pkg-config-flags=--static --extra-ldflags=-static \\
    && make -j2 ffmpeg ffprobe \\
    && install ffmpeg ffprobe /usr/local/bin/ \\
    && ffmpeg -version && ffprobe -version

'''
    result = original[:start] + stage + original[end:]
    result = result.replace('COPY requirements.txt requirements-embeddings.txt constraints.txt ./','COPY requirements.txt requirements-embeddings.txt constraints.txt stack-constraints.txt ./')
    result = result.replace('-c constraints.txt -r','-c constraints.txt -c stack-constraints.txt -r')
    result = result.replace('ENV PYTHONPATH=/app', 'ENV PYTHONPATH=/opt/local-policy:/app')
    result = result.replace('ENTRYPOINT ["docker-entrypoint.sh"]',
        'COPY local-policy /opt/local-policy\nENTRYPOINT ["python", "/opt/local-policy/local_entrypoint.py"]')
    if 'python_image' in deps:
        result = result.replace('FROM python:3.11-slim', 'FROM ' + deps['python_image'])
    return result

def build(docker, image, cold=False):
    deps = json.loads((ROOT/'config/dependencies.json').read_text())
    work = ROOT/'.work/source'
    if not work.exists():
        command(['git','clone','--no-checkout',deps['speakr']['repository'],work])
    actual = subprocess.check_output(['git','-C',str(work),'rev-parse',deps['speakr']['commit']], text=True).strip()
    if actual != deps['speakr']['commit']:
        raise ValueError('Source identity mismatch')
    original = subprocess.check_output(['git','-C',str(work),'show',actual+':Dockerfile'],text=True)
    # Export the exact commit tree, excluding modified/untracked source/cache files.
    context=ROOT/'.work'/('build-context-'+uuid.uuid4().hex)
    archive_tree(work,actual,context)
    (context/'Dockerfile.stack').write_text(dockerfile(original,deps),encoding='utf-8')
    shutil.copy2(ROOT/'docker/python-constraints.txt',context/'stack-constraints.txt')
    (context/'local-policy').mkdir(exist_ok=True)
    for name in ('local_entrypoint.py','sitecustomize.py'):
        shutil.copy2(ROOT/'docker'/name,context/'local-policy'/name)
    args=[docker,'build','--progress=plain','--build-arg','LIGHTWEIGHT=1',
          '--label','org.opencontainers.image.revision='+actual,
          '--label','org.speakr-obsidian-stack.source='+actual,
          '--label','org.speakr-obsidian-stack.policy-sha256='+policy_digest(),
          '-f',context/'Dockerfile.stack','-t',image]
    if cold: args += ['--no-cache','--pull']
    command(args+[context])
    command([docker,'run','--rm','--entrypoint','ffmpeg',image,'-version'])
    command([docker,'run','--rm','--entrypoint','ffprobe',image,'-version'])

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--docker',default='docker.exe')
    parser.add_argument('--image',default='speakr-obsidian-stack:73ba1f9')
    parser.add_argument('--cold',action='store_true')
    args=parser.parse_args()
    build(args.docker,args.image,args.cold)
