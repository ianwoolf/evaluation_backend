"""Deployment-owned Guardrail instance registry; credentials never leave the detail API."""
import hashlib,json,os
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,ConfigDict,Field,StrictBool,model_validator
from evaluation_backend.runs import authorize

class Instance(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id:str=Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
    address:str
    name:str=''
    enabled:bool=True
    verify_ssl:StrictBool=True
    token_env:str|None=Field(default=None,pattern=r'^[A-Za-z_][A-Za-z0-9_]*$')
    @model_validator(mode='after')
    def endpoint(self):
        p=urlsplit(self.address)
        if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or p.query or p.fragment:
            raise ValueError('Guardrail address must be an HTTP(S) endpoint without credentials, query or fragment')
        return self
class Provider(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id:str=Field(pattern=r'^(f5|nemo guardrail)$')
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
    try:return Registry.model_validate_json(Path(path).read_text(encoding='utf-8'))
    except (OSError,ValueError):raise RuntimeError('Invalid Guardrail instance registry configuration') from None

def registry():
    path=os.getenv('EVAL_GUARDRAIL_REGISTRY')
    if path:return read_file(path)
    # Compatibility for existing deployments; explicit registry takes precedence.
    item=dict(id=os.getenv('EVAL_MOCK_GUARDRAIL_ID','mock-guardrail'),name='Mock Guardrail',address=os.getenv('EVAL_MOCK_GUARDRAIL_URL','http://evaluation-mock-guardrail:8002/runtime/v1/endpoints/mock-guardrail/backend/v1/scans'),token_env='EVAL_MOCK_GUARDRAIL_TOKEN')
    return Registry(providers=[Provider(id=p,name=p,instances=[Instance(**item)]) for p in ('f5','nemo guardrail')])

def revision(value):return hashlib.sha256(value.model_dump_json().encode()).hexdigest()[:16]

def resolve(target):
    value=registry()
    for provider in value.providers:
        if provider.id!=target.get('provider'):continue
        for instance in provider.instances:
            if instance.id==(target.get('guardrail_id') or target['id']) and instance.enabled:
                return dict(provider=provider.id,id=instance.id,name=instance.name,address=instance.address,token_env=instance.token_env,verify_ssl=instance.verify_ssl,registry_revision=revision(value))
    raise HTTPException(422,'Selected Guardrail instance is unavailable for this provider; choose an enabled configured instance')

router=APIRouter(prefix='/api/v1/evaluations',dependencies=[Depends(authorize)])
@router.get('/guardrail-instances')
def catalog(provider:str|None=None):
    value=registry()
    return {'revision':revision(value),'providers':[{'id':p.id,'name':p.name,'instances':[i.model_dump(exclude={'token_env'}) for i in p.instances]} for p in value.providers if provider is None or p.id==provider]}



def credential(connection):
    key=connection.get('token_env')
    if not key:return ''
    value=os.getenv(key,'')
    if not value and os.getenv(key+'_FILE'):
        try:value=Path(os.environ[key+'_FILE']).read_text().rstrip('\r\n')
        except OSError:raise HTTPException(503,'Guardrail credential unavailable') from None
    if not value:raise HTTPException(503,'Guardrail credential unavailable')
    return value
