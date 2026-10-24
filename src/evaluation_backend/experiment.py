"""Vendor-neutral experiment contracts and environment configuration."""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class Settings:
    dataset_id: str
    evaluation_id: str
    scorer_ids: list[str]
    dataset_version: datetime
    platform: str = 'storage'
    concurrency: int = 1

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        def required(name):
            value = env.get(name, '').strip()
            if not value:
                raise ValueError(f'{name} is required')
            return value
        ids = json.loads(required('SCORER_IDS'))
        if not isinstance(ids, list) or not ids or not all(isinstance(x, str) and x.strip() for x in ids) or len(set(ids)) != len(ids):
            raise ValueError('SCORER_IDS must be a nonempty unique JSON string array')
        evaluation_id = env.get('EXPERIMENT_ID') or env.get('EVALUATE_ID') or env.get('EVALUATION_ID')
        if not evaluation_id or not evaluation_id.strip():
            raise ValueError('EVALUATE_ID is required')
        if env.get('EVALUATE_ID') and env.get('EVALUATION_ID') and env['EVALUATE_ID'] != env['EVALUATION_ID']:
            raise ValueError('conflicting evaluation IDs')
        version = datetime.fromisoformat(env.get('DATASET_VERSION', datetime.now(timezone.utc).isoformat()).replace('Z', '+00:00'))
        if version.tzinfo is None:
            raise ValueError('DATASET_VERSION must include a timezone')
        concurrency = int(env.get('EVAL_CONCURRENCY', '1'))
        if not 1 <= concurrency <= 32:
            raise ValueError('EVAL_CONCURRENCY must be 1..32')
        return cls(required('DATASET_ID'), evaluation_id.strip(), ids, version.astimezone(timezone.utc), env.get('EVAL_PLATFORM', 'storage'), concurrency)


@dataclass(frozen=True)
class Dataset:
    id: str
    name: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class Score:
    value: float
    reason: str

    def __post_init__(self):
        if isinstance(self.value, bool) or not math.isfinite(self.value) or not 0 <= self.value <= 1:
            raise ValueError('score must be finite and in [0,1]')


@dataclass(frozen=True)
class Scorer:
    id: str
    version: str
    threshold: float
    evaluate: Callable[..., Score]


class DatasetSource(Protocol):
    def get_dataset(self, settings: Settings) -> Dataset: ...


class ScorerLibrary(Protocol):
    def get_scorers(self, dataset: Dataset, ids: list[str]) -> list[Scorer]: ...


class ExperimentReporter(Protocol):
    def execute(self, settings: Settings, dataset: Dataset, target: Callable, scorers: list[Scorer]) -> dict: ...


def run_experiment(settings: Settings, source: DatasetSource, library: ScorerLibrary, reporter: ExperimentReporter, target: Callable) -> dict:
    dataset = source.get_dataset(settings)
    scorers = library.get_scorers(dataset, settings.scorer_ids)
    if [x.id for x in scorers] != settings.scorer_ids:
        raise ValueError('scorer library returned unexpected scorers')
    return reporter.execute(settings, dataset, target, scorers)
