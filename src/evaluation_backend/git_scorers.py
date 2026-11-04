"""Fetch pinned Git source once per runner process; execute JSON scripts locally."""
import os
import json
import subprocess
import tempfile
import sys
import atexit
import shutil
import re
import shlex
import threading
from pathlib import Path
from evaluation_backend.observability import event
from evaluation_backend.tracing import traced
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

class GitExecution(BaseModel):
    model_config = ConfigDict(extra='forbid')
    engine: Literal['git'] = 'git'
    repo_url: str = Field(min_length=1)
    commit: str = Field(pattern=r'^[0-9a-f]{40}$')
    script_path: str = Field(min_length=1)
    timeout: int = Field(default=30, ge=1, le=120)

    @model_validator(mode='after')
    def paths(self):
        from urllib.parse import urlsplit
        scp = bool(re.fullmatch(r'git@[A-Za-z0-9.-]+:[A-Za-z0-9_./-]+',self.repo_url))
        url = urlsplit(self.repo_url) if not scp else None
        if not scp and (url.scheme not in ('https', 'local-fixture','ssh') or not url.netloc or url.password or (url.username and not (url.scheme=='ssh' and url.username=='git'))):
            raise ValueError('Use HTTPS without credentials, ssh://git@host/repo or git@host:repo')
        if any(p in ('', '.', '..') for p in self.script_path.split('/')) or any(c in self.script_path for c in ('\\', ':', '\x00')):
            raise ValueError('Script must be a repository-relative path')
        return self

_cache = {}
_cache_lock = threading.RLock()
_roots = []

@atexit.register
def cleanup():
    for root in _roots:
        shutil.rmtree(root, ignore_errors=True)

def artifact(config):
    c = GitExecution.model_validate(config)
    repo = c.repo_url
    if repo == 'local-fixture://guardrail-scorer-git':
        repo = os.getenv('EVAL_GIT_FIXTURE_URL','http://evaluation-scorer-git:8080/scorer.git')
    return c, repo

def prepare(config):
    with _cache_lock:
        return _prepare(config)

@traced('git.checkout')
def _prepare(config):
    c, repo = artifact(config)
    key = (repo,c.commit,c.script_path)
    if key in _cache: return c,_cache[key]
    root = Path(tempfile.mkdtemp(prefix='evaluation-git-'))
    _roots.append(root)
    env = {'PATH':os.defpath,'HOME':str(root),'GIT_TERMINAL_PROMPT':'0','GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':os.devnull,'GIT_LFS_SKIP_SMUDGE':'1'}
    ssh = repo.startswith(('git@','ssh://'))
    if ssh:
        key_file=os.getenv('EVAL_GIT_SSH_KEY_FILE','')
        hosts_file=os.getenv('EVAL_GIT_SSH_KNOWN_HOSTS_FILE','')
        if not key_file or not hosts_file:
            raise ValueError('Runner Git SSH key/known_hosts mounts are not configured')
        private=root/'ssh-key';shutil.copyfile(key_file,private);private.chmod(0o600)
        env['GIT_SSH_COMMAND']=shlex.join(['ssh','-F','/dev/null','-i',str(private),'-o','IdentitiesOnly=yes','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+hosts_file,'-o','ConnectTimeout=15'])
    event('git.scorer.fetch.started' ,commit=c.commit,script_path=c.script_path)
    try:
        def git(*args):
            return subprocess.run(['git','-c','core.hooksPath=/dev/null','-C',str(root),*args],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=90,check=True).stdout.decode().strip()
        git('init','--quiet')
        git('remote','add','origin',repo)
        if c.repo_url.startswith('local-fixture:'): git('fetch','origin',c.commit)
        else: git('fetch','--depth=1','origin',c.commit)
        if git('rev-parse','FETCH_HEAD') != c.commit: raise ValueError('Git commit mismatch')
        git('checkout','--detach','--force',c.commit)
        if ssh: private.unlink()
        script = root/c.script_path
        if not script.is_file() or script.is_symlink() or not script.resolve().is_relative_to(root.resolve()):
            raise ValueError('Scorer script missing or outside repository')
        if script.suffix != '.py': raise ValueError('Scorer entry must be a Python script')
        _cache[key]=script
        event('git.scorer.fetch.completed',commit=c.commit,script_path=c.script_path)
        return c,script
    except Exception as error:
        event('git.scorer.fetch.failed',level='ERROR',commit=c.commit,error=error)
        raise ValueError('Cannot fetch pinned Git commit or locate scorer script') from None

def verify(config):
    c=GitExecution.model_validate(config)
    if c.repo_url.startswith(('git@','ssh://')) and not os.getenv('EVAL_GIT_SSH_KEY_FILE'):
        return {'valid':True,'commit':c.commit,'script_path':c.script_path,'checkout_validation':'runner','execution':'runner'}
    c,script = prepare(config)
    return {'valid':True,'commit':c.commit,'script_path':c.script_path,'execution':'runner'}

@traced('scorer.execute')
def execute(config,evidence):
    c,script=prepare(config)
    event('git.scorer.execute.started',commit=c.commit)
    # A subprocess in the runner, with no inherited API, Guardrail or collector credentials.
    # The runner container remains the isolation boundary; this is not a hostile-code sandbox.
    import signal
    with tempfile.TemporaryDirectory(prefix='evaluation-score-') as work:
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            process=subprocess.Popen([sys.executable,'-I',str(script)],cwd=work,env={'PATH':os.defpath,'HOME':work,'TMPDIR':work},stdin=subprocess.PIPE,stdout=out,stderr=err,start_new_session=True)
            try:
                process.communicate(json.dumps(evidence,allow_nan=False).encode(),timeout=c.timeout)
                if process.returncode: raise ValueError('Git scorer script failed')
                if out.tell()>1000000: raise ValueError('Git scorer result exceeds 1 MB')
                out.seek(0);result=json.load(out)
                if not isinstance(result,dict): raise ValueError('Git scorer must return a JSON object')
                event('git.scorer.execute.completed',commit=c.commit)
                return result
            except Exception as error:
                if process.poll() is None:
                    os.killpg(process.pid,signal.SIGKILL);process.wait()
                event('git.scorer.execute.failed',level='ERROR',commit=c.commit,error=error)
                raise ValueError('Git scorer execution failed or timed out') from None
