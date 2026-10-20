"""Trusted internal workload runner, using docker or kubectl without a shell."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

OWNER = 'evaluation-backend'
LABEL = 'evaluation.backend/owner'
FINGERPRINT = 'evaluation.backend/spec'
TERMINAL = {'SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT'}


class RunnerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    mode: str = 'docker'
    namespace: str = 'default'
    context: str = ''
    docker_context: str = ''
    network: str = 'none'
    state_db: str = 'data/runner.db'
    cli_timeout: int = 60

    def __post_init__(self):
        if self.mode not in {'docker', 'k8s'}:
            raise ValueError('mode must be docker or k8s')
        if self.cli_timeout < 1:
            raise ValueError('cli_timeout must be positive')
        if len(self.namespace) > 63 or not re.fullmatch(r'[a-z0-9]([-a-z0-9]*[a-z0-9])?', self.namespace):
            raise ValueError('invalid Kubernetes namespace')

    @classmethod
    def from_env(cls):
        return cls(mode=os.getenv('EVAL_RUNNER_MODE', 'docker'),
                   namespace=os.getenv('EVAL_K8S_NAMESPACE', 'default'),
                   context=os.getenv('EVAL_K8S_CONTEXT', ''),
                   docker_context=os.getenv('EVAL_DOCKER_CONTEXT', ''),
                   network=os.getenv('EVAL_DOCKER_NETWORK', 'none'),
                   state_db=os.getenv('EVAL_RUNNER_STATE_DB', 'data/runner.db'),
                   cli_timeout=int(os.getenv('EVAL_RUNNER_CLI_TIMEOUT', '60')))


@dataclass(frozen=True)
class Task:
    id: str
    image: str
    command: list[str]
    manifest: dict = field(default_factory=dict)
    cpu: float = 1
    memory_mb: int = 512
    timeout_seconds: int = 600

    def __post_init__(self):
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', self.id):
            raise ValueError('id must be 1-48 lowercase letters, digits or hyphens')
        if not isinstance(self.image, str) or not self.image or self.image.startswith('-'):
            raise ValueError('image is required')
        if not isinstance(self.command, list) or not self.command or not all(isinstance(x, str) and x for x in self.command):
            raise ValueError('command must be a nonempty string list')
        if not (0 < self.cpu <= 64 and 16 <= self.memory_mb <= 262144 and 1 <= self.timeout_seconds <= 604800):
            raise ValueError('invalid CPU, memory or timeout limit')
        if len(json.dumps(self.manifest, allow_nan=False).encode()) > 16384:
            raise ValueError('manifest exceeds 16KiB; use artifact references')

    @property
    def name(self):
        return 'eval-' + self.id

    @property
    def digest(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, allow_nan=False).encode()).hexdigest()[:63]


@dataclass(frozen=True)
class Status:
    id: str
    state: str
    detail: str = ''
    exit_code: int | None = None


def execute(args: list[str], stdin: str | None = None, timeout: int = 60) -> str:
    try:
        result = subprocess.run(args, input=stdin, text=True, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RunnerError(f'{args[0]} invocation failed ({type(exc).__name__})') from exc
    if result.returncode:
        # Do not leak environment values/manifest credentials in command or stderr.
        raise RunnerError(f'{args[0]} exited {result.returncode}; inspect runtime access and resource state')
    return result.stdout + (result.stderr if 'logs' in args else '')


class Runner:
    """State DB serializes lifecycle operations and preserves cancellation intent.

    It must live on durable storage. This is an internal CLI/library, not an
    unauthenticated API. Call status periodically to enforce Docker deadlines.
    """
    def __init__(self, config: Config, call: Callable = execute):
        self.config, self.call = config, call
        Path(config.state_db).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, scope TEXT NOT NULL, spec TEXT NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL DEFAULT "", exit_code INTEGER)')

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.config.state_db, timeout=self.config.cli_timeout + 10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @property
    def scope(self):
        c = self.config
        return json.dumps([c.mode, c.context, c.namespace, c.docker_context, c.network], separators=(',', ':'))

    def fingerprint(self, task):
        return hashlib.sha256((self.scope + task.digest).encode()).hexdigest()[:63]

    def run(self, args, stdin=None):
        c = self.config
        if c.mode == 'docker':
            prefix = ['docker'] + (['--context', c.docker_context] if c.docker_context else [])
        else:
            prefix = ['kubectl'] + (['--context', c.context] if c.context else []) + ['--namespace', c.namespace]
        return self.call(prefix + args, stdin, c.cli_timeout)

    def _object(self, task):
        if self.config.mode == 'docker':
            ids = self.run(['container', 'ls', '-a', '--filter', 'name=^/' + task.name + '$', '--format', '{{.ID}}']).strip()
            if not ids:
                return None
            obj = json.loads(self.run(['container', 'inspect', task.name]))[0]
            labels = obj.get('Config', {}).get('Labels') or {}
        else:
            raw = self.run(['get', 'job', task.name, '--ignore-not-found', '-o', 'json']).strip()
            if not raw:
                return None
            obj = json.loads(raw)
            labels = obj['metadata'].get('labels', {})
        if labels.get(LABEL) != OWNER or labels.get(FINGERPRINT) != self.fingerprint(task):
            raise RunnerError('resource name conflict or ownership mismatch')
        return obj

    def job_manifest(self, task):
        labels = {LABEL: OWNER, FINGERPRINT: self.fingerprint(task)}
        return {'apiVersion': 'batch/v1', 'kind': 'Job',
                'metadata': {'name': task.name, 'namespace': self.config.namespace, 'labels': labels},
                'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': task.timeout_seconds,
                         'template': {'metadata': {'labels': labels}, 'spec': {
                             'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                             'securityContext': {'runAsNonRoot': True, 'runAsUser': 65532, 'seccompProfile': {'type': 'RuntimeDefault'}},
                             'containers': [{'name': 'evaluation', 'image': task.image,
                                 'command': task.command,
                                 'env': [{'name': 'EVALUATION_MANIFEST', 'value': json.dumps(task.manifest)}],
                                 'resources': {'requests': {'cpu': str(task.cpu), 'memory': f'{task.memory_mb}Mi'},
                                               'limits': {'cpu': str(task.cpu), 'memory': f'{task.memory_mb}Mi'}},
                                 'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}},
                                 'volumeMounts': [{'name': 'tmp', 'mountPath': '/tmp'}]}],
                             'volumes': [{'name': 'tmp', 'emptyDir': {'sizeLimit': '128Mi'}}]}}}}

    def submit(self, task: Task):
        spec = json.dumps(asdict(task), sort_keys=True)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT scope,spec,state FROM jobs WHERE id=?', (task.id,)).fetchone()
            if row and (row[0] != self.scope or row[1] != spec):
                raise RunnerError('task id already bound to a different spec or backend')
            if row and row[2] != 'SUBMITTING':
                return self._saved(db, task.id)
            if not row:
                db.execute('INSERT INTO jobs(id,scope,spec,state) VALUES(?,?,?,?)', (task.id, self.scope, spec, 'SUBMITTING'))
        # Intent persisted before CLI calls; uncertain submission can be reconciled.
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if self._saved(db, task.id).state != 'SUBMITTING':
                return self._saved(db, task.id)
            self._launch(task)
            db.execute('UPDATE jobs SET state=? WHERE id=? AND state=?', ('QUEUED', task.id, 'SUBMITTING'))
        return self.status(task.id)

    def _launch(self, task):
        obj = self._object(task)
        if obj is None:
            if self.config.mode == 'docker':
                self.run(['container', 'create', '--name', task.name,
                          '--label', LABEL + '=' + OWNER, '--label', FINGERPRINT + '=' + self.fingerprint(task),
                          '--restart=no', '--cpus', str(task.cpu), '--memory', f'{task.memory_mb}m',
                          '--network', self.config.network, '--read-only', '--cap-drop=ALL',
                          '--security-opt=no-new-privileges', '--user', '65532:65532',
                          '--pids-limit', '256', '--tmpfs', '/tmp:rw,nosuid,nodev,size=128m',
                          '--log-opt', 'max-size=10m', '--log-opt', 'max-file=3',
                          '--env', 'EVALUATION_MANIFEST=' + json.dumps(task.manifest),
                          '--entrypoint', task.command[0], task.image, *task.command[1:]])
            else:
                self.run(['create', '-f', '-'], json.dumps(self.job_manifest(task)))
        if self.config.mode == 'docker':
            obj = self._object(task)
            if obj['State']['Status'] == 'created':
                self.run(['container', 'start', task.name])

    def _saved(self, db, job_id):
        row = db.execute('SELECT state,detail,exit_code FROM jobs WHERE id=?', (job_id,)).fetchone()
        return Status(job_id, *row)

    def _task(self, db, job_id):
        row = db.execute('SELECT scope,spec FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise RunnerError('unknown task')
        if row[0] != self.scope:
            raise RunnerError('task belongs to another backend/context/namespace')
        return Task(**json.loads(row[1]))

    def status(self, job_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self._task(db, job_id)
            saved = self._saved(db, job_id)
            if saved.state in TERMINAL:
                return saved
            obj = self._object(task)
            if saved.state == 'CANCELLING':
                if obj:
                    self._stop(task, obj)
                result = Status(job_id, 'CANCELLED')
            elif obj is None:
                result = Status(job_id, 'SUBMITTING' if saved.state == 'SUBMITTING' else 'LOST', 'runtime resource not found')
            elif self.config.mode == 'docker':
                state = obj['State']
                if state['Status'] in {'exited', 'dead'}:
                    code = state.get('ExitCode', 1)
                    result = Status(job_id, 'SUCCEEDED' if code == 0 else 'FAILED', 'OOMKilled' if state.get('OOMKilled') else '', code)
                elif state['Status'] == 'running':
                    from datetime import datetime, timezone
                    started = datetime.fromisoformat(state['StartedAt'][:26].replace('Z', '')).replace(tzinfo=timezone.utc)
                    if (datetime.now(timezone.utc) - started).total_seconds() >= task.timeout_seconds:
                        self._stop(task, obj)
                        result = Status(job_id, 'TIMED_OUT', 'execution deadline exceeded')
                    else:
                        result = Status(job_id, 'RUNNING')
                else:
                    result = Status(job_id, 'SUBMITTING' if saved.state == 'SUBMITTING' else 'QUEUED')
            else:
                conditions = {x['type']: x for x in obj.get('status', {}).get('conditions', []) if x.get('status') == 'True'}
                if 'Complete' in conditions:
                    result = Status(job_id, 'SUCCEEDED', exit_code=0)
                elif 'Failed' in conditions:
                    reason = conditions['Failed'].get('reason', 'JobFailed')
                    result = Status(job_id, 'TIMED_OUT' if reason == 'DeadlineExceeded' else 'FAILED', reason)
                else:
                    result = Status(job_id, 'RUNNING' if obj.get('status', {}).get('active') else 'QUEUED')
            db.execute('UPDATE jobs SET state=?,detail=?,exit_code=? WHERE id=?', (result.state, result.detail, result.exit_code, job_id))
            return result

    def _stop(self, task, obj):
        if self.config.mode == 'docker':
            if obj['State']['Status'] == 'running':
                self.run(['container', 'stop', '--time', '10', task.name])
        else:
            self.run(['delete', 'job', task.name, '--ignore-not-found', '--cascade=foreground', '--wait=true', '--timeout=40s'])

    def cancel(self, job_id):
        current = self.status(job_id)
        if current.state in TERMINAL:
            return current
        with self.db() as db:
            self._task(db, job_id)
            db.execute('UPDATE jobs SET state=? WHERE id=? AND state NOT IN (?,?,?,?)', ('CANCELLING', job_id, *sorted(TERMINAL)))
        return self.status(job_id)

    def cleanup(self, job_id):
        current = self.status(job_id)
        if current.state not in TERMINAL:
            raise RunnerError('cancel or wait for terminal state before cleanup')
        with self.db() as db:
            task = self._task(db, job_id)
        obj = self._object(task)
        if obj:
            if self.config.mode == 'docker':
                self.run(['container', 'rm', task.name])
            else:
                self._stop(task, obj)
        return current

    def logs(self, job_id, tail=200):
        if not 1 <= tail <= 10000:
            raise ValueError('tail must be 1..10000')
        with self.db() as db:
            task = self._task(db, job_id)
        if not self._object(task):
            raise RunnerError('logs unavailable: runtime resource removed')
        if self.config.mode == 'docker':
            return self.run(['container', 'logs', '--tail', str(tail), task.name])
        return self.run(['logs', 'job/' + task.name, '--all-containers=true', '--tail', str(tail)])

    def wait(self, job_id):
        import time
        while True:
            result = self.status(job_id)
            if result.state in TERMINAL:
                return result
            if result.state in {'LOST', 'SUBMITTING'}:
                raise RunnerError('submission/resource needs reconciliation before waiting')
            time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description='Internal evaluation workload runner')
    parser.add_argument('action', choices=['submit', 'status', 'cancel', 'cleanup', 'logs', 'wait'])
    parser.add_argument('value', help='task JSON path for submit; task id otherwise')
    args = parser.parse_args()
    runner = Runner(Config.from_env())
    try:
        if args.action == 'submit':
            result = runner.submit(Task(**json.loads(Path(args.value).read_text())))
        else:
            result = getattr(runner, args.action)(args.value)
        print(result if isinstance(result, str) else json.dumps(asdict(result)))
        if args.action == 'wait' and result.state != 'SUCCEEDED':
            parser.exit(2)
    except (RunnerError, ValueError) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
