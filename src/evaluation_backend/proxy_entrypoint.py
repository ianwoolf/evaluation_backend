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
    proxy = subprocess.Popen([sys.executable, '-m', 'evaluation_backend.llm_proxy'])
    child = None
    def stop(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, stop)
    try:
        for _ in range(120):
            if proxy.poll() is not None:
                raise RuntimeError('LLM proxy failed to start')
            try:
                with urlopen(f'http://127.0.0.1:{port}/health', timeout=1):
                    break
            except Exception:
                time.sleep(.5)
        else:
            raise RuntimeError('LLM proxy startup timeout')
        env = dict(os.environ)
        env.update(OPENAI_BASE_URL=f'http://127.0.0.1:{port}/v1', OPENAI_API_BASE=f'http://127.0.0.1:{port}/v1', OPENAI_API_KEY=env['EVAL_LLM_PROXY_TOKEN'])
        # Workload needs only the local proxy credential, not resolver/provider credentials.
        env.pop('EVAL_MODEL_RESOLVER_TOKEN', None)
        child = subprocess.Popen(command, env=env)
        while child.poll() is None:
            if proxy.poll() is not None:
                raise RuntimeError('LLM proxy exited during workload')
            time.sleep(.2)
        return child.returncode
    finally:
        for process in (child, proxy):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == '__main__':
    raise SystemExit(main())
