"""Explicit mock Scan endpoint for local integration; never reads dataset labels."""
import hmac
import random
import os
import json
import re
import time
from datetime import datetime, timezone
from uuid import uuid4
from typing import Literal
from fastapi import FastAPI, Header, HTTPException, Response, Request
from pydantic import BaseModel, ConfigDict, StrictBool, Field

app = FastAPI(title='Mock Guardrail Scan')


def request_log(event, level='INFO', **fields):
    print(json.dumps({'timestamp':datetime.now(timezone.utc).isoformat(),'level':level,
        'service':'evaluation-mock-guardrail','event':event,**fields},ensure_ascii=True),flush=True)


@app.middleware('http')
async def log_request(request: Request, call_next):
    if request.url.path.rstrip('/') in ('/health','/ready'):
        return await call_next(request)
    supplied=request.headers.get('X-Request-ID','')
    ident=supplied if re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',supplied) else str(uuid4())
    request.state.request_id=ident
    fields={'request_id':ident,'method':request.method,'path':request.url.path[:256]}
    parent=request.headers.get('traceparent','')
    if re.fullmatch(r'[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}',parent):
        fields.update(trace_id=parent.split('-')[1],parent_span_id=parent.split('-')[2])
    started=time.perf_counter()
    request_log('guardrail.request.started',**fields)
    try:
        response=await call_next(request)
    except Exception as error:
        request_log('guardrail.request.failed',level='ERROR',**fields,status_code=500,
            duration_ms=round((time.perf_counter()-started)*1000,2),error_type=type(error).__name__)
        raise
    response.headers['X-Request-ID']=ident
    extra=getattr(request.state,'scan_log',{})
    request_log('guardrail.request.completed' if response.status_code<400 else 'guardrail.request.failed',
        level='INFO' if response.status_code<400 else 'ERROR' if response.status_code>=500 else 'WARNING',
        **fields,**extra,status_code=response.status_code,duration_ms=round((time.perf_counter()-started)*1000,2))
    return response


@app.get('/health')
def health():
    return {'status': 'ok', 'source': 'mock'}


def shared_token():
    return os.getenv('EVAL_GUARDRAIL_TOKEN') or os.getenv('EVAL_MOCK_GUARDRAIL_TOKEN', '')


@app.get('/ready')
def ready():
    if not shared_token():
        raise HTTPException(503, 'Mock credential is not configured')
    return {'status': 'ready', 'source': 'mock'}


class Scan(BaseModel):
    model_config = ConfigDict(extra='ignore')
    input: str = Field(min_length=1, max_length=100000)
    scanDirection: Literal['request', 'response'] = 'request'
    flagOnly: StrictBool = True
    verbose: StrictBool = False


@app.post('/runtime/v1/endpoints/{endpoint_id}/backend/v1/scans')
def scan(endpoint_id: str, body: Scan, response: Response, request: Request, authorization: str = Header(default='')):
    request.state.scan_log={'endpoint_id':endpoint_id,'input_chars':len(body.input),'scan_direction':body.scanDirection,'flag_only':body.flagOnly}
    token = shared_token()
    if not token:
        raise HTTPException(503, 'Mock credential is not configured')
    if endpoint_id != os.getenv('EVAL_MOCK_GUARDRAIL_ID', 'mock-guardrail') or not hmac.compare_digest(authorization, 'Bearer '+token):
        raise HTTPException(401, 'Invalid endpoint credential', headers={'WWW-Authenticate': 'Bearer'})
    ident = request.state.request_id
    response.headers['X-Request-ID'] = ident
    response.headers['X-TaskLattice-Scan-Compatibility'] = 'v1'
    response.headers['X-Evaluation-Source'] = 'mock'
    hit = random.random() < 0.5
    outcome = ('flagged' if body.flagOnly else 'blocked') if hit else 'cleared'
    request.state.scan_log['outcome']=outcome
    now = datetime.now(timezone.utc).isoformat()
    details = [{'outcome': 'failed' if hit else 'passed', 'data': {'type': 'custom'},
                'startedDate': now, 'completedDate': now, 'scanDirection': body.scanDirection,
                'scannerId': None, 'scannerVersionMeta': None, 'message': None, 'customConfig': False}] if body.verbose else []
    return {'id': ident, 'result': {'outcome': outcome, 'response': None, 'scannerResults': details},
            'redactedInput': body.input, 'scanners': None}
