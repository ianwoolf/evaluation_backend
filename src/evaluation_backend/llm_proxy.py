"""Container-local OpenAI chat proxy; fixed runner model, streamed upstream, OTEL generations."""
import asyncio
import json
import os
import secrets
import ssl
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from evaluation_backend.config import load_config


def merge(chunks):
    """Reconstruct OpenAI ChatCompletion from SSE chunks, including tool calls."""
    result = {'object': 'chat.completion', 'choices': []}
    choices = {}
    for chunk in chunks:
        for key in ('id', 'created', 'model', 'system_fingerprint', 'usage'):
            if chunk.get(key) is not None:
                result[key] = chunk[key]
        for choice in chunk.get('choices', []):
            index = choice.get('index', 0)
            dst = choices.setdefault(index, {'index': index, 'message': {'role': 'assistant', 'content': ''}, 'finish_reason': None})
            if choice.get('finish_reason'):
                dst['finish_reason'] = choice['finish_reason']
            for key, value in choice.get('delta', {}).items():
                if key == 'tool_calls':
                    tools = dst['message'].setdefault('tool_calls', [])
                    for tool in value:
                        i = tool['index']
                        while len(tools) <= i:
                            tools.append({'type': 'function', 'function': {'name': '', 'arguments': ''}})
                        for k in ('id', 'type'):
                            if k in tool:
                                tools[i][k] = tool[k]
                        for k, v in tool.get('function', {}).items():
                            tools[i]['function'][k] = tools[i]['function'].get(k, '') + v
                elif isinstance(value, str) and key != 'role':
                    dst['message'][key] = dst['message'].get(key, '') + value
                elif value is not None:
                    dst['message'][key] = value
    result['choices'] = [choices[i] for i in sorted(choices)]
    return result


def generated(chunk):
    return any(any(d.get(k) for k in ('content', 'reasoning_content', 'tool_calls', 'function_call'))
               for c in chunk.get('choices', []) for d in [c.get('delta', {})])


def proxy_ssl_context(env):
    # Keep environment proxy auto-discovery disabled, but honor the runtime's CA bundle.
    return ssl.create_default_context(cafile=env.get('SSL_CERT_FILE') or env.get('REQUESTS_CA_BUNDLE') or None)


def create_proxy(env=None, client=None, telemetry=None):
    env = dict(os.environ if env is None else env)
    required = ('EVAL_MODEL_ID', 'EVAL_MODEL_RESOLVER_URL', 'EVAL_MODEL_RESOLVER_TOKEN', 'EVAL_LLM_PROXY_TOKEN')
    if any(not env.get(k) for k in required):
        raise ValueError('runner model, resolver URL/token and local proxy token are required')
    if telemetry is None:
        from evaluation_backend.telemetry import create_telemetry
        telemetry = create_telemetry(env)
    owned = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15), follow_redirects=False, trust_env=False,
                                           verify=proxy_ssl_context(env))

    @asynccontextmanager
    async def lifespan(app):
        yield
        if owned:
            await client.aclose()
        await asyncio.to_thread(telemetry.flush)

    app = FastAPI(lifespan=lifespan)

    @app.get('/health')
    def health():
        return {'status': 'ok'}

    @app.post('/v1/chat/completions')
    async def chat(request: Request, authorization: str = Header(default='')):
        if not secrets.compare_digest(authorization, 'Bearer ' + env['EVAL_LLM_PROXY_TOKEN']):
            raise HTTPException(401, 'unauthorized')
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get('messages'), list):
            raise HTTPException(400, 'messages required')
        stream = body.get('stream', False)
        if type(stream) is not bool:
            raise HTTPException(400, 'stream must be boolean')
        # Never forward caller headers, endpoint overrides or credentials.
        allowed = {'messages', 'temperature', 'top_p', 'max_tokens', 'max_completion_tokens', 'stop', 'seed', 'tools', 'tool_choice', 'parallel_tool_calls', 'response_format', 'presence_penalty', 'frequency_penalty', 'n', 'thinking'}
        payload = {k: v for k, v in body.items() if k in allowed}
        started = time.perf_counter()
        metadata = {'model_id': env['EVAL_MODEL_ID'], 'experiment_id': env.get('EXPERIMENT_ID') or env.get('EVALUATE_ID', env.get('EVALUATION_ID', '')), 'client_stream': stream, 'request_parameters': {k: v for k, v in payload.items() if k != 'messages'}}
        trace_context = None
        import re
        parent = re.fullmatch(r'00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}', request.headers.get('traceparent', ''))
        if parent and int(parent[1], 16) and int(parent[2], 16):
            trace_context = {'trace_id': parent[1], 'parent_span_id': parent[2]}
        span = telemetry.start_observation(name='runner.llm', as_type='generation', trace_context=trace_context, input=payload['messages'], metadata=metadata,
                                           model_parameters={k: v for k, v in payload.items() if k != 'messages' and isinstance(v, (str, bool, int, float))})
        upstream = None
        try:
            from urllib.parse import quote
            resolved = await client.get(env['EVAL_MODEL_RESOLVER_URL'].rstrip('/') + '/internal/models/' + quote(env['EVAL_MODEL_ID'], safe=''), headers={'Authorization': 'Bearer ' + env['EVAL_MODEL_RESOLVER_TOKEN']})
            resolved.raise_for_status()
            model = resolved.json()
            payload.update(model=model['model'], stream=True, stream_options={'include_usage': True})
            span.update(model=model['model'])
            upstream = await client.send(client.build_request('POST', model['address'].rstrip('/') + '/chat/completions', json=payload, headers={'Authorization': 'Bearer ' + model['token']}), stream=True)
            upstream.raise_for_status()
        except Exception:
            if upstream is not None:
                await upstream.aclose()
            span.update(level='ERROR', status_message='model resolution or upstream request failed', metadata={**metadata, 'duration_ms': (time.perf_counter()-started)*1000})
            span.end()
            raise HTTPException(502, 'LLM upstream unavailable') from None

        chunks = []
        async def events():
            first = None
            done = False
            failed = False
            try:
                async for line in upstream.aiter_lines():
                    if not line.startswith('data:'):
                        continue
                    data = line[5:].strip()
                    if data == '[DONE]':
                        done = True
                        yield 'data: [DONE]\n\n'
                        break
                    chunk = json.loads(data)
                    if chunk.get('error'):
                        raise ValueError('provider stream error')
                    if first is None and generated(chunk):
                        first = time.perf_counter()
                        span.update(completion_start_time=datetime.now(timezone.utc))
                    chunks.append(chunk)
                    yield 'data: ' + data + '\n\n'
                if not done:
                    raise ValueError('incomplete provider stream')
            except BaseException:
                failed = True
                span.update(level='ERROR', status_message='LLM stream interrupted')
                raise
            finally:
                await upstream.aclose()
                output = merge(chunks)
                usage = output.get('usage') or {}
                usage_details = {dest: usage[key] for key, dest in [('prompt_tokens', 'input'), ('completion_tokens', 'output'), ('total_tokens', 'total')] if isinstance(usage.get(key), int)}
                metadata.update(duration_ms=(time.perf_counter()-started)*1000, ttft_ms=(first-started)*1000 if first else None,
                                response_model=output.get('model'), usage_reported=bool(usage), interrupted=failed)
                for key in ('prompt_tokens_details', 'completion_tokens_details'):
                    if key in usage:
                        metadata[key] = usage[key]
                span.update(output=output.get('choices'), usage_details=usage_details, metadata=metadata)
                span.end()
        if stream:
            return StreamingResponse(events(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache'})
        try:
            async for _ in events():
                pass
        except Exception:
            raise HTTPException(502, 'LLM stream interrupted') from None
        return JSONResponse(merge(chunks))
    return app


def main():
    import uvicorn
    load_config()
    uvicorn.run(create_proxy(), host='127.0.0.1', port=int(os.getenv('EVAL_LLM_PROXY_PORT', '18090')))


if __name__ == '__main__':
    main()
