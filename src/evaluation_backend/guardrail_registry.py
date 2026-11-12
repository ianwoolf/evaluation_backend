"""Project Guardrail registry with deployment defaults and one shared credential."""
import hashlib,json,os
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import APIRouter,Depends,Header,HTTPException
from pydantic import BaseModel,ConfigDict,Field,StrictBool,model_validator
from evaluation_backend.guardrail_contracts import ProviderId
from evaluation_backend.runs import authorize
from evaluation_backend.storage import NotFound
from evaluation_backend.storage.service import get_service

class Instance(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id:str=Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
    address:str
    name:str=''
    enabled:bool=True
    verify_ssl:StrictBool=True
    @model_validator(mode='after')
    def endpoint(self):
        p=urlsplit(self.address)
        if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or p.query or p.fragment:
            raise ValueError('Guardrail address must be an HTTP(S) endpoint without credentials, query or fragment')
        return self
class Provider(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id: ProviderId
    name:str
    instances:list[Instance]
    @model_validator(mode='after')
    def unique(self):
        if len({i.id for i in self.instances})!=len(self.instances):raise ValueError('Duplicate Guardrail ID within provider')
        return self
class Registry(BaseModel):
    model_config=ConfigDict(extra='forbid')
    version:int=Field(default=1,ge=1)
    providers:list[Provider]
    @model_validator(mode='after')
    def unique(self):
        if len({p.id for p in self.providers})!=len(self.providers):raise ValueError('Duplicate provider ID')
        return self
@lru_cache
def read_file(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        for provider in data.get('providers', []):
            for instance in provider.get('instances', []):
                instance.pop('token_env', None)
        return Registry.model_validate(data)
    except (OSError,ValueError):raise RuntimeError('Invalid Guardrail instance registry configuration') from None

def deployment_registry():
    path=os.getenv('EVAL_GUARDRAIL_REGISTRY')
    if path:return read_file(path)
    # Compatibility for existing deployments; explicit registry takes precedence.
    item=dict(id=os.getenv('EVAL_MOCK_GUARDRAIL_ID','mock-guardrail'),name='Mock Guardrail',address=os.getenv('EVAL_MOCK_GUARDRAIL_URL','http://evaluation-mock-guardrail:8002/runtime/v1/endpoints/mock-guardrail/backend/v1/scans'))
    return Registry(providers=[Provider(id=p,name=p,instances=[Instance(**item)]) for p in ('f5','nemo guardrail')])

def revision(value):return hashlib.sha256(value.model_dump_json().encode()).hexdigest()[:16]

REGISTRY_COLLECTION = 'guardrail-registries'
REGISTRY_ID = 'default'


def stored_registry(service, project):
    if service is not None:
        try:
            record = service.storage.get(REGISTRY_COLLECTION, project, REGISTRY_ID)
        except NotFound:
            pass
        else:
            return Registry.model_validate(record['registry']), record['_revision']
    value = deployment_registry()
    return value, 'bootstrap:' + revision(value)


def registry(service=None, project=None):
    return stored_registry(service, project)[0]


def resolve(target, service=None, project=None):
    value=registry(service, project)
    for provider in value.providers:
        if provider.id!=target.get('provider'):continue
        for instance in provider.instances:
            if instance.id==(target.get('guardrail_id') or target['id']) and instance.enabled:
                return dict(provider=provider.id,id=instance.id,name=instance.name,address=instance.address,verify_ssl=instance.verify_ssl,registry_revision=revision(value))
    raise HTTPException(422,'Selected Guardrail instance is unavailable for this provider; choose an enabled configured instance')

router=APIRouter(prefix='/api/v1/evaluations',dependencies=[Depends(authorize)])
@router.get('/guardrail-instances')
def catalog(provider: str | None = None, x_project_id: str = Header(min_length=1),
            service=Depends(get_service)):
    value = registry(service, x_project_id)
    return {'revision': revision(value), 'providers': [p.model_dump() for p in value.providers
                                                        if provider is None or p.id == provider]}


def credential():
    # Support the old deployment key during upgrades. Instance settings never select credentials.
    for key in ('EVAL_GUARDRAIL_TOKEN', 'EVAL_MOCK_GUARDRAIL_TOKEN'):
        if value := os.getenv(key):
            return value
        if path := os.getenv(key + '_FILE'):
            try:
                value = Path(path).read_text().rstrip('\r\n')
            except OSError:
                raise HTTPException(503, 'Guardrail credential unavailable') from None
            if value:
                return value
    raise HTTPException(503, 'Guardrail credential unavailable')
