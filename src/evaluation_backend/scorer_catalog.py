"""Versioned scoring specifications. Registration does not install executable code."""
from typing import Literal
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from evaluation_backend.runs import authorize
from evaluation_backend.storage import NotFound, Conflict
from evaluation_backend.storage.service import get_service, public

class Criteria(BaseModel):
    model_config = ConfigDict(extra='forbid')
    authoringMode: Literal['criteria', 'steps'] = 'criteria'
    evaluationSteps: str = Field(default='', max_length=16000)
    aspect: Literal['completion', 'grounding', 'friendliness', 'tool-use', 'safety', 'custom'] = 'custom'
    subject: str = Field(default='', max_length=2000)
    expectation: str = Field(default='', max_length=16000)
    requirements: str = Field(default='', max_length=16000)
    evidence: str = Field(default='', max_length=4000)

class ScorerConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default='', max_length=16000)
    kind: Literal['LLM_JUDGE', 'CODE', 'RULE']
    definition: str = Field(default='', max_length=32000)
    repoUrl: str | None = None
    scriptPath: str | None = None
    threshold: float = Field(ge=0, le=1, allow_inf_nan=False)
    notes: str = Field(default='', max_length=4000)
    criteria: Criteria | None = None
    execution: dict | None = None

    @model_validator(mode="after")
    def validate_engine(self):
        if self.execution is not None:
            from evaluation_backend.executable_scorers import validate_execution
            self.execution=validate_execution(self.execution)
            if self.kind != {"rule":"RULE","llm_judge":"LLM_JUDGE"}[self.execution["engine"]]:raise ValueError("Engine/type mismatch")
        return self

class Version(ScorerConfig):
    version: int = Field(ge=1)
    publishedAt: str = Field(min_length=1, max_length=100)

class Document(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
    source: Literal['CUSTOM'] = 'CUSTOM'
    enabled: bool = True
    draft: ScorerConfig
    versions: list[Version] = Field(default_factory=list, max_length=100)
    revision: str | None = None

    @model_validator(mode='after')
    def ordered(self):
        if [v.version for v in self.versions] != list(range(1, len(self.versions)+1)):
            raise ValueError('Versions must be contiguous')
        for v in self.versions[-1:]:
            if not v.criteria or not all(s.strip() for s in (v.criteria.subject, v.criteria.expectation, v.criteria.requirements, v.criteria.evidence)):
                raise ValueError('Published scorers require subject, expectation, requirements and evidence')
        return self

router = APIRouter(prefix='/api/v1/evaluations/scorers', dependencies=[Depends(authorize)])

@router.get('')
def listing(x_project_id: str = Header(min_length=1), offset: int = Query(0, ge=0, le=9900), limit: int = Query(100, ge=1, le=100), service=Depends(get_service)):
    return service.page('scorers', x_project_id, offset=offset, limit=limit)

@router.get('/{ident}')
def get(ident: str, x_project_id: str = Header(min_length=1), service=Depends(get_service)):
    return public(service.storage.get('scorers', x_project_id, ident))

@router.post('/{ident}')
def save(ident: str, value: Document, x_project_id: str = Header(min_length=1), service=Depends(get_service)):
    if ident != value.id: raise HTTPException(422, 'Scorer ID mismatch')
    if len(value.model_dump_json().encode()) > 1_000_000: raise HTTPException(413, 'Scorer exceeds 1 MB')
    data = value.model_dump(mode='json', exclude_none=True, exclude_unset=True, exclude={'revision'})
    try: current = service.storage.get('scorers', x_project_id, ident)
    except NotFound:
        return public(service.storage.put('scorers', x_project_id, ident, data, create=True))
    if value.revision != current['_revision']: raise Conflict('Scorer changed; reload before saving')
    old = current['versions']
    if data['versions'][:len(old)] != old or len(data['versions']) > len(old)+1:
        raise Conflict('Published versions are immutable; append one at a time')
    return public(service.storage.put('scorers', x_project_id, ident, data, revision=value.revision))

class TestRequest(BaseModel):
    config: ScorerConfig
    evidence: dict

@router.post('/actions/validate')
def validate(value: ScorerConfig):
    if not value.execution:raise HTTPException(422,'Configure an execution engine')
    return {'valid':True,'execution':value.execution}

@router.post('/actions/test')
def test(value: TestRequest):
    import time
    from evaluation_backend.executable_scorers import evaluate_execution
    from evaluation_backend.capabilities import scoring_enabled
    if len(value.model_dump_json().encode())>256_000:raise HTTPException(413,'Test evidence exceeds 256 KB')
    if not value.config.execution:raise HTTPException(422,'Configure an execution engine')
    if not scoring_enabled():return {'source':'mock','score':.5,'passed':.5>=value.config.threshold,'reason':'Demo score; no engine or model called'}
    started=time.perf_counter()
    try:score=evaluate_execution(value.config.execution,value.evidence)
    except ValueError as error:raise HTTPException(422,str(error)) from None
    except Exception:raise HTTPException(502,'Scorer execution failed; check runner/API logs') from None
    return {'source':'real','score':score.value,'passed':score.value>=value.config.threshold,'reason':score.reason,'duration_ms':round((time.perf_counter()-started)*1000), 'engine':value.config.execution['engine']}
