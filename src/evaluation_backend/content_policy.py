"""Run-owned serialization policy. Execution always receives unfiltered values."""
import json
import os
from copy import deepcopy
from typing import Literal
from pydantic import BaseModel, ConfigDict


class ContentPolicy(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    input: Literal['retain', 'omit'] = 'retain'
    response: Literal['retain', 'omit'] = 'retain'
    reference: Literal['retain', 'omit'] = 'retain'

    @classmethod
    def from_json(cls, value):
        return cls.model_validate(json.loads(value))

    @classmethod
    def from_env(cls):
        return cls.from_json(os.getenv('EVAL_CONTENT_POLICY', '{}'))

    @property
    def restricted(self):
        return 'omit' in self.model_dump().values()

    def sample(self, value):
        result = deepcopy(value)
        for field, mode in [('input', self.input), ('output', self.response), ('reference', self.reference)]:
            if mode == 'omit': result[field] = {}
        if self.restricted:
            result['error'] = 'Sample execution failed' if result.get('error') else None
            result['scores'] = [{**{k: s[k] for k in ('scorerId', 'scorerVersion', 'value', 'passed') if k in s}, 'reason': ''} for s in result.get('scores', [])]
            result['trace'] = [{'name': 'Execution step', 'kind': s.get('kind', 'SCORER'), 'durationMs': s.get('durationMs', 0), 'detail': ''} for s in result.get('trace', [])]
        return result

    def observation(self, value):
        if not self.restricted: return deepcopy(value)
        # Free-form attributes, events, parameters and exception messages can copy prompts.
        result = {k: deepcopy(value[k]) for k in ('duration_ms', 'completion_start_time', 'usage_details', 'level') if k in value}
        if 'usage_details' in result:
            result['usage_details'] = {k:v for k,v in result['usage_details'].items() if k in ('input','output','total') and type(v) in (int,float)}
        result['metadata'] = {k: v for k, v in value.get('metadata', {}).items()
                              if k in ('ttft_ms', 'duration_ms', 'usage_reported', 'interrupted') and isinstance(v, (bool, int, float, type(None)))}
        for field, mode in [('input', self.input), ('output', self.response), ('reference', self.reference)]:
            if mode == 'retain' and field in value: result[field] = deepcopy(value[field])
        return result

    def trace_data(self, value):
        if not self.restricted: return deepcopy(value)
        if 'span' in value:
            span = value['span']
            return {'span': {k: span[k] for k in ('traceId', 'spanId', 'parentSpanId', 'startTimeUnixNano', 'endTimeUnixNano', 'kind') if k in span}}
        if 'scores' in value and 'latencyMs' in value: return self.sample(value)
        return self.observation(value)


def policy_for(service, project, evaluation):
    # Collector gets the frozen policy through its Job environment, never caller JSON.
    if os.getenv('EVAL_COLLECTOR_LOCAL') == 'true':
        if evaluation != os.environ['EXPERIMENT_ID']:
            from fastapi import HTTPException
            raise HTTPException(403, 'Collector evaluation mismatch')
        return ContentPolicy.from_env()
    from evaluation_backend.storage import NotFound
    try:
        context = service.storage.get('run-contexts', project, evaluation)
        return ContentPolicy.model_validate(context['content_policy'])
    except NotFound:
        # Legacy runs created before run contexts follow deployment policy.
        return ContentPolicy.from_env()
