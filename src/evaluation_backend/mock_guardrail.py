"""Explicit mock Scan endpoint for local integration; never reads dataset labels."""
import hmac
import os
from uuid import uuid4
from typing import Literal
from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, StrictBool, Field

app = FastAPI(title='Mock Guardrail Scan')


@app.get('/health')
def health():
    return {'status': 'ok', 'source': 'mock'}


@app.get('/ready')
def ready():
    if not os.getenv('EVAL_MOCK_GUARDRAIL_TOKEN'):
        raise HTTPException(503, 'Mock credential is not configured')
    return {'status': 'ready', 'source': 'mock'}


class Scan(BaseModel):
    model_config = ConfigDict(extra='ignore')
    input: str = Field(min_length=1, max_length=100000)
    scanDirection: Literal['request', 'response'] = 'request'
    flagOnly: StrictBool = True
    verbose: StrictBool = False


@app.post('/runtime/v1/endpoints/{endpoint_id}/backend/v1/scans')
def scan(endpoint_id: str, body: Scan, response: Response, authorization: str = Header(default='')):
    token = os.getenv('EVAL_MOCK_GUARDRAIL_TOKEN', '')
    if not token:
        raise HTTPException(503, 'Mock credential is not configured')
    if endpoint_id != os.getenv('EVAL_MOCK_GUARDRAIL_ID', 'mock-guardrail') or not hmac.compare_digest(authorization, 'Bearer '+token):
        raise HTTPException(401, 'Invalid endpoint credential', headers={'WWW-Authenticate': 'Bearer'})
    ident = str(uuid4())
    response.headers['X-Request-ID'] = ident
    response.headers['X-TaskLattice-Scan-Compatibility'] = 'v1'
    response.headers['X-Evaluation-Source'] = 'mock'
    # Deliberate fixture markers provide repeatable cases independently of labels.
    if '[mock:timeout]' in body.input:
        raise HTTPException(504, {'code': 'guardrail_timeout', 'message': 'Mock timeout', 'requestId': ident})
    if '[mock:error]' in body.input:
        raise HTTPException(503, {'code': 'guardrail_execution_failed', 'message': 'Mock failure', 'requestId': ident})
    hit = '[mock:unsafe]' in body.input
    outcome = ('flagged' if body.flagOnly else 'blocked') if hit else 'cleared'
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    details = [{'outcome': 'failed' if hit else 'passed', 'data': {'type': 'custom'},
                'startedDate': now, 'completedDate': now, 'scanDirection': body.scanDirection,
                'scannerId': None, 'scannerVersionMeta': None, 'message': None, 'customConfig': False}] if body.verbose else []
    return {'id': ident, 'result': {'outcome': outcome, 'response': None, 'scannerResults': details},
            'redactedInput': body.input, 'scanners': None}
