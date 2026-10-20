"""One experiment identity on all runner-owned OTEL spans, including SDK internals."""
import os
import uuid
from evaluation_backend.config import LangfuseConfig


def prepare_experiment(env, fresh=False):
    ident = None if fresh else env.get('EXPERIMENT_ID')
    ident = ident or 'exp-' + uuid.uuid4().hex
    env['EXPERIMENT_ID'] = ident
    env['EVALUATE_ID'] = ident
    env['EVALUATION_ID'] = ident
    return ident


def create_telemetry(config=None):
    from langfuse import Langfuse
    from opentelemetry.sdk.trace import TracerProvider, SpanProcessor
    from opentelemetry.sdk.resources import Resource
    ident = os.environ.get('EXPERIMENT_ID') or os.environ.get('EVALUATE_ID')
    if not ident:
        raise ValueError('EXPERIMENT_ID is required for runner telemetry')
    class ExperimentLabels(SpanProcessor):
        def on_start(self, span, parent_context=None):
            span.set_attribute('experiment_id', ident)
            span.set_attribute('langfuse.observation.metadata.experiment_id', ident)
            span.set_attribute('langfuse.trace.metadata.experiment_id', ident)
            span.set_attribute('langfuse.trace.tags', ['experiment_id:' + ident])
        def on_end(self, span): pass
        def shutdown(self): pass
        def force_flush(self, timeout_millis=30000): return True
    provider = TracerProvider(resource=Resource.create({'service.name': 'evaluation-runner', 'experiment_id': ident}))
    provider.add_span_processor(ExperimentLabels())
    config = config or LangfuseConfig.from_env()
    return Langfuse(public_key=config.public_key, secret_key=config.secret_key,
                    base_url=config.base_url, tracer_provider=provider)
