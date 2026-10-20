"""Authenticated model resolution. Secrets never appear in public model lists."""
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlparse
from fastapi import APIRouter, Header, HTTPException, Response

router = APIRouter()


@router.get('/internal/models/{model_id}', include_in_schema=False)
def resolve_model(model_id: str, response: Response, authorization: str = Header(default='')):
    response.headers['Cache-Control'] = 'no-store'
    token = os.getenv('EVAL_MODEL_RESOLVER_TOKEN', '')
    if not token or not secrets.compare_digest(authorization, 'Bearer ' + token):
        raise HTTPException(401, 'unauthorized')
    try:
        record = json.loads(Path(os.environ['EVAL_MODEL_REGISTRY']).read_text())[model_id]
        url = urlparse(record['address'])
        if url.scheme not in {'http', 'https'} or not url.netloc or url.username or url.password or url.query or url.fragment:
            raise ValueError()
        if not record.get('model') or not record.get('token'):
            raise ValueError()
        return {key: record[key] for key in ('address', 'model', 'token')}
    except KeyError:
        raise HTTPException(404, 'model not found') from None
    except Exception:
        raise HTTPException(503, 'model configuration unavailable') from None
