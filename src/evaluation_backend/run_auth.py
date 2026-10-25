"""Short-lived, project/run-bound worker capability. No global trigger secret in Jobs."""
import base64
import hashlib
import hmac
import json
import os
import time
from fastapi import HTTPException


def issue_worker_token(project, evaluation):
    value=base64.urlsafe_b64encode(json.dumps({'project':project,'evaluation':evaluation,'expires':int(time.time())+3600},sort_keys=True).encode()).decode().rstrip('=')
    sig=hmac.new(os.environ['EVAL_TRIGGER_TOKEN'].encode(),value.encode(),hashlib.sha256).hexdigest()
    return value+'.'+sig


def verify_worker_token(token):
    try:
        value,sig=token.split('.')
        expected=hmac.new(os.environ['EVAL_TRIGGER_TOKEN'].encode(),value.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected,sig):raise ValueError()
        claims=json.loads(base64.urlsafe_b64decode(value+'='*(-len(value)%4)))
        if claims['expires']<=time.time():raise ValueError()
        return claims
    except Exception:raise HTTPException(401,'Unauthorized') from None


async def worker_boundary(request, claims):
    if request.headers.get('x-project-id')!=claims['project']:raise HTTPException(403,'Project mismatch')
    path=request.url.path
    root='/api/v1/evaluations/'
    exp=claims['evaluation']
    allowed=(request.method=='GET' and path==root+'run-input/'+exp) or (request.method=='POST' and path in (root+'experiments/'+exp,root+'experiments/'+exp+'/samples'))
    if request.method=='POST' and path==root+'traces':
        allowed=(await request.json()).get('evaluation_id')==exp
    if not allowed:raise HTTPException(403,'Worker capability does not allow this operation')
