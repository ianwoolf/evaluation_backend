import copy
import pytest
from pydantic import ValidationError
from evaluation_backend.storage.service import DatasetContent
from evaluation_backend.guardrail_contracts import ClassificationResult, GuardrailRequestResult


def content():
    return dict(name='Injection', objective='Detect injection', evaluation_type='guardrail',
                guardrail=dict(scorer_id='classifier', scorer_version=1, aspect_id='injection',
                               aspect_name='Injection', label_mapping={'0': 'safe', '1': 'unsafe'}),
                scorerIds=['classifier'], fields=[dict(name=n, role=r, type=t, required=True) for n,r,t in
                    [('input','input','string'),('label','reference','number'),('strategy','metadata','string')]],
                rows=[dict(input='hello',label=0,strategy='benign')])


def test_guardrail_roundtrip_and_strict_mapping():
    original=content(); parsed=DatasetContent.model_validate(original)
    assert parsed.guardrail.normalize_label(0)=='safe'
    assert parsed.guardrail.normalize_label(1)=='unsafe'
    assert DatasetContent.model_validate_json(parsed.model_dump_json())==parsed
    for invalid in [True,False,1.0,None,'unknown']:
        value=copy.deepcopy(original);value['rows'][0]['label']=invalid
        with pytest.raises(ValidationError):DatasetContent.model_validate(value)


@pytest.mark.parametrize('change', ['missing-column','extra-column','wrong-role','empty-input','scorer-mismatch'])
def test_invalid_guardrail_dataset(change):
    value=content()
    if change=='missing-column':del value['rows'][0]['strategy']
    elif change=='extra-column':value['rows'][0]['secret']='not allowed'
    elif change=='wrong-role':value['fields'][1]['role']='metadata'
    elif change=='empty-input':value['rows'][0]['input']='  '
    elif change=='missing-config':del value['guardrail']
    else:value['scorerIds']=['other']
    with pytest.raises(ValidationError):DatasetContent.model_validate(value)


def test_generic_serialization_does_not_change_immutable_versions():
    value=dict(name='Generic',objective='Existing behavior',fields=[dict(name='question',role='input',type='string',required=True)],rows=[dict(question='hi',arbitrary='retained')])
    result=DatasetContent.model_validate(value).model_dump(mode='json')
    assert 'evaluation_type' not in result and 'guardrail' not in result
    assert result['rows']==value['rows']


@pytest.mark.parametrize('expected,predicted,outcome',[('unsafe','unsafe','TP'),('safe','unsafe','FP'),('safe','safe','TN'),('unsafe','safe','FN')])
def test_quadrant_semantics(expected,predicted,outcome):
    result=ClassificationResult(binding_id='b',expected=expected,predicted=predicted,outcome=outcome,reason='check')
    assert result.outcome==outcome
    with pytest.raises(ValidationError):ClassificationResult(**{**result.model_dump(),'outcome':'FN' if outcome!='FN' else 'TP'})


def test_errors_never_classified_and_label_must_match():
    args=dict(request_id='r',binding_ids=['b'],row_id='row-1',strategy='s',label='unsafe',status='ERROR',guardrail_latency_ms=10,scorer_latency_ms=0,total_latency_ms=10,error_stage='target',error_code='timeout')
    assert GuardrailRequestResult(**args).classifications==[]
    c=dict(binding_id='b',expected='safe',predicted='safe',outcome='TN',reason='check')
    with pytest.raises(ValidationError):GuardrailRequestResult(**{**args,'classifications':[c]})
    with pytest.raises(ValidationError):GuardrailRequestResult(**{**args,'status':'COMPLETED','error_stage':None,'error_code':None,'classifications':[c]})


def test_binding_metrics_errors_duplicates_and_null_denominators():
    from evaluation_backend.guardrail_metrics import summarize_requests
    requests=[]
    for i,(expected,predicted,outcome) in enumerate([('unsafe','unsafe','TP'),('safe','unsafe','FP'),('safe','safe','TN'),('unsafe','safe','FN')]):
        requests.append(dict(request_id=str(i),binding_ids=['b'],status='COMPLETED',sent=True,
            guardrail_latency_ms=(i+1)*10,cost={'amount':.001},classifications=[dict(binding_id='b',outcome=outcome)]))
    requests.append(dict(request_id='error',binding_ids=['b'],status='ERROR',sent=True,guardrail_latency_ms=100,cost={'amount':.001}))
    result=summarize_requests(requests+[requests[0]],[dict(binding_id='b',sample_count=6),dict(binding_id='empty',sample_count=1)])
    r=result[0]
    assert [r[k] for k in ('TP','FP','TN','FN')]==[1,1,1,1]
    assert r['errors']==1 and r['pending']==1 and r['coverage']==.8
    assert r['f1']==.5 and r['accuracy']==.5 and r['cost']['amount']==pytest.approx(.005)
    assert r['latency']['p95_ms']==100
    assert result[1]['f1'] is None and result[1]['latency']['p95_ms'] is None


def test_guardrail_target_id_is_configurable(monkeypatch):
    from evaluation_backend.guardrail_runs import resolve_bindings
    from fastapi import HTTPException
    monkeypatch.setenv('EVAL_MOCK_GUARDRAIL_ID', 'gr')
    assert resolve_bindings(None, 'p', {'id':'gr','kind':'GUARDRAIL'}, {'modelId':'none'}, [], []) == []
    with pytest.raises(HTTPException) as error:
        resolve_bindings(None, 'p', {'id':'other','kind':'GUARDRAIL'}, {'modelId':'none'}, [], [])
    assert error.value.status_code == 422
    assert 'No connection configured' in error.value.detail
    with pytest.raises(HTTPException):
        resolve_bindings(None, 'p', {'id':'gr','kind':'PROMPT'}, {'modelId':'none'}, [], [])


def test_multiple_configs_share_guardrail_connection(monkeypatch):
    from evaluation_backend.guardrail_runs import resolve_bindings
    monkeypatch.setenv('EVAL_MOCK_GUARDRAIL_ID', 'mock-guardrail')
    for ident in ('debug-gr', 'regression-gr'):
        assert resolve_bindings(None, 'p', {'id':ident,'kind':'GUARDRAIL','guardrail_id':'mock-guardrail'}, {'modelId':'none'}, [], []) == []
