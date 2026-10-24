"""Versioned dataset and result service; independent of Elasticsearch."""
from copy import deepcopy
from functools import lru_cache
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import Conflict, NotFound, Storage


class DatasetField(BaseModel):
    name: str = Field(min_length=1)
    type: Literal['string', 'number', 'boolean', 'json']
    role: Literal['input', 'reference', 'metadata']
    required: bool
    description: str = ''


class DatasetContent(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    description: str = ''
    tags: list[str] = Field(default_factory=list)
    scorerIds: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    fields: list[DatasetField] = Field(min_length=1)
    rows: list[dict[str, Any]] = Field(default_factory=list, max_length=10000)

    @model_validator(mode='after')
    def validate_fields(self):
        names = [f.name for f in self.fields]
        if len(names) != len(set(names)) or not any(f.role == 'input' for f in self.fields):
            raise ValueError('Dataset requires unique field names and at least one input field')
        for row in self.rows:
            for field in self.fields:
                if field.required and (field.name not in row or row[field.name] is None):
                    raise ValueError('Missing required dataset field: ' + field.name)
        return self


class DatasetVersion(DatasetContent):
    version: int = Field(ge=1)
    publishedAt: datetime


class DatasetDocument(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
    draft: DatasetContent
    versions: list[DatasetVersion] = Field(default_factory=list)
    revision: str | None = None

    @model_validator(mode='after')
    def ordered_versions(self):
        if [v.version for v in self.versions] != list(range(1, len(self.versions) + 1)):
            raise ValueError('Dataset versions must be contiguous and ordered')
        if any(not v.rows for v in self.versions):
            raise ValueError('Published datasets require records')
        return self


def public(value):
    value = deepcopy(value)
    revision = value.pop('_revision', None)
    if revision is not None:
        value['revision'] = revision
    return value


class EvaluationStorageService:
    def __init__(self, storage: Storage):
        self.storage = storage

    def save_dataset(self, project, dataset: DatasetDocument):
        value = dataset.model_dump(mode='json', exclude={'revision'})
        try:
            current = self.storage.get('datasets', project, dataset.id)
        except NotFound:
            return public(self.storage.put('datasets', project, dataset.id, value, create=True))
        if dataset.revision != current['_revision']:
            raise Conflict('Dataset changed; reload before saving')
        old = current['versions']
        if value['versions'][:len(old)] != old or len(value['versions']) > len(old) + 1:
            raise Conflict('Published versions are immutable; append one new version at a time')
        return public(self.storage.put('datasets', project, dataset.id, value, revision=dataset.revision))

    def dataset_version(self, project, ident, version):
        dataset = self.storage.get('datasets', project, ident)
        for item in dataset['versions']:
            if item['version'] == version:
                return item
        raise NotFound('Dataset version not found')

    def page(self, collection, project, **kwargs):
        result = self.storage.search(collection, project, **kwargs)
        return {**result, 'items': [public(v) for v in result['items']]}


@lru_cache(maxsize=1)
def get_service():
    import os
    from .factory import create_storage
    return EvaluationStorageService(create_storage(os.getenv('EVAL_STORAGE_BACKEND', 'elasticsearch')))


def now():
    return datetime.now(timezone.utc).isoformat()
