"""Guardrail-specific contracts; generic datasets retain their existing schema."""
from typing import Annotated, Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Label = Literal['safe', 'unsafe']
ProviderId = Annotated[str, Field(pattern=r'^(nemo guardrail|[A-Za-z0-9][A-Za-z0-9_.-]{0,127})$')]


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
    roles = {f.name: f.role for f in fields}
    if any(roles.get(name) != role for name, role in expected.items()) or set(roles) - set(expected) - {'caseid'} or (roles.get('caseid') not in (None, 'metadata')) or any(f.name == 'caseid' and f.type != 'string' for f in fields):
        raise ValueError('Guardrail datasets require input, label, strategy and optional caseid')
    caseids = set()
    for row in rows:
        if set(row) - set(roles) or not set(expected).issubset(row):
            raise ValueError('Guardrail rows require input, label, strategy and optional caseid')
        caseid = row.get('caseid')
        if caseid not in (None, ''):
            if 'caseid' not in roles or not isinstance(caseid, str) or not caseid.strip() or len(caseid) > 128 or caseid in caseids:
                raise ValueError('Guardrail caseid must be unique nonempty text (maximum 128 characters)')
            caseids.add(caseid)
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
    caseid: str = Field(min_length=1, max_length=128)
    strategy: str
    status: Literal['COMPLETED', 'ERROR']
    state_type: Literal['ok', 'rate-limit', 'timeout', 'error']
    guardrail_outcome: str | None = None
    reason: str | None = Field(default=None, max_length=8000)
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
        if self.status == 'ERROR' and (not self.error_stage or not self.error_code or not self.reason or self.classifications or self.state_type == 'ok'):
            raise ValueError('Failed requests require stage, code, reason, non-ok state and no classification')
        if self.status == 'COMPLETED' and (self.error_stage or self.error_code or not self.classifications or self.state_type != 'ok'):
            raise ValueError('Completed requests require classifications and no error')
        ids = [c.binding_id for c in self.classifications]
        if self.status == 'COMPLETED' and set(ids) != set(self.binding_ids):
            raise ValueError('Completed request must classify every binding')
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate classification binding')
        return self
