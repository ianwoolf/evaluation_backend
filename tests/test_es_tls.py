"""Exercise real TLS handshakes with an untrusted, locally generated certificate."""
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from evaluation_backend.storage import StorageError
from evaluation_backend.storage.elastic import ElasticsearchStorage


@pytest.fixture
def tls_server(tmp_path):
    openssl = shutil.which('openssl')
    if not openssl:
        pytest.skip('openssl is required for the local TLS fixture')
    cert, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    subprocess.run([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                    '-keyout', str(key), '-out', str(cert), '-subj', '/CN=localhost',
                    '-addext', 'subjectAltName=DNS:localhost'], check=True, capture_output=True)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'https://localhost:{server.server_port}', str(cert)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_ssl_verification_defaults_off_and_can_be_enabled(monkeypatch, tls_server):
    url, cert = tls_server
    monkeypatch.setenv('EVAL_ES_URL', url)
    for key in ('EVAL_ES_VERIFY_SSL', 'EVAL_ES_CA_FILE', 'EVAL_ES_API_KEY', 'EVAL_ES_USERNAME', 'EVAL_ES_PASSWORD'):
        monkeypatch.delenv(key, raising=False)
    storage = ElasticsearchStorage.from_env()
    try:
        assert storage.call('GET', '/') == {'ok': True}
    finally:
        storage.client.close()
    monkeypatch.setenv('EVAL_ES_VERIFY_SSL', 'true')
    storage = ElasticsearchStorage.from_env()
    try:
        with pytest.raises(StorageError):
            storage.call('GET', '/')
    finally:
        storage.client.close()
    monkeypatch.setenv('EVAL_ES_CA_FILE', cert)
    storage = ElasticsearchStorage.from_env()
    try:
        assert storage.call('GET', '/') == {'ok': True}
    finally:
        storage.client.close()
    monkeypatch.setenv('EVAL_ES_VERIFY_SSL', 'false')
    monkeypatch.setenv('EVAL_ES_CA_FILE', '/nonexistent/ignored.pem')
    storage = ElasticsearchStorage.from_env()
    try:
        assert storage.call('GET', '/') == {'ok': True}
    finally:
        storage.client.close()


def test_ssl_invalid_setting_is_rejected(monkeypatch):
    monkeypatch.setenv('EVAL_ES_VERIFY_SSL', 'typo')
    with pytest.raises(ValueError, match='EVAL_ES_VERIFY_SSL'):
        ElasticsearchStorage.from_env()
