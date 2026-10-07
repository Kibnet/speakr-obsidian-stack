"""One bounded supervision pass. Never infer permission to stop a busy GPU job."""
import argparse
import json
from pathlib import Path
import subprocess
import time
import os

from bridge import WorkerLock
from common import CREATE_NO_WINDOW, ROOT, atomic_json, config, maintenance_paused, powershell_env
from health import probe_http, probe_llm


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return default


class WindowsRuntime:
    def __init__(self, cfg, root):
        self.cfg, self.root = cfg, Path(root)

    def command(self, action, **params):
        args = ['powershell.exe', '-NoProfile', '-NonInteractive', '-File',
                str(self.root / 'runtime-control.ps1'), '-Action', action, '-Root', str(self.root),
                '-BridgeTask', self.cfg['bridge_task'], '-OllamaTask', self.cfg['ollama_task']]
        for key, value in params.items():
            args += ['-' + key, str(value)]
        p = subprocess.run(args, capture_output=True, timeout=20 if action == 'Inspect' else 10,
                           creationflags=CREATE_NO_WINDOW, check=True, env=powershell_env(), stdin=subprocess.DEVNULL)
        return json.loads(p.stdout.decode('utf-8-sig')) if p.stdout.strip() else {}

    def observe(self):
        runtime = self.command('Inspect')
        runtime['speakr'] = probe_http(self.cfg['speakr_health_url'])
        runtime['asr'] = probe_http(self.cfg['asr_health_url'])
        runtime['ollama'] = probe_llm(self.cfg)
        try:
            p = subprocess.run([self.cfg['docker'], 'info', '--format', '{{.ServerVersion}}'],
                               capture_output=True, timeout=3, creationflags=CREATE_NO_WINDOW)
            runtime['docker_ready'] = p.returncode == 0
        except subprocess.TimeoutExpired:
            runtime['docker_ready'] = None
        except OSError:
            runtime['docker_ready'] = None
        runtime['docker_process_present'] = self.docker_process_present() if runtime['docker_ready'] is not True else None
        return runtime

    def docker_process_present(self):
        # A running frontend/backend must never be relaunched for a slow engine.
        environment = {k: v for k, v in os.environ.items() if k.lower() != 'psmodulepath'}
        try:
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                "$ErrorActionPreference='Stop'; try { @(Get-Process -ErrorAction Stop | Where-Object { $_.ProcessName -in @('Docker Desktop','com.docker.backend') }).Count -gt 0 } catch { [Console]::Error.WriteLine('Process query failed'); exit 1 }"],
                capture_output=True, timeout=10, creationflags=CREATE_NO_WINDOW, env=environment, stdin=subprocess.DEVNULL)
            value = result.stdout.decode('utf-8-sig').strip().lower()
            if result.returncode == 0 and not result.stderr and value in ('true', 'false'):
                return value == 'true'
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            pass
        return None  # Unknown process state never authorizes a GUI launch.

    def action(self, component, verb, heartbeat=None):
        if self.cfg.get('supervision_candidate') and component in ('docker', 'containers'):
            raise RuntimeError('Candidate cannot control Docker or live containers')
        if component == 'docker':
            if self.docker_process_present() is not False:
                raise RuntimeError('Docker Desktop/backend already running or presence unknown; launch refused')
            subprocess.Popen([self.cfg['docker_desktop']], creationflags=CREATE_NO_WINDOW,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif component == 'containers':
            import hashlib
            manifest = self.cfg.get('runtime_manifest', {})
            if hashlib.sha256(Path(self.cfg['compose_file']).read_bytes()).hexdigest() != manifest.get('compose_sha256'):
                raise RuntimeError('Compose drift; containers not changed')
            for name, expected in manifest.get('containers', {}).items():
                p = subprocess.run([self.cfg['docker'], 'inspect', '--format', '{{.Image}} {{.State.Running}} {{index .Config.Labels "com.docker.compose.project"}} {{index .Config.Labels "com.docker.compose.service"}}', name],
                                   capture_output=True, timeout=3, creationflags=CREATE_NO_WINDOW)
                values = p.stdout.decode().strip().split()
                if p.returncode == 0:
                    if len(values)!=4 or values[0] != expected['image_id'] or values[2]!=expected.get('project') or values[3]!=expected['service']:
                        raise RuntimeError('Container image/ownership drift')
                    if values[1] == 'true':
                        continue
                    args = [self.cfg['docker'], 'start', name]
                else:
                    image = subprocess.run([self.cfg['docker'], 'image', 'inspect', '--format', '{{.Id}}', expected['image_ref']],
                                           capture_output=True, timeout=3, creationflags=CREATE_NO_WINDOW, check=True)
                    if image.stdout.decode().strip() != expected['image_id']:
                        raise RuntimeError('Local image drift; missing container not recreated')
                    env_file=Path(self.cfg['compose_file']).parent/'.env'
                    if not env_file.is_file() or hashlib.sha256(env_file.read_bytes()).hexdigest()!=manifest.get('env_sha256'):
                        raise RuntimeError('Compose env drift; missing container not recreated')
                    environment=dict(os.environ)
                    for line in env_file.read_text(encoding='utf-8-sig').splitlines():
                        if not line or line.startswith('#'): continue
                        key,value=line.split('=',1)
                        environment[key]=value[1:-1] if len(value)>=2 and value[0]==value[-1]=="'" else value
                    for key in list(environment):
                        if key.startswith('COMPOSE_'): environment.pop(key,None)
                    args = [self.cfg['docker'], 'compose', '--env-file', str(env_file), '-f', self.cfg['compose_file'], 'up', '-d', '--no-recreate',
                            '--no-deps', '--no-build', '--pull', 'never', expected['service']]
                subprocess.Popen(args, env=environment if p.returncode!=0 else None, creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return  # Only the stopped/missing service; never touch the running peer.
            raise RuntimeError('Container manifest unavailable or differs')
        else:
            self.command(verb, Component=component,
                         **({'ProcessId': heartbeat['pid'], 'StartMarker': heartbeat['start_marker']} if heartbeat else {}))

    def notify(self, recovery=False):
        subprocess.Popen(['powershell.exe', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
                          '-File', str(self.root / 'notify.ps1'), '-Event', 'Recovered' if recovery else 'Outage'],
                         creationflags=CREATE_NO_WINDOW, env=powershell_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Watchdog:
    def __init__(self, cfg, root=ROOT, runtime=None, clock=time.time):
        self.cfg, self.root, self.clock = cfg, Path(root), clock
        self.runtime = runtime or WindowsRuntime(cfg, root)
        self.state = read_json(self.root / 'watchdog-state.json', {})
        self.state.setdefault('started_at', clock())
        self.state.setdefault('components', {})

    def allowed(self, name):
        now = self.clock()
        c = self.state['components'].setdefault(name, {'actions': [], 'failures': 0})
        c['actions'] = [t for t in c['actions'] if now - t < 900]
        return len(c['actions']) < 3 and (not c['actions'] or now - c['actions'][-1] >= 120)

    def act(self, name, verb='Start', heartbeat=None):
        if getattr(self, 'acted_this_tick', False) or maintenance_paused(self.root) or not self.allowed(name):
            return False
        self.acted_this_tick = True
        # Persist the cooldown before spawning: a crash cannot produce an action storm.
        self.state['components'][name]['actions'].append(self.clock())
        atomic_json(self.root / 'watchdog-state.json', self.state)
        try:
            self.runtime.action(name, verb, heartbeat)
            return True
        except Exception as exc:
            self.state['components'][name]['error'] = type(exc).__name__
            return False

    def tick(self):
        now = self.clock()
        self.acted_this_tick = False
        if maintenance_paused(self.root):
            result = {'updated': now, 'maintenance': True, 'recovery_action': [], 'attention_required': False}
            atomic_json(self.root / 'watchdog-status.json', result)
            return result
        observed = self.runtime.observe()
        heartbeat = read_json(self.root / 'heartbeat.json', {})
        actions, problems = [], []
        for component in ('speakr', 'asr', 'ollama'):
            c = self.state['components'].setdefault(component, {'actions': [], 'failures': 0})
            c['failures'] = 0 if observed[component]['ready'] else c['failures'] + 1
            if not observed[component]['ready']:
                problems.append(component)
        bridge = observed.get('bridge', {})
        ollama = observed.get('ollama_task', {})
        alive = bridge.get('process_matches', False)
        fresh = heartbeat and now - heartbeat.get('updated', 0) < 45
        if not alive or not fresh:
            problems.append('bridge')
        if fresh and heartbeat.get('deadline') and now > heartbeat['deadline'] + 60:
            problems.append('bridge_operation_overdue')
        if observed['docker_ready'] is not True:
            problems.append('docker')
            if observed['docker_ready'] is False and observed.get('docker_process_present') is False and self.act('docker'):
                actions.append('docker:start')
        elif observed.get('containers_absent', False):
            if self.act('containers'):
                actions.append('containers:start')
        # Running tasks with a dead endpoint are degraded, not safe to kill blindly.
        if not observed['ollama']['ready'] and ollama.get('enabled') and ollama.get('state') == 'Ready':
            if self.act('ollama'):
                actions.append('ollama:start')
        if bridge.get('enabled') and bridge.get('state') == 'Ready' and not alive:
            if self.act('bridge'):
                actions.append('bridge:start')
        if alive and not fresh and heartbeat.get('deadline') and now > heartbeat['deadline'] + 60:
            requested = self.state.get('bridge_stop_requested')
            if not requested:
                if not maintenance_paused(self.root):
                    atomic_json(self.root / 'stop.json', {'at': now})
                    self.state['bridge_stop_requested'] = now
                    actions.append('bridge:graceful-stop')
            elif now - requested >= 120 and bridge.get('enabled'):
                if self.act('bridge', 'StopVerified', heartbeat):
                    actions.append('bridge:stop-verified')
        if fresh:
            self.state.pop('bridge_stop_requested', None)
        if not problems:
            if self.state.get('notified'):
                self.runtime.notify(recovery=True)
            self.state.pop('outage_since', None)
            self.state['notified'] = False
        else:
            self.state.setdefault('outage_since', now)
            if now - self.state['outage_since'] >= 600 and not self.state.get('notified'):
                self.runtime.notify()
                self.state['notified'] = True
        result = {'updated': now, 'maintenance': False, 'health': observed,
                  'recovery_action': actions, 'attention_required': bool(problems),
                  'problems': problems, 'notification_emitted': bool(self.state.get('notified'))}
        atomic_json(self.root / 'watchdog-state.json', self.state)
        atomic_json(self.root / 'watchdog-status.json', result)
        return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--config')
    args = p.parse_args()
    root = Path(args.config).resolve().parent if args.config else ROOT
    (root / 'watchdog').mkdir(exist_ok=True)
    try:
        with WorkerLock(root / 'watchdog'):
            Watchdog(config(args.config), root).tick()
    except RuntimeError as exc:
        if str(exc) != 'Bridge is already running':
            raise
    except Exception as exc:
        atomic_json(root / 'watchdog-status.json', {'updated': time.time(), 'attention_required': True,
                                                  'error': type(exc).__name__, 'recovery_action': []})
        raise


if __name__ == '__main__':
    main()
