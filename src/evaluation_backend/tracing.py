"""Optional standard OTel traces with a runner-local ES export path."""
import os
import json
import functools
from contextlib import contextmanager
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode, SpanKind
from evaluation_backend.content_policy import ContentPolicy
from evaluation_backend.observability import event

_provider=None

def configure():
    global _provider
    endpoint=os.getenv('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT') or os.getenv('OTEL_EXPORTER_OTLP_ENDPOINT')
    if _provider or not endpoint or os.getenv('OTEL_SDK_DISABLED','').lower()=='true' or os.getenv('OTEL_TRACES_EXPORTER','otlp')=='none': return
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    protocol=os.getenv('OTEL_EXPORTER_OTLP_TRACES_PROTOCOL',os.getenv('OTEL_EXPORTER_OTLP_PROTOCOL','http/protobuf'))
    if protocol!='http/protobuf': raise ValueError('Tracing supports OTLP HTTP/protobuf')
    target=endpoint if os.getenv('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT') else endpoint.rstrip('/')+'/v1/traces'
    from urllib.parse import urlsplit
    url=urlsplit(target)
    if url.scheme not in ('http','https') or not url.netloc or url.username or url.password: raise ValueError('Invalid trace collector endpoint')
    local=os.getenv('EVAL_COLLECTOR_URL','').rstrip('/')+'/v1/traces'
    provider=TracerProvider(resource=Resource.create({'service.name':'evaluation-runner','evaluation.id':os.getenv('EXPERIMENT_ID',''),'project.id':os.getenv('EVAL_PROJECT_ID','')}))
    headers={'Authorization':'Bearer '+os.getenv('EVAL_STORAGE_TOKEN','')}
    destinations=[target]
    if os.getenv('EVAL_COLLECTOR_URL') and local!=target: destinations.append(local)
    for address in destinations:
        exporter=OTLPSpanExporter(endpoint=address,headers=headers if address==local else None,timeout=5)
        provider.add_span_processor(BatchSpanProcessor(exporter,schedule_delay_millis=1000,max_export_batch_size=64))
    _provider=provider
    event('tracing.configured',component='otlp')

def shutdown():
    global _provider
    provider,_provider=_provider,None
    if provider:
        provider.force_flush(timeout_millis=10000)
        provider.shutdown()

@contextmanager
def span(name,attributes=None,client=False):
    tracer=_provider.get_tracer('evaluation.runner') if _provider else trace.NoOpTracer()
    attrs={'evaluation.id':os.getenv('EXPERIMENT_ID',''),**(attributes or {})}
    with tracer.start_as_current_span(name,attributes=attrs,kind=SpanKind.CLIENT if client else SpanKind.INTERNAL,record_exception=False,set_status_on_exception=False) as current:
        try:
            yield current
        except BaseException as error:
            current.set_status(Status(StatusCode.ERROR,type(error).__name__))
            current.set_attribute('error.type',type(error).__name__)
            raise

def payload(current,kind,value):
    policy=ContentPolicy.from_env()
    mode={'input':policy.input,'output':policy.response,'reference':policy.reference}[kind]
    if current.is_recording() and mode=='retain':
        text=json.dumps(value,ensure_ascii=False,default=str)
        if len(text)>32000:
            current.set_attribute('evaluation.'+kind+'.truncated',True)
            text=text[:32000]
        current.set_attribute('evaluation.'+kind,text)

def traced(name):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(*args,**kwargs):
            with span(name): return fn(*args,**kwargs)
        return wrapped
    return decorate
