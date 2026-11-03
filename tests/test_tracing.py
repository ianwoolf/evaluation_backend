import pytest
from evaluation_backend import tracing
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

def test_disabled_without_collector(monkeypatch):
    monkeypatch.delenv('OTEL_EXPORTER_OTLP_ENDPOINT',raising=False)
    monkeypatch.delenv('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT',raising=False)
    monkeypatch.setattr(tracing,'_provider',None)
    tracing.configure()
    with tracing.span('test') as span:assert not span.is_recording()

def test_parent_error_and_content_policy(monkeypatch):
    exporter=InMemorySpanExporter();provider=TracerProvider();provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing,'_provider',provider)
    monkeypatch.setenv('EVAL_CONTENT_POLICY','{"input":"omit"}')
    with tracing.span('request') as parent:
        with pytest.raises(ValueError):
            with tracing.span('guardrail.scan',client=True) as child:
                tracing.payload(child,'input',{'secret':'CANARY_INPUT'})
                tracing.payload(child,'output',{'outcome':'blocked'})
                raise ValueError('CANARY_EXCEPTION')
    spans=exporter.get_finished_spans()
    assert spans[0].parent.span_id==spans[1].context.span_id
    assert spans[0].status.status_code.name=='ERROR'
    assert 'CANARY' not in str(spans[0].attributes)+str(spans[0].events)+str(spans[0].status.description)
    assert 'evaluation.output' in spans[0].attributes
    provider.shutdown()

def test_collector_accepts_standard_protobuf(monkeypatch):
    from fastapi.testclient import TestClient
    from evaluation_backend.collector import create_collector
    from evaluation_backend.storage.service import get_service
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    monkeypatch.setenv('EVAL_COLLECTOR_EXPORTER','elasticsearch')
    monkeypatch.setenv('EVAL_TRIGGER_TOKEN','test-token')
    monkeypatch.setenv('EVAL_PROJECT_ID','project')
    monkeypatch.setenv('EXPERIMENT_ID','exp-1')
    monkeypatch.setenv('EVAL_COLLECTOR_LOCAL','true')
    monkeypatch.setenv('EVAL_CONTENT_POLICY','{}')
    monkeypatch.setenv('EVAL_TARGET_SNAPSHOT','{"id":"g","name":"Guardrail","provider":"f5","version":1}')
    saved=[]
    class Store:
        def put(self,*args):saved.append(args)
    class Service:storage=Store()
    app=create_collector();app.dependency_overrides[get_service]=lambda:Service()
    request=ExportTraceServiceRequest();span=request.resource_spans.add().scope_spans.add().spans.add()
    span.trace_id=bytes.fromhex('ab'*16);span.span_id=bytes.fromhex('cd'*8);span.parent_span_id=bytes.fromhex('ef'*8)
    span.name='guardrail.scan';span.start_time_unix_nano=1000000;span.end_time_unix_nano=2000000
    attr=span.attributes.add();attr.key='evaluation.sample_id';attr.value.string_value='sample-1'
    with TestClient(app) as client:
        response=client.post('/v1/traces',content=request.SerializeToString(),headers={'Content-Type':'application/x-protobuf','Authorization':'Bearer test-token'})
        assert response.status_code==200
        assert response.headers['content-type']=='application/x-protobuf'
    record=saved[0][3]
    assert record['sample_id']=='sample-1'
    assert record['data']['span']['traceId']=='ab'*16
    assert record['data']['span']['parentSpanId']=='ef'*8
