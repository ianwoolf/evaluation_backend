"""Project-scoped storage HTTP service. Browser requests use the Control proxy."""
import json
import math
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from evaluation_backend.runs import authorize
from .service import DatasetDocument, get_service, now, public

router = APIRouter(prefix='/api/v1/evaluations', dependencies=[Depends(authorize)])


class TraceDocument(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    evaluation_id: str = Field(min_length=1, max_length=128)
    sample_id: str = ''
    created_at: str = Field(default_factory=now)
    name: str = Field(min_length=1, max_length=200)
    data: dict[str, Any]


class SampleDocument(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    input: dict[str, Any]
    reference: dict[str, Any]
    output: dict[str, Any]
    status: Literal['PASS', 'FAIL', 'ERROR']
    error: str | None = None
    latencyMs: float = Field(ge=0, allow_inf_nan=False)
    costUsd: float = Field(default=0, ge=0, allow_inf_nan=False)
    scores: list[dict[str, Any]] = Field(max_length=100)
    trace: list[dict[str, Any]] = Field(max_length=200)

    @model_validator(mode='after')
    def scores_valid(self):
        ids = []
        for score in self.scores:
            value = score.get('value')
            if (not isinstance(score.get('scorerId'), str) or not score['scorerId'] or
                type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1 or
                type(score.get('passed')) is not bool or not isinstance(score.get('reason'), str)):
                raise ValueError('Invalid score')
            ids.append(score['scorerId'])
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate sample scorer')
        return self


class ExperimentDocument(BaseModel):
    status: Literal['RUNNING', 'COMPLETED', 'FAILED']
    dataset_id: str
    dataset_version: int = Field(ge=1)
    items: int = Field(ge=0)
    errors: int = Field(ge=0)


@router.get('/datasets')
def list_datasets(x_project_id: str = Header(min_length=1), offset: int = Query(0, ge=0, le=9900),
                  limit: int = Query(100, ge=1, le=100), service=Depends(get_service)):
    return service.page('datasets', x_project_id, offset=offset, limit=limit)


@router.get('/datasets/{ident}')
def get_dataset(ident: str, x_project_id: str = Header(min_length=1), service=Depends(get_service)):
    return public(service.storage.get('datasets', x_project_id, ident))


@router.get('/datasets/{ident}/versions/{version}')
def get_version(ident: str, version: int, x_project_id: str = Header(min_length=1), service=Depends(get_service)):
    return service.dataset_version(x_project_id, ident, version)


@router.post('/datasets/{ident}')
def save_dataset(ident: str, value: DatasetDocument, x_project_id: str = Header(min_length=1), service=Depends(get_service)):
    if value.id != ident:
        raise HTTPException(422, 'Dataset ID mismatch')
    if len(value.model_dump_json().encode()) > 8_000_000:
        raise HTTPException(413, 'Dataset exceeds 8 MB')
    return service.save_dataset(x_project_id, value)


@router.post('/traces')
def save_trace(value: TraceDocument, x_project_id: str = Header(min_length=1), service=Depends(get_service)):
    if len(value.model_dump_json().encode()) > 1_000_000:
        raise HTTPException(413, 'Trace exceeds 1 MB')
    return public(service.storage.put('traces', x_project_id, value.id, value.model_dump()))


@router.get('/traces')
def traces(evaluation_id: str, x_project_id: str = Header(min_length=1), offset: int = Query(0, ge=0, le=9900),
           limit: int = Query(100, ge=1, le=100), service=Depends(get_service)):
    return service.page('traces', x_project_id, filters={'evaluation_id': evaluation_id}, offset=offset, limit=limit)


@router.get('/scores')
def scores(evaluation_id: str, x_project_id: str = Header(min_length=1), offset: int = Query(0, ge=0, le=9900),
           limit: int = Query(100, ge=1, le=100), service=Depends(get_service)):
    return service.page('scores', x_project_id, filters={'evaluation_id': evaluation_id}, offset=offset, limit=limit)


@router.post('/experiments/{ident}/samples')
def save_sample(ident: str, value: SampleDocument, x_project_id: str = Header(min_length=1), service=Depends(get_service)):
    if len(value.model_dump_json().encode()) > 2_000_000:
        raise HTTPException(413, 'Sample exceeds 2 MB')
    # Deterministic IDs make a retry after an interrupted HTTP response safe.
    stamp = now()
    sample = {**value.model_dump(), 'evaluation_id': ident, 'sample_id': value.id, 'created_at': stamp}
    for score in value.scores:
        score_id = json.dumps([ident, value.id, score['scorerId']])
        service.storage.put('scores', x_project_id, score_id, {**score, 'id': score_id,
            'evaluation_id': ident, 'sample_id': value.id, 'scorer_id': score['scorerId'], 'created_at': stamp})
    trace_id = json.dumps([ident, value.id])
    service.storage.put('traces', x_project_id, trace_id, {'id': trace_id, 'evaluation_id': ident,
        'sample_id': value.id, 'created_at': stamp, 'name': 'evaluation.sample', 'data': sample})
    # The sample is the commit marker, written after its trace and scores.
    return public(service.storage.put('samples', x_project_id, trace_id, sample))


@router.get('/experiments/{ident}/samples')
def samples(ident: str, x_project_id: str = Header(min_length=1), offset: int = Query(0, ge=0, le=9900),
            limit: int = Query(100, ge=1, le=100), service=Depends(get_service)):
    return service.page('samples', x_project_id, filters={'evaluation_id': ident}, offset=offset, limit=limit)


@router.post('/experiments/{ident}')
def save_experiment(ident: str, value: ExperimentDocument, x_project_id: str = Header(min_length=1), service=Depends(get_service)):
    return public(service.storage.put('experiments', x_project_id, ident,
        {**value.model_dump(), 'id': ident, 'evaluation_id': ident, 'created_at': now()}))


@router.get('/experiments/{ident}')
def get_experiment(ident: str, x_project_id: str = Header(min_length=1), service=Depends(get_service)):
    return public(service.storage.get('experiments', x_project_id, ident))


@router.get('/reports/{ident}')
def report(ident: str, x_project_id: str = Header(min_length=1), offset: int = Query(0, ge=0, le=9900),
           limit: int = Query(100, ge=1, le=100), service=Depends(get_service)):
    """Read by experiment/evaluation ID, not Kubernetes Job name. Results may be partial."""
    experiment = public(service.storage.get('experiments', x_project_id, ident))
    samples = service.page('samples', x_project_id, filters={'evaluation_id': ident}, offset=offset, limit=limit)
    return {'evaluation_id': ident, 'status': experiment['status'], 'experiment': experiment,
            'results': samples['items'], 'total': samples['total'], 'offset': offset, 'limit': limit,
            'partial': experiment['status'] == 'RUNNING',
            'nextOffset': offset + limit if offset + limit < samples['total'] else None}
