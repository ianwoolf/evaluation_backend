"""Single-user evaluation API configuration; no identity database or browser token."""
import os
from fastapi.responses import JSONResponse


class EvaluationBoundary:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not scope['path'].startswith('/api/v1/evaluations/'):
            return await self.app(scope, receive, send)
        headers = dict(scope['headers'])
        if os.getenv('EVAL_AUTH_MODE', 'token') == 'none':
            project = os.getenv('EVAL_DEFAULT_PROJECT_ID', 'individual')
            supplied = headers.get(b'x-project-id', b'').decode()
            if supplied and supplied != project:
                return await JSONResponse({'detail': 'Project does not match deployment configuration'}, status_code=400)(scope, receive, send)
            scope['headers'] = [(k, v) for k, v in scope['headers'] if k != b'x-project-id'] + [(b'x-project-id', project.encode())]
        if scope['method'] in ('POST', 'PUT', 'PATCH'):
            tail = scope['path'].removeprefix('/api/v1/evaluations/')
            limit = (8_000_000 if tail == 'uploads' or tail.startswith('datasets/') else
                     2_000_000 if tail.endswith('/samples') else 1_000_000 if tail == 'traces' else 40_000)
            chunks, size = [], 0
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                chunk = message.get('body', b'')
                size += len(chunk)
                if size > limit:
                    return await JSONResponse({'detail': 'Request too large'}, status_code=413)(scope, receive, send)
                chunks.append(chunk)
                if not message.get('more_body'):
                    break
            pending = True
            async def replay():
                nonlocal pending
                if pending:
                    pending = False
                    return {'type': 'http.request', 'body': b''.join(chunks), 'more_body': False}
                return await receive()
            return await self.app(scope, replay, send)
        return await self.app(scope, receive, send)
