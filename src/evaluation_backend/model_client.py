"""Model invocation port; task prompts and output parsing belong to targets."""
from evaluation_backend.observability import observed
import json
import os
from typing import Protocol
from urllib.request import Request, urlopen


class ModelClient(Protocol):
    @observed('model.invoke')
    def complete(self, messages: list[dict], *, options: dict | None = None) -> str: ...


class OpenAICompatibleClient:
    @observed('model.invoke')
    def complete(self, messages, *, options=None):
        payload = {**(options or {}), 'model': os.environ['EVAL_MODEL_ID'], 'messages': messages}
        request = Request(os.environ['OPENAI_BASE_URL'].rstrip('/') + '/chat/completions',
            data=json.dumps(payload).encode(), headers={
                'Authorization': 'Bearer ' + os.environ['OPENAI_API_KEY'], 'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=120) as response:
                return json.load(response)['choices'][0]['message']['content']
        except Exception as error:
            from evaluation_backend.observability import event
            event('model.transport.failed', level='ERROR', error=error, model_id=os.getenv('EVAL_MODEL_ID'))
            raise RuntimeError('Model invocation failed') from None


def create_model_client() -> ModelClient:
    adapter = os.getenv('EVAL_MODEL_ADAPTER', 'openai-compatible')
    if adapter == 'mock':
        return MockModelClient()
    if adapter != 'openai-compatible':
        raise ValueError('Unknown model adapter: ' + adapter)
    return OpenAICompatibleClient()


class MockModelClient:
    """Explicit integration-test adapter; never selected automatically."""
    @observed('model.invoke')
    def complete(self, messages, *, options=None):
        from evaluation_backend.telemetry import create_telemetry
        span = create_telemetry().start_observation(name='mock.model', input=messages, metadata={'mock': True})
        output = '{"has_issue": false}'
        span.update(output=output)
        span.end()
        return output
