"""Structured operational logs. Business payloads and exception messages stay out."""
import atexit
import contextvars
import functools
import inspect
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
import uuid
from urllib.parse import urlparse
from contextlib import contextmanager
from datetime import datetime, timezone

_context = contextvars.ContextVar('evaluation_log_context', default={})
_logger = logging.getLogger('evaluation.operations')
_logger.propagate = False
_lock = threading.RLock()
_provider = None
_service = None
_FIELDS = {'request_id', 'project_id', 'run_id', 'sample_id', 'dataset_id', 'environment_id',
           'target_id', 'model_id', 'scorer_id', 'job_name', 'upload_id', 'collection', 'operation',
           'method', 'route', 'status_code', 'status', 'count', 'duration_ms', 'exit_code',
           'component', 'version', 'scoring_mode', 'cpu_millicores', 'memory_mib', 'phase'}


class JsonHandler(logging.Handler):
    def emit(self, record):
        # Resolve stdout at emit time (also works with CLI/test capture).
        sys.stdout.write(record.getMessage() + '\n')
        sys.stdout.flush()


class OTelHandler(logging.Handler):
    def __init__(self, provider):
        super().__init__()
        self.otel = provider.get_logger('evaluation.operations')

    def emit(self, record):
        try:
            from opentelemetry._logs import SeverityNumber
            severity = SeverityNumber.ERROR if record.levelno >= logging.ERROR else (
                SeverityNumber.WARN if record.levelno >= logging.WARNING else SeverityNumber.INFO)
            self.otel.emit(body=record.getMessage(), severity_number=severity,
                           severity_text=record.levelname, attributes=getattr(record, 'attributes', {}))
        except Exception:
            # stdout already contains this log. Never recursively log exporter failures.
            pass


def configure_logging(service='evaluation-backend'):
    global _provider, _service
    with _lock:
        if _service == service:
            return
        shutdown_logging()
        _service = service
        _logger.handlers = [JsonHandler()]
        _logger.setLevel(getattr(logging, os.getenv('EVAL_LOG_LEVEL', 'INFO').upper(), logging.INFO))
        endpoint = os.getenv('OTEL_EXPORTER_OTLP_LOGS_ENDPOINT') or os.getenv('OTEL_EXPORTER_OTLP_ENDPOINT')
        if endpoint and os.getenv('OTEL_SDK_DISABLED', '').lower() != 'true' and os.getenv('OTEL_LOGS_EXPORTER', 'otlp') != 'none':
            try:
                from opentelemetry.sdk._logs import LoggerProvider
                from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
                from opentelemetry.sdk.resources import Resource
                from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
                # HTTP/protobuf; standard env vars supply endpoint, TLS and auth headers.
                protocol = os.getenv('OTEL_EXPORTER_OTLP_LOGS_PROTOCOL', os.getenv('OTEL_EXPORTER_OTLP_PROTOCOL', 'http/protobuf'))
                if protocol != 'http/protobuf':
                    raise ValueError('Unsupported logging protocol')
                parsed = urlparse(endpoint)
                if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise ValueError('Invalid logging endpoint')
                provider = LoggerProvider(resource=Resource.create({'service.name': service}))
                provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(timeout=5)))
                _provider = provider
                _logger.addHandler(OTelHandler(provider))
            except Exception as error:
                event('logging.otel_setup_failed', level='ERROR', error=error)
        event('logging.configured', component='otel+stdout' if _provider else 'stdout')


def shutdown_logging():
    global _provider, _service
    provider, _provider = _provider, None
    if provider is not None:
        try:
            provider.force_flush(timeout_millis=5000)
            provider.shutdown()
        except Exception:
            pass
    _service = None


atexit.register(shutdown_logging)


def event(name, *, level='INFO', error=None, **fields):
    if _service is None:
        configure_logging(os.getenv('EVAL_LOG_SERVICE', 'evaluation-backend'))
    data = {'timestamp': datetime.now(timezone.utc).isoformat(), 'level': level,
            'service': _service, 'event': name}
    values = {'project_id': os.getenv('EVAL_PROJECT_ID'), 'run_id': os.getenv('EXPERIMENT_ID'),
              **_context.get(), **fields}
    data.update({k: (v[:200] if isinstance(v, str) else v) for k, v in values.items()
                 if k in _FIELDS and v is not None and isinstance(v, (str, int, float, bool))})
    if error is not None:
        data['error_type'] = type(error).__name__
        data['error_stack'] = [{'file': os.path.basename(f.filename), 'line': f.lineno, 'function': f.name}
                               for f in traceback.extract_tb(error.__traceback__)[-12:]]
        code = getattr(error, 'status_code', None) or getattr(getattr(error, 'response', None), 'status_code', None)
        if isinstance(code, int):
            data['status_code'] = code
    try:
        from opentelemetry.trace import get_current_span
        span = get_current_span().get_span_context()
        if span.is_valid:
            data.update(trace_id=f'{span.trace_id:032x}', span_id=f'{span.span_id:016x}')
    except ImportError:
        pass
    _logger.log(getattr(logging, level), json.dumps(data, ensure_ascii=False),
                extra={'attributes': {k: v for k, v in data.items() if isinstance(v, (str, int, float, bool))}})


@contextmanager
def log_context(**fields):
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


@contextmanager
def operation(name, **fields):
    started = time.monotonic()
    with log_context(**fields):
        event(name + '.started')
        try:
            yield
        except BaseException as error:
            expected = isinstance(error, (KeyboardInterrupt, SystemExit, ValueError)) or type(error).__name__ in ('NotFound', 'Conflict', 'UploadError') or 400 <= getattr(error, 'status_code', 0) < 500
            event(name + '.failed', level='WARNING' if expected else 'ERROR',
                  error=error, duration_ms=round((time.monotonic() - started) * 1000, 2))
            raise
        else:
            event(name + '.completed', duration_ms=round((time.monotonic() - started) * 1000, 2))


def observed(name, **bindings):
    """Explicit safe argument names only; never serialize args, results or exception text."""
    def decorate(fn):
        signature = inspect.signature(fn)
        def context(args, kwargs):
            bound = signature.bind_partial(*args, **kwargs).arguments
            return {field: bound[arg] for field, arg in bindings.items() if arg in bound}
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapped(*args, **kwargs):
                with operation(name, **context(args, kwargs)):
                    return await fn(*args, **kwargs)
        else:
            @functools.wraps(fn)
            def wrapped(*args, **kwargs):
                with operation(name, **context(args, kwargs)):
                    return fn(*args, **kwargs)
        return wrapped
    return decorate


def instrument_app(app):
    @app.middleware('http')
    async def request_log(request, call_next):
        if request.url.path == '/health':
            return await call_next(request)
        supplied = request.headers.get('x-request-id', '')
        ident = supplied if re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', supplied) else uuid.uuid4().hex
        started = time.monotonic()
        with propagated_context(request.headers), log_context(request_id=ident, project_id=request.headers.get('x-project-id')):
            event('http.request.started', method=request.method)
            try:
                response = await call_next(request)
            except Exception as error:
                event('http.request.failed', level='ERROR', error=error, method=request.method,
                      duration_ms=round((time.monotonic() - started) * 1000, 2))
                from fastapi.responses import JSONResponse
                return JSONResponse(status_code=500, content={'detail': 'Internal server error'}, headers={'X-Request-ID': ident})
            route = getattr(request.scope.get('route'), 'path', 'unmatched')
            event('http.request.completed', level='ERROR' if response.status_code >= 500 else 'WARNING' if response.status_code >= 400 else 'INFO',
                  method=request.method, route=route, status_code=response.status_code,
                  duration_ms=round((time.monotonic() - started) * 1000, 2))
            response.headers['X-Request-ID'] = ident
            return response


@contextmanager
def propagated_context(headers):
    try:
        from opentelemetry import context, propagate
    except ImportError:
        yield
        return
    token = context.attach(propagate.extract(headers))
    try:
        yield
    finally:
        context.detach(token)


def correlation_headers():
    headers = {}
    if _context.get().get('request_id'):
        headers['X-Request-ID'] = _context.get()['request_id']
    try:
        from opentelemetry.trace import get_current_span
        span = get_current_span().get_span_context()
        if span.is_valid:
            headers['traceparent'] = f'00-{span.trace_id:032x}-{span.span_id:016x}-{int(span.trace_flags):02x}'
    except ImportError:
        pass
    return headers
