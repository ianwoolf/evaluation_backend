import json
from dataclasses import replace
from datetime import datetime, timezone, timedelta

import pytest

from evaluation_backend.runner import Config, Task, Runner, RunnerError, LABEL, FINGERPRINT, OWNER


class Runtime:
    def __init__(self, mode):
        self.mode, self.obj, self.calls = mode, None, []
        self.fail_start = False

    def __call__(self, args, stdin, timeout):
        self.calls.append((args, stdin))
        if 'inspect' in args:
            return json.dumps([self.obj])
        if 'ls' in args:
            return 'container-id' if self.obj else ''
        if 'get' in args:
            return json.dumps(self.obj) if self.obj else ''
        if 'create' in args:
            if self.mode == 'k8s':
                self.obj = json.loads(stdin)
                self.obj['status'] = {}
            else:
                labels = dict(args[i + 1].split('=', 1) for i, arg in enumerate(args) if arg == '--label')
                self.obj = {'Config': {'Labels': labels}, 'State': {'Status': 'created'}}
        if 'start' in args:
            if self.fail_start:
                raise RunnerError('uncertain start')
            self.obj['State'] = {'Status': 'running', 'StartedAt': datetime.now(timezone.utc).isoformat()}
        if 'stop' in args:
            self.obj['State'] = {'Status': 'exited', 'ExitCode': 137}
        if 'rm' in args or 'delete' in args:
            self.obj = None
        if 'logs' in args:
            return 'worker output'
        return ''


@pytest.fixture
def task():
    return Task('test-1', 'busybox:1.36', ['/bin/sh', '-c', 'echo done'], {'evaluationId': 'e-1'})


def runner(tmp_path, mode, runtime):
    return Runner(Config(mode=mode, state_db=str(tmp_path / 'runner.db')), runtime)


@pytest.mark.parametrize('mode', ['docker', 'k8s'])
def test_submit_idempotency_and_conflict(tmp_path, mode, task):
    runtime = Runtime(mode)
    r = runner(tmp_path, mode, runtime)
    assert r.submit(task).state in {'RUNNING', 'QUEUED'}
    r.submit(task)
    assert sum('create' in args for args, _ in runtime.calls) == 1
    with pytest.raises(RunnerError, match='different spec'):
        r.submit(replace(task, image='another-image'))


@pytest.mark.parametrize('mode', ['docker', 'k8s'])
def test_cancel_survives_service_restart_and_cleanup(tmp_path, mode, task):
    runtime = Runtime(mode)
    r = runner(tmp_path, mode, runtime)
    r.submit(task)
    assert r.cancel(task.id).state == 'CANCELLED'
    restarted = runner(tmp_path, mode, runtime)
    assert restarted.status(task.id).state == 'CANCELLED'
    assert restarted.cleanup(task.id).state == 'CANCELLED'
    assert runtime.obj is None
    assert restarted.cleanup(task.id).state == 'CANCELLED'


def test_docker_uncertain_start_reconciles_without_duplicate(tmp_path, task):
    runtime = Runtime('docker')
    runtime.fail_start = True
    r = runner(tmp_path, 'docker', runtime)
    with pytest.raises(RunnerError):
        r.submit(task)
    assert r.status(task.id).state == 'SUBMITTING'
    runtime.fail_start = False
    assert r.submit(task).state == 'RUNNING'
    assert sum('create' in args for args, _ in runtime.calls) == 1


@pytest.mark.parametrize('code,expected', [(0, 'SUCCEEDED'), (1, 'FAILED'), (137, 'FAILED')])
def test_docker_completion_and_oom(tmp_path, task, code, expected):
    runtime = Runtime('docker')
    r = runner(tmp_path, 'docker', runtime)
    r.submit(task)
    runtime.obj['State'] = {'Status': 'exited', 'ExitCode': code, 'OOMKilled': code == 137}
    assert r.status(task.id).state == expected
    assert r.status(task.id).exit_code == code
    if code == 137:
        assert r.status(task.id).detail == 'OOMKilled'
    assert r.cleanup(task.id).state == expected


def test_docker_timeout_stops_workload(tmp_path, task):
    runtime = Runtime('docker')
    r = runner(tmp_path, 'docker', runtime)
    r.submit(task)
    runtime.obj['State']['StartedAt'] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert r.status(task.id).state == 'TIMED_OUT'
    assert any('stop' in args for args, _ in runtime.calls)


@pytest.mark.parametrize('condition,reason,expected', [('Complete', '', 'SUCCEEDED'), ('Failed', 'DeadlineExceeded', 'TIMED_OUT'), ('Failed', 'BackoffLimitExceeded', 'FAILED')])
def test_k8s_terminal_mapping(tmp_path, task, condition, reason, expected):
    runtime = Runtime('k8s')
    r = runner(tmp_path, 'k8s', runtime)
    r.submit(task)
    runtime.obj['status'] = {'conditions': [{'type': condition, 'status': 'True', 'reason': reason}]}
    assert r.status(task.id).state == expected


def test_manifest_limits_and_no_shell_expansion(tmp_path, task):
    runtime = Runtime('k8s')
    r = runner(tmp_path, 'k8s', runtime)
    obj = r.job_manifest(task)
    assert obj['spec']['backoffLimit'] == 0
    assert obj['spec']['activeDeadlineSeconds'] == task.timeout_seconds
    pod = obj['spec']['template']['spec']
    assert pod['automountServiceAccountToken'] is False
    assert pod['restartPolicy'] == 'Never'
    assert pod['containers'][0]['command'] == task.command
    assert len(obj['metadata']['labels'][FINGERPRINT]) <= 63
    assert pod['containers'][0]['resources']['limits']['memory'] == '512Mi'


def test_docker_command_has_explicit_entrypoint_and_limits(tmp_path, task):
    runtime = Runtime('docker')
    r = runner(tmp_path, 'docker', runtime)
    r.submit(task)
    args = next(args for args, _ in runtime.calls if 'create' in args)
    assert args[args.index('--entrypoint') + 1] == '/bin/sh'
    assert '--read-only' in args and '--cap-drop=ALL' in args
    assert args[-3:] == [task.image, '-c', 'echo done']


def test_refuse_foreign_resource_and_cross_backend(tmp_path, task):
    runtime = Runtime('docker')
    r = runner(tmp_path, 'docker', runtime)
    runtime.obj = {'Config': {'Labels': {LABEL: 'foreign'}}}
    with pytest.raises(RunnerError, match='ownership'):
        r.submit(task)
    other = runner(tmp_path, 'k8s', Runtime('k8s'))
    with pytest.raises(RunnerError, match='another backend'):
        other.cancel(task.id)


def test_active_cleanup_forbidden_and_missing_resource_explicit(tmp_path, task):
    runtime = Runtime('docker')
    r = runner(tmp_path, 'docker', runtime)
    r.submit(task)
    with pytest.raises(RunnerError):
        r.cleanup(task.id)
    assert r.logs(task.id) == 'worker output'
    runtime.obj = None
    assert r.status(task.id).state == 'LOST'


@pytest.mark.parametrize('data', [{'mode': 'invalid'}, {'namespace': '../bad'}])
def test_invalid_config(data):
    with pytest.raises(ValueError):
        Config(**data)


def test_config_environment(monkeypatch):
    monkeypatch.setenv('EVAL_RUNNER_MODE', 'k8s')
    monkeypatch.setenv('EVAL_K8S_NAMESPACE', 'evaluation')
    assert Config.from_env().mode == 'k8s'
    assert Config.from_env().namespace == 'evaluation'


@pytest.mark.parametrize('changes', [{'id': '../bad'}, {'image': '-v'}, {'command': []}, {'cpu': 0}, {'timeout_seconds': 0}, {'manifest': {'data': 'x' * 17000}}])
def test_invalid_task(task, changes):
    with pytest.raises(ValueError):
        replace(task, **changes)
