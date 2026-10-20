"""OpenShell lifecycle; modes select explicit gateways, never raw containers."""
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from evaluation_backend.experiment import Settings

ENV_KEYS = ('EXPERIMENT_ID', 'EVAL_TARGET_MODE', 'EVAL_SCORER_MODE', 'DATASET_ID', 'EVALUATE_ID', 'EVALUATION_ID', 'SCORER_IDS', 'DATASET_VERSION',
            'EVAL_PLATFORM', 'EVAL_CONCURRENCY', 'EVAL_TARGET_URL', 'EVAL_TARGET_TOKEN', 'EVAL_MODEL_ID',
            'EVAL_MODEL_RESOLVER_URL', 'EVAL_MODEL_RESOLVER_TOKEN', 'EVAL_LLM_PROXY_ENABLED',
            'LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY', 'LANGFUSE_BASE_URL')


class OpenShellRunner:
    def __init__(self, env=None, call=None):
        self.env = dict(os.environ if env is None else env)
        self.call = call or subprocess.run
        self.mode = self.env.get('EVAL_RUNNER_MODE', 'docker')
        if self.mode not in {'docker', 'k8s'}:
            raise ValueError('EVAL_RUNNER_MODE must be docker or k8s')
        self.gateway = self.env.get('EVAL_OPENSHELL_' + self.mode.upper() + '_GATEWAY')
        if not self.gateway:
            raise ValueError('explicit OpenShell gateway registration required for ' + self.mode)
        self.image = self.env.get('EVAL_WORKER_IMAGE')
        self.policy = self.env.get('EVAL_OPENSHELL_POLICY')
        if not self.image or self.image.startswith('-') or not self.policy or not Path(self.policy).is_file():
            raise ValueError('EVAL_WORKER_IMAGE and existing EVAL_OPENSHELL_POLICY required')
        self.timeout = int(self.env.get('EVAL_EXECUTION_TIMEOUT', '600'))
        if not 1 <= self.timeout <= 604800:
            raise ValueError('invalid execution timeout')

    def command(self, args, timeout):
        # Do not inherit host provider tokens for CLI auto-discovery.
        clean = {k: v for k, v in os.environ.items() if k in {'PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'XDG_CONFIG_HOME', 'SSL_CERT_FILE', 'SSL_CERT_DIR'}}
        result = self.call([self.env.get('EVAL_OPENSHELL_BIN', 'openshell'), '--gateway', self.gateway, *args],
                           text=True, capture_output=True, timeout=timeout, env=clean)
        if result.returncode:
            raise RuntimeError('OpenShell command failed; inspect named sandbox logs')
        return result

    def run(self):
        from evaluation_backend.telemetry import prepare_experiment
        experiment_id = prepare_experiment(self.env, fresh=True)
        settings = Settings.from_env(self.env)
        name = 'eval-' + uuid.uuid4().hex[:14]
        print(json.dumps({'sandbox': name, 'gateway': self.gateway, 'experiment_id': experiment_id, 'status': 'CREATING'}), flush=True)
        created, success = False, False
        with tempfile.TemporaryDirectory(prefix='eval-env-') as directory:
            path = Path(directory) / 'worker-env.json'
            fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w') as stream:
                payload = {key: self.env[key] for key in ENV_KEYS if key in self.env}
                payload['DATASET_VERSION'] = settings.dataset_version.isoformat()
                json.dump(payload, stream)
            try:
                self.command(['sandbox', 'create', '--name', name, '--from', self.image,
                              '--policy', self.policy, '--label', 'evaluation.backend/owner=evaluation-backend', '--label', 'experiment_id=' + experiment_id,
                              '--detach', '--',
                              'python', '-c', f'import time; time.sleep({self.timeout + 120})'], 120)
                created = True
                self.command(['sandbox', 'upload', name, str(path), '/tmp/worker-env.json'], 120)
                result = self.command(['sandbox', 'exec', '-n', name, '--', 'python', '-m',
                                       'evaluation_backend.worker', '--env-file', '/tmp/worker-env.json'], self.timeout)
                print(result.stdout, end='')
                success = True
            finally:
                # Retain failed sandboxes for diagnosis. No blind automatic rerun
                # after uncertain exec, which could repeat target side effects.
                if created and success:
                    self.command(['sandbox', 'delete', name], 60)
                elif created:
                    # Bound lifetime even if exec transport lost its connection.
                    self.command(['sandbox', 'stop', name], 60)
                    print(json.dumps({'sandbox': name, 'status': 'FAILED_STOPPED', 'manual_cleanup_required': True}), file=sys.stderr)
                else:
                    print(json.dumps({'sandbox': name, 'status': 'CREATE_UNCERTAIN', 'inspect_before_retry': True}), file=sys.stderr)
        return name


def main():
    def cancelled(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, cancelled)
    import argparse
    from evaluation_backend.config import load_config
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, help='dotenv configuration; environment takes precedence')
    args = parser.parse_args()
    try:
        load_config(args.config)
        OpenShellRunner().run()
    except (Exception, KeyboardInterrupt) as error:
        print(json.dumps({'status': 'FAILED', 'error_type': type(error).__name__}), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
