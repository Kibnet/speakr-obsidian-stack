import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import subprocess
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import stack
import build

def fixture(base):
    cfg=json.loads((ROOT/'config/stack.example.json').read_text())
    cfg.update(project='fixture-stack',target_root=str(base/'runtime'),vault_root=str(base/'vault'),vault=str(base/'vault'/'notes'),sources=[str(base/'audio')],ollama_models=str(base/'models'))
    (base/'vault').mkdir(exist_ok=True)
    (base/'audio').mkdir(exist_ok=True)
    return cfg

class StackTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='стек с пробелами ')
        self.base=Path(self.temp.name)
        self.cfg=fixture(self.base)
    def tearDown(self): self.temp.cleanup()

    def test_root_overlap_rejected_even_when_empty(self):
        for path in (ROOT/'new-runtime',self.base,self.base/'vault'/'runtime',self.base/'audio'/'runtime'):
            cfg=dict(self.cfg,target_root=str(path))
            with self.assertRaises(ValueError): stack.validate(cfg,binaries=False)
    def test_external_config_is_protected(self):
        with self.assertRaises(ValueError):
            stack.validate(self.cfg,Path(self.cfg['target_root'])/'input.json',binaries=False)
    def test_symlink_overlap_rejected(self):
        alias=self.base/'alias'
        try: alias.symlink_to(self.base/'vault',target_is_directory=True)
        except OSError: self.skipTest('No symlink privilege; native fixture covers junction')
        with self.assertRaises(ValueError): stack.validate(dict(self.cfg,target_root=str(alias/'runtime')),binaries=False)
    def test_custom_paths_and_names_preserved(self):
        cfg=stack.validate(self.cfg,binaries=False)
        self.assertEqual(cfg['target_root'],self.cfg['target_root'])
        self.assertEqual(cfg['project'],'fixture-stack')
    def test_port_collision_and_invalid_project_rejected(self):
        for change in ({'llm_port':self.cfg['app_port']},{'app_port':0},{'project':'Bad Name'},{'llm_model':'bad\nvalue'}):
            with self.assertRaises(ValueError): stack.validate(dict(self.cfg,**change),binaries=False)
    def test_prepare_owned_layout_avoids_empty_root_conflict(self):
        secret=self.base/'secrets.env'
        secret.write_text('ADMIN_EMAIL=fixture@example.test\nADMIN_PASSWORD=synthetic-only-password\nHF_TOKEN=hf_synthetic\n')
        with patch.object(stack,'executable',side_effect=lambda x:x),patch.object(Path,'is_file',return_value=True):
            stack.prepare(self.cfg,self.base/'input.json',secret)
        m=stack.verify_owned(self.cfg)
        self.assertEqual(m['phase'],'installed')
        self.assertTrue((self.base/'runtime/stack/compose.yaml').is_file())
        bridge=stack.load(self.base/'runtime/automation/config.json')
        self.assertEqual(bridge['bridge_task'],'fixture-stack Bridge')
        self.assertEqual(bridge['llm_model'],self.cfg['llm_model'])
        self.assertEqual(bridge['sources'],self.cfg['sources'])
        env=stack.env_file(self.base/'runtime/stack/.env')
        self.assertEqual(env['ADMIN_EMAIL'],'fixture@example.test')
        self.assertEqual(env['TEXT_MODEL_BASE_URL'],'http://host.docker.internal:11435/v1')
    def test_credential_examples_refused(self):
        with self.assertRaises(ValueError): stack.check_credentials(stack.env_file(ROOT/'config/secrets.example.env'))
    def test_owned_file_drift_rejected(self):
        runtime=self.base/'runtime'
        runtime.mkdir()
        p=runtime/'test.txt'
        p.write_text('old')
        stack.atomic_json(runtime/'stack-install.json',{'project':self.cfg['project'],'target_root':str(runtime),'files':{'test.txt':stack.sha256(p)}})
        p.write_text('new')
        with self.assertRaises(ValueError): stack.verify_owned(self.cfg)
    def test_model_mismatch_is_not_accepted(self):
        deps=stack.load(ROOT/'config/dependencies.json')
        with self.assertRaises(ValueError): stack.verify_model(self.cfg,deps,{'models':[{'name':deps['model']['name'],'digest':'sha256:wrong'}]})
        stack.verify_model(self.cfg,deps,{'models':[dict(deps['model'],digest=deps['model']['digest'].removeprefix('sha256:'))]})
    def test_build_replaces_ephemeral_ffmpeg_and_enforces_local_policy(self):
        deps=stack.load(ROOT/'config/dependencies.json')
        old='FROM python:3.11-slim AS ffmpeg-stage\nARG BTBN_TAG=old\n###############################################################################\nENV PYTHONPATH=/app\nENTRYPOINT ["docker-entrypoint.sh"]'
        result=build.dockerfile(old,deps)
        self.assertNotIn('BTBN_TAG',result)
        self.assertIn(deps['ffmpeg']['sha256'],result)
        self.assertIn('sha256sum -c',result)
        self.assertIn('/opt/local-policy',result)
    def test_local_endpoint_policy_negative_cases(self):
        spec=importlib.util.spec_from_file_location('local_entrypoint',ROOT/'docker/local_entrypoint.py')
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        valid={'TEXT_MODEL_BASE_URL':'http://host.docker.internal:11435/v1','CHAT_MODEL_BASE_URL':'http://host.docker.internal:11435/v1','ASR_BASE_URL':'http://whisperx-asr:9000','TEXT_MODEL_NAME':'fixture','CHAT_MODEL_NAME':'fixture'}
        module.validate(valid)
        for key in ('TEXT_MODEL_BASE_URL','CHAT_MODEL_BASE_URL'):
            for value in ('','https://api.openai.com/v1','http://example.com/v1','http://localhost/v1#bad'):
                with self.assertRaises(ValueError): module.validate(dict(valid,**{key:value}))
    def test_env_dollar_is_literal(self):
        p=self.base/'env'
        stack.write_env(p,{'ADMIN_PASSWORD':'dollar$one strong password'})
        self.assertEqual(stack.env_file(p)['ADMIN_PASSWORD'],'dollar$one strong password')
    def test_fresh_model_alias_created_after_verified_pull(self):
        deps=stack.load(ROOT/'config/dependencies.json')
        self.cfg['ollama']='ollama.exe'
        (self.base/'runtime').mkdir()
        base={'name':deps['model']['name'],'digest':stack.digest(deps['model']['digest'])}
        alias={'name':self.cfg['llm_model'],'digest':'a'*64}
        parameters='\n'.join(line.removeprefix('PARAMETER ') for line in (ROOT/'models/Modelfile').read_text().splitlines() if line.startswith('PARAMETER '))
        responses=[{'models':[]},{'status':'success'},{'models':[base]},{'models':[base,alias]},{'parameters':parameters}]
        with patch.object(stack,'verify_owned'),patch.object(stack,'request',side_effect=responses),patch.object(stack,'run') as command:
            stack.model(self.cfg)
        self.assertEqual(command.call_args.args[0][1],'create')
        state=stack.load(self.base/'runtime/model-state.json')
        self.assertEqual(state['alias_digest'],'a'*64)
    def test_alias_cannot_overwrite_base(self):
        self.cfg['llm_model']='qwen3.5:9b'
        with patch.object(stack,'verify_owned'),patch.object(stack,'request') as request:
            with self.assertRaises(ValueError): stack.model(self.cfg)
            request.assert_not_called()
    def test_inherited_compose_variables_cannot_override_owned_inputs(self):
        directory=self.base/'runtime/stack'
        directory.mkdir(parents=True)
        stack.write_env(directory/'.env',{'STACK_PROJECT':'fixture-stack','APP_IMAGE':'correct:image','ASR_IMAGE':'correct:asr'})
        with patch.dict(os.environ,{'APP_IMAGE':'evil:image','STACK_PROJECT':'other','COMPOSE_FILE':'evil.yaml'}),patch.object(stack,'run') as command:
            stack.compose(self.cfg,'config')
        env=command.call_args.kwargs['env']
        self.assertEqual(env['APP_IMAGE'],'correct:image')
        self.assertEqual(env['STACK_PROJECT'],'fixture-stack')
        self.assertNotIn('COMPOSE_FILE',env)
    @unittest.skipUnless(shutil.which('git'),'Git unavailable: source export test runs on host')
    def test_archive_excludes_dirty_and_private_cache_inputs(self):
        repo=self.base/'source'
        repo.mkdir()
        subprocess.run(['git','init',str(repo)],check=True,capture_output=True)
        (repo/'code.txt').write_text('committed')
        subprocess.run(['git','-C',str(repo),'add','code.txt'],check=True,capture_output=True)
        subprocess.run(['git','-C',str(repo),'-c','user.name=Fixture','-c','user.email=fixture@example.test','commit','-m','test: fixture'],check=True,capture_output=True)
        (repo/'code.txt').write_text('uncommitted change')
        (repo/'private.env').write_text('not for build')
        context=self.base/'context'
        build.archive_tree(repo,'HEAD',context)
        self.assertEqual((context/'code.txt').read_text(),'committed')
        self.assertFalse((context/'private.env').exists())
    def test_alias_parameter_mismatch_refused(self):
        with self.assertRaises(ValueError): stack.verify_parameters({'parameters':'num_ctx 1'})
    def test_incomplete_prepare_is_not_owned(self):
        runtime=self.base/'runtime'
        runtime.mkdir()
        stack.atomic_json(runtime/'stack-install.json',{'project':self.cfg['project'],'target_root':str(runtime),'phase':'prepared','files':{}})
        with self.assertRaises(ValueError): stack.verify_owned(self.cfg)
        m=stack.load(runtime/'stack-install.json');m['phase']='installed'
        stack.atomic_json(runtime/'stack-install.json',m)
        with self.assertRaises(ValueError): stack.verify_owned(self.cfg)
    def test_model_latest_normalization_and_explicit_tag(self):
        self.assertEqual(stack.model_key('my-model'),'my-model:latest')
        self.assertEqual(stack.model_key('localhost:11435/my-model'),'localhost:11435/my-model:latest')
        self.assertEqual(stack.model_key('my-model:v1'),'my-model:v1')
        self.cfg['llm_model']='my-model'
        with self.assertRaises(ValueError): stack.validate(self.cfg,binaries=False)
        self.cfg['llm_model']='my-model:latest'
        stack.validate(self.cfg,binaries=False)
        import health
        with patch.object(health,'probe_http',return_value={'ready':True,'data':{'models':[{'name':'my-model'}]}}):
            self.assertTrue(health.probe_llm({'llm_model':'my-model:latest','llm_url':'http://localhost:11435'})['ready'])
    def test_finish_preserves_installed_config_and_probe_failure_is_incomplete(self):
        runtime=self.base/'runtime'
        bridge=runtime/'automation';bridge.mkdir(parents=True)
        manifest={'phase':'installed','files':{},'up_intent':{'containers':{}}}
        identity={'containers':{'fixture':{'image_id':'immutable'}}}
        cfgpath=bridge/'config.json'
        cfgpath.write_text(json.dumps({'runtime_manifest':identity},indent=4),encoding='utf-8-sig')
        (bridge/'install-state.json').write_text('{}')
        original=cfgpath.read_bytes()
        with patch.object(stack,'verify_owned',return_value=manifest),patch.object(stack,'identities',return_value=identity),patch.object(stack,'run',side_effect=RuntimeError('Probe unavailable')):
            with self.assertRaises(RuntimeError): stack.finish_up(self.cfg)
        self.assertEqual(stack.load(stack.manifest_path(self.cfg))['up_phase'],'starting')
        self.assertEqual(cfgpath.read_bytes(),original)
        with patch.object(stack,'verify_owned',return_value=manifest),patch.object(stack,'identities',return_value=identity),patch.object(stack,'run'):
            stack.finish_up(self.cfg)
        self.assertEqual(stack.load(stack.manifest_path(self.cfg))['up_phase'],'ready')
        self.assertEqual(cfgpath.read_bytes(),original)
    def test_service_dns_recovery_and_proxy_cleanup(self):
        script='''
import importlib.util,socket,os
socket.getaddrinfo=lambda *a,**k: (_ for _ in ()).throw(OSError('not ready'))
spec=importlib.util.spec_from_file_location('policy',r"POLICY")
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
assert 'HTTP_PROXY' not in os.environ and os.environ['NO_PROXY']=='*'
def answer(ip): return [(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,9000))]
m._resolve=lambda *a,**k: answer('10.0.0.2')
m.local_resolve('whisperx-asr',9000); m._allowed('10.0.0.2')
m._resolve=lambda *a,**k: answer('10.0.0.3')
m.local_resolve('whisperx-asr',9000); m._allowed('10.0.0.3')
try: m._allowed('10.0.0.2')
except PermissionError: pass
else: raise AssertionError('Stale service IP retained')
'''.replace('POLICY',str(ROOT/'docker/sitecustomize.py'))
        subprocess.run([sys.executable,'-c',script],env=dict(os.environ,HTTP_PROXY='http://localhost:1'),check=True,capture_output=True)

if __name__=='__main__': unittest.main()
