import functools
import http.server
import subprocess
import threading
import pytest
from evaluation_backend.git_scorers import GitExecution, prepare, execute
from evaluation_backend.executable_scorers import validate_execution
from evaluation_backend.scorer_catalog import ScorerConfig

CONFIG=dict(engine='git',repo_url='https://github.com/example/scorers.git',commit='a'*40,script_path='scorer.py')
def test_pinned_git_validation():
    assert validate_execution(CONFIG)['engine']=='git'
    for patch in ({'commit':'main'},{'script_path':'../scorer.py'},{'repo_url':'https://secret@github.com/a/b'}):
        with pytest.raises(ValueError): GitExecution.model_validate({**CONFIG,**patch})

@pytest.fixture
def source(tmp_path,monkeypatch):
    def git(*args): return subprocess.check_output(['git',*args]).decode().strip()
    repo=tmp_path/'work';repo.mkdir()
    git('init',str(repo))
    (repo/'scorer.py').write_text('import json,sys,os; v=json.load(sys.stdin); print(json.dumps({"score":v["score"],"reason":str(os.getenv("SECRET_PROBE"))}))')
    (repo/'slow.py').write_text('import time; time.sleep(10)')
    git('-C',str(repo),'add','.')
    git('-C',str(repo),'-c','user.name=Test','-c','user.email=test@example.com','commit','-m','fixture')
    commit=git('-C',str(repo),'rev-parse','HEAD')
    git('clone','--bare',str(repo),str(tmp_path/'scorer.git'))
    git('--git-dir='+str(tmp_path/'scorer.git'),'update-server-info')
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),functools.partial(http.server.SimpleHTTPRequestHandler,directory=str(tmp_path)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    monkeypatch.setenv('EVAL_GIT_FIXTURE_URL',f'http://127.0.0.1:{server.server_port}/scorer.git')
    yield {**CONFIG,'repo_url':'local-fixture://guardrail-scorer-git','commit':commit}
    server.shutdown();thread.join();server.server_close()

def test_fetch_cache_and_execute_without_inherited_credentials(source,monkeypatch):
    monkeypatch.setenv('SECRET_PROBE','must-not-leak')
    first=prepare(source)[1]
    assert prepare(source)[1]==first
    assert execute(source,{'score':.75})=={'score':.75,'reason':'None'}
    with pytest.raises(ValueError): prepare({**source,'script_path':'missing.py'})
    with pytest.raises(ValueError): prepare({**source,'commit':'f'*40})

def test_script_timeout(source):
    with pytest.raises(ValueError,match='timed out'): execute({**source,'script_path':'slow.py','timeout':1},{})

def test_git_type_and_source_consistency():
    base=dict(name='Git',kind='CODE',threshold=.8,execution=CONFIG,repoUrl=CONFIG['repo_url'],scriptPath='scorer.py')
    assert ScorerConfig(**base).execution['engine']=='git'
    with pytest.raises(ValueError): ScorerConfig(**{**base,'kind':'RULE'})
    with pytest.raises(ValueError): ScorerConfig(**{**base,'scriptPath':'other.py'})


def test_ssh_git_sources_and_no_api_private_key(monkeypatch):
    from evaluation_backend.git_scorers import verify
    monkeypatch.delenv('EVAL_GIT_SSH_KEY_FILE',raising=False)
    for repo in ('git@github.com:ianwoolf/evaluation_backend.git','ssh://git@github.com/ianwoolf/evaluation_backend.git'):
        config={**CONFIG,'repo_url':repo}
        assert verify(config)['checkout_validation']=='runner'
        with pytest.raises(ValueError): GitExecution.model_validate({**config,'script_path':'../private-key'})

def test_runner_ssh_secret_references(monkeypatch):
    from evaluation_backend.k8s_job import build_job
    monkeypatch.setenv('EVAL_GIT_SSH_SECRET_NAME','test-git-secret')
    monkeypatch.setenv('EVAL_GIT_SSH_SECRET_KEY','private-key')
    monkeypatch.setenv('EVAL_GIT_SSH_KNOWN_HOSTS_CONFIGMAP','test-hosts')
    job=build_job('ds','none',['git'])
    pod=job['spec']['template']['spec']
    volume=next(v for v in pod['volumes'] if v['name']=='git-ssh-key')
    assert volume['secret']['secretName']=='test-git-secret'
    assert volume['secret']['items']==[{'key':'private-key','path':'private-key'}]
    assert next(m for m in pod['containers'][0]['volumeMounts'] if m['name']=='git-ssh-key')['readOnly']
    monkeypatch.delenv('EVAL_GIT_SSH_SECRET_KEY')
    with pytest.raises(ValueError): build_job('ds','none',['git'])
