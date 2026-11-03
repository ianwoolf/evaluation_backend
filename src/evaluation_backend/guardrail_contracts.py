"""Guardrail-specific contracts; generic datasets retain their existing schema."""
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Label = Literal['safe', 'unsafe']


class StrictContract(BaseModel):
    model_config = ConfigDict(extra='forbid')


class GuardrailDatasetConfig(StrictContract):
    label_mapping: dict[str, Label] = Field(default_factory=lambda: {'safe': 'safe', 'unsafe': 'unsafe'})
    scorer_id: str = Field(min_length=1, max_length=128)
    scorer_version: int = Field(ge=1, strict=True)
    aspect_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
    aspect_name: str = Field(min_length=1, max_length=200)

    def normalize_label(self, value: Any) -> Label:
        # bool is not an integer label. Numeric labels are integral only.
        if type(value) not in (str, int):
            raise ValueError('Guardrail label must be a string or integer with an explicit mapping')
        key = str(value)
        if key not in self.label_mapping:
            raise ValueError('Guardrail label is not present in label_mapping')
        return self.label_mapping[key]


def validate_guardrail_rows(fields, rows, config):
    expected = {'input': 'input', 'label': 'reference', 'strategy': 'metadata'}
    if {f.name: f.role for f in fields} != expected:
        raise ValueError('Guardrail datasets require input, label, strategy with input/reference/metadata roles')
    for row in rows:
        if set(row) != set(expected):
            raise ValueError('Guardrail rows require exactly input, label, strategy')
        value = row['input']
        if not ((isinstance(value, str) and value.strip()) or (isinstance(value, dict) and value)):
            raise ValueError('Guardrail input must be a nonempty string or object')
        if config is not None:
            config.normalize_label(row['label'])
        elif type(row['label']) not in (str, int):
            raise ValueError('Guardrail label must be a string or integer')
        if not isinstance(row['strategy'], str):
            raise ValueError('Guardrail strategy must be a string')


class GuardrailBinding(StrictContract):
    binding_id: str = Field(min_length=1, max_length=128)
    dataset_id: str = Field(min_length=1, max_length=128)
    dataset_version: int = Field(ge=1, strict=True)
    scorer_id: str = Field(min_length=1, max_length=128)
    scorer_version: int = Field(ge=1, strict=True)
    aspect_id: str = Field(min_length=1, max_length=128)
    aspect_name: str = Field(min_length=1, max_length=200)
    scorer_commit: str = Field(pattern=r'^[0-9a-f]{40}$')
    artifact_digest: str = Field(pattern=r'^sha256:[0-9a-f]{64}$')


class ClassificationResult(StrictContract):
    kind: Literal['binary_classification'] = 'binary_classification'
    binding_id: str = Field(min_length=1, max_length=128)
    expected: Label
    predicted: Label
    outcome: Literal['TP', 'FP', 'TN', 'FN']
    reason: str = Field(max_length=8000)
    status: Literal['completed'] = 'completed'
    source: Literal['real', 'mock'] = 'real'

    @model_validator(mode='after')
    def check_outcome(self):
        expected = {('unsafe', 'unsafe'): 'TP', ('safe', 'unsafe'): 'FP',
                    ('safe', 'safe'): 'TN', ('unsafe', 'safe'): 'FN'}[self.expected, self.predicted]
        if self.outcome != expected:
            raise ValueError('Classification outcome disagrees with expected/predicted labels')
        return self


class RequestCost(StrictContract):
    amount: float = Field(default=0, ge=0, allow_inf_nan=False)
    currency: Literal['USD'] = 'USD'
    source: Literal['mock'] = 'mock'
    policy_version: str = Field(default='mock-v1', min_length=1, max_length=128)


class GuardrailRequestResult(StrictContract):
    request_id: str = Field(min_length=1, max_length=128)
    binding_ids: list[str] = Field(min_length=1, max_length=100)
    row_id: str = Field(min_length=1, max_length=128)
    strategy: str
    label: Label
    status: Literal['COMPLETED', 'ERROR']
    guardrail_latency_ms: float = Field(ge=0, allow_inf_nan=False)
    scorer_latency_ms: float = Field(ge=0, allow_inf_nan=False)
    total_latency_ms: float = Field(ge=0, allow_inf_nan=False)
    http_status: int | None = Field(default=None, ge=100, le=599)
    sent: bool = False
    error_stage: Literal['target', 'response', 'scorer', 'collector'] | None = None
    error_code: str | None = Field(default=None, max_length=128)
    cost: RequestCost = Field(default_factory=RequestCost)
    classifications: list[ClassificationResult] = Field(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def consistent(self):
        if len(self.binding_ids) != len(set(self.binding_ids)):
            raise ValueError('Duplicate request binding')
        if self.status == 'ERROR' and (not self.error_stage or not self.error_code or self.classifications):
            raise ValueError('Failed requests require an error stage/code and no classification')
        if self.status == 'COMPLETED' and (self.error_stage or self.error_code or not self.classifications):
            raise ValueError('Completed requests require classifications and no error')
        if any(c.expected != self.label for c in self.classifications):
            raise ValueError('Classification expected label must match the dataset label')
        ids = [c.binding_id for c in self.classifications]
        if self.status == 'COMPLETED' and set(ids) != set(self.binding_ids):
            raise ValueError('Completed request must classify every binding')
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate classification binding')
        return self
