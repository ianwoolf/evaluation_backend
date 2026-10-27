import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from evaluation_backend import observability as logs


@pytest.fixture(autouse=True)
def isolated_logging(monkeypatch):
    logs.shutdown_logging()
    for key in ('OTEL_EXPORTER_OTLP_ENDPOINT', 'OTEL_EXPORTER_OTLP_LOGS_ENDPOINT',
                'OTEL_EXPORTER_OTLP_PROTOCOL', 'OTEL_EXPORTER_OTLP_LOGS_PROTOCOL',
                'OTEL_SDK_DISABLED', 'OTEL_LOGS_EXPORTER', 'EVAL_LOG_LEVEL', 'EXPERIMENT_ID'):
        monkeypatch.delenv(key, raising=False)
    yield
    logs.shutdown_logging()


def test_stdout_failure_is_safe_and_context_resets(capsys):
    logs.configure_logging('test-worker')
    with pytest.raises(ValueError):
        with logs.operation('scorer.execute', run_id='r1', sample_id='s1', scorer_id='a'):
            raise ValueError('CANARY_SECRET raw-input')
    logs.event('safe', token='CANARY_SECRET', input={'private': 'CANARY_SECRET'})
    text = capsys.readouterr().out
    assert 'CANARY_SECRET' not in text and 'raw-input' not in text
    records = [json.loads(line) for line in text.splitlines()]
    failed = next(row for row in records if row['event'] == 'scorer.execute.failed')
    assert failed['run_id'] == 'r1' and failed['sample_id'] == 's1'
    assert failed['error_type'] == 'ValueError' and failed['error_stack']
    assert 'run_id' not in records[-1]


def test_http_errors_log_safe_route_and_request_id(capsys):
    app = FastAPI()
    logs.instrument_app(app)
    @app.get('/boom/{item}')
    def boom(item: int):
        raise RuntimeError('CANARY_SECRET')
    with TestClient(app) as client:
        invalid = client.get('/boom/wrong?token=CANARY_SECRET')
        failed = client.get('/boom/1', headers={'X-Request-ID': 'request-123'})
    assert invalid.status_code == 422 and failed.status_code == 500
    assert failed.headers['x-request-id'] == 'request-123'
    text = capsys.readouterr().out
    assert 'CANARY_SECRET' not in text
    rows = [json.loads(line) for line in text.splitlines()]
    assert any(r.get('status_code') == 422 and r.get('route') == '/boom/{item}' for r in rows)
    assert any(r['event'] == 'http.request.failed' and r.get('request_id') == 'request-123' for r in rows)


def test_otlp_http_receives_logs_and_stdout_remains(monkeypatch, capsys):
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
    received = []
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200)
            self.end_headers()
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv('OTEL_EXPORTER_OTLP_ENDPOINT', f'http://127.0.0.1:{server.server_port}')
        logs.configure_logging('test-otel')
        with logs.log_context(run_id='otel-run', sample_id='s1'):
            logs.event('sample.completed', status='PASS')
        logs.shutdown_logging()
        decoded = [ExportLogsServiceRequest.FromString(body) for path, body in received if path == '/v1/logs']
        bodies = [json.loads(record.body.string_value) for message in decoded for resource in message.resource_logs
                  for scope in resource.scope_logs for record in scope.log_records]
        assert any(body.get('run_id') == 'otel-run' for body in bodies)
        assert 'sample.completed' in capsys.readouterr().out
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_bad_otel_config_falls_back_to_stdout(monkeypatch, capsys):
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_LOGS_ENDPOINT', 'http://user:CANARY_SECRET@host/v1/logs')
    logs.configure_logging('bad-config')
    logs.event('still.running')
    output = capsys.readouterr().out
    assert 'logging.otel_setup_failed' in output and 'still.running' in output
    assert 'CANARY_SECRET' not in output


def test_supervisor_cleans_collector_if_proxy_spawn_fails(monkeypatch, capsys):
    from unittest.mock import Mock
    from evaluation_backend import proxy_entrypoint
    monkeypatch.setenv('EVAL_MODEL_ID', 'some-model')
    monkeypatch.setattr('sys.argv', ['supervisor'])
    collector = Mock()
    collector.poll.return_value = None
    monkeypatch.setattr(proxy_entrypoint.subprocess, 'Popen', Mock(side_effect=[collector, OSError('CANARY_SECRET')]))
    with pytest.raises(OSError):
        proxy_entrypoint.main()
    collector.terminate.assert_called_once()
    output = capsys.readouterr().out
    assert 'supervisor.failed' in output and 'CANARY_SECRET' not in output
