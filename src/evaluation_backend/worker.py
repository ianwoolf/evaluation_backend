"""Sandbox entry point; reads IDs and configuration from environment."""
import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from evaluation_backend.experiment import Settings, run_experiment
from evaluation_backend.scorers import InstalledScorerLibrary


class HttpGuardrail:
    def __init__(self, url, model_id=None, token=None):
        parsed = urlparse(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError('EVAL_TARGET_URL must be an HTTP(S) URL without credentials')
        self.url, self.model_id, self.token = url, model_id, token

    def __call__(self, value):
        messages = value.get('messages') if isinstance(value, dict) else value
        if not isinstance(messages, list) or not messages or not all(isinstance(x, dict) and isinstance(x.get('content'), str) and x.get('role') in {'system', 'user', 'assistant'} for x in messages):
            raise ValueError('target input requires conversation messages')
        payload = {'messages': messages}
        if self.model_id:
            payload['model'] = self.model_id
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        request = Request(self.url, data=json.dumps(payload).encode(), headers=headers, method='POST')
        with urlopen(request, timeout=30) as response:
            output = json.load(response)
        if not isinstance(output, dict) or type(output.get('has_issue')) is not bool:
            raise ValueError('Guardrail response requires boolean has_issue')
        return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--env-file', type=Path, help='trusted OpenShell uploaded JSON environment')
    parser.add_argument('--config', type=Path, help='dotenv configuration; environment takes precedence')
    args = parser.parse_args()
    from evaluation_backend.config import load_config
    load_config(args.config)
    if args.env_file:
        values = json.loads(args.env_file.read_text())
        if not isinstance(values, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in values.items()):
            parser.error('invalid environment file')
        os.environ.update(values)
    if os.getenv('EVAL_LLM_PROXY_ENABLED', '').lower() == 'true' and not os.getenv('OPENAI_BASE_URL'):
        import subprocess
        import secrets
        os.environ.setdefault('EVAL_LLM_PROXY_TOKEN', secrets.token_urlsafe(32))
        # Uploaded environment is loaded before spawning the proxy supervisor.
        return subprocess.call([sys.executable, '-m', 'evaluation_backend.proxy_entrypoint', '--', sys.executable, '-m', 'evaluation_backend.worker'])
    try:
        from evaluation_backend.telemetry import prepare_experiment
        prepare_experiment(os.environ)
        settings = Settings.from_env()
        from evaluation_backend.platforms.factory import create_platform
        platform = create_platform(settings.platform)
        from evaluation_backend.llm_target import LlmClassificationTarget, MockScorerLibrary
        target_mode = os.getenv('EVAL_TARGET_MODE', 'guardrail')
        scorer_mode = os.getenv('EVAL_SCORER_MODE', 'installed')
        if target_mode not in {'guardrail', 'llm'} or scorer_mode not in {'installed', 'mock'}:
            raise ValueError('unknown target/scorer mode')
        target = LlmClassificationTarget() if target_mode == 'llm' else HttpGuardrail(os.environ['EVAL_TARGET_URL'], os.getenv('EVAL_MODEL_ID'), os.getenv('EVAL_TARGET_TOKEN'))
        library = MockScorerLibrary() if scorer_mode == 'mock' else InstalledScorerLibrary()
        result = run_experiment(settings, platform, library, platform, target)
        result['experiment_id'] = settings.evaluation_id
        result['mock_scorer'] = scorer_mode == 'mock'
        print(json.dumps(result))
        return 0 if result['status'] == 'COMPLETED' else 1
    except Exception as error:
        # Vendor exceptions can contain auth headers or private sample data.
        print(json.dumps({'status': 'FAILED', 'error_type': type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
