"""Run a workload behind a local LLM proxy in an existing Docker/OpenShell sandbox."""
import os
import secrets
import signal
import subprocess
import sys
import time
from urllib.request import urlopen
from evaluation_backend.config import load_config


def main():
    load_config()
    from evaluation_backend.telemetry import prepare_experiment
    prepare_experiment(os.environ)
    command = sys.argv[1:]
    if command[:1] == ['--']:
        command = command[1:]
    command = command or [sys.executable, '-m', 'evaluation_backend.worker']
    os.environ.setdefault('EVAL_LLM_PROXY_TOKEN', secrets.token_urlsafe(32))
    port = int(os.getenv('EVAL_LLM_PROXY_PORT', '18090'))
    collector_port = int(os.getenv('EVAL_COLLECTOR_PORT', '18091'))
    os.environ['EVAL_COLLECTOR_URL'] = f'http://127.0.0.1:{collector_port}'
    collector_env = dict(os.environ)
    collector_env['EVAL_COLLECTOR_LOCAL'] = 'true'
    collector_env['EVAL_TRIGGER_TOKEN'] = os.environ.get('EVAL_STORAGE_TOKEN', '')
    collector = subprocess.Popen([sys.executable, '-m', 'evaluation_backend.collector'], env=collector_env)
    proxy_env = {k: v for k, v in os.environ.items() if not k.startswith('EVAL_ES_')}
    proxy = subprocess.Popen([sys.executable, '-m', 'evaluation_backend.llm_proxy'], env=proxy_env)
    child = None
    def stop(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, stop)
    try:
        for _ in range(120):
            if proxy.poll() is not None or collector.poll() is not None:
                raise RuntimeError('Runner service failed to start')
            try:
                with urlopen(f'http://127.0.0.1:{port}/health', timeout=1):
                    with urlopen(f'http://127.0.0.1:{collector_port}/health', timeout=1):
                        break
            except Exception:
                time.sleep(.5)
        else:
            raise RuntimeError('LLM proxy startup timeout')
        env = dict(os.environ)
        env.update(OPENAI_BASE_URL=f'http://127.0.0.1:{port}/v1', OPENAI_API_BASE=f'http://127.0.0.1:{port}/v1', OPENAI_API_KEY=env['EVAL_LLM_PROXY_TOKEN'])
        # Workload needs only the local proxy credential, not resolver/provider credentials.
        env.pop('EVAL_MODEL_RESOLVER_TOKEN', None)
        for key in list(env):
            if key.startswith('EVAL_ES_'):
                env.pop(key)
        child = subprocess.Popen(command, env=env)
        while child.poll() is None:
            if proxy.poll() is not None or collector.poll() is not None:
                raise RuntimeError('Runner service exited during workload')
            time.sleep(.2)
        return child.returncode
    finally:
        for process in (child, proxy, collector):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == '__main__':
    raise SystemExit(main())
