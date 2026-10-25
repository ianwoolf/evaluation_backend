import json
import pytest
from evaluation_backend.content_policy import ContentPolicy


def test_defaults_preserve_report_content_without_mutating_execution_values():
    sample = {'input': {'text':'secret'}, 'output': {'text':'answer'}, 'reference': {'text':'label'}, 'scores':[{'reason':'secret'}]}
    assert ContentPolicy().sample(sample) == sample
    filtered = ContentPolicy(input='omit').sample(sample)
    assert filtered['input'] == {}
    assert filtered['output'] == {'text':'answer'}
    assert filtered['reference'] == {'text':'label'}
    assert filtered['scores'][0]['reason'] == ''
    assert sample['input']['text'] == 'secret'


def test_restricted_outputs_remove_free_text_and_unstructured_otel_copies():
    p = ContentPolicy(input='omit', response='omit', reference='omit')
    sample = {'input':{'text':'CANARY'},'output':{'text':'CANARY'},'reference':{'text':'CANARY'},'error':'CANARY',
              'scores':[{'scorerId':'s','value':1,'passed':True,'reason':'CANARY','extra':'CANARY'}],
              'trace':[{'name':'CANARY','kind':'SCORER','durationMs':12,'detail':'CANARY'}]}
    assert 'CANARY' not in json.dumps(p.sample(sample))
    observation = {'input':'CANARY','output':'CANARY','metadata':{'raw':'CANARY','ttft_ms':5},'usage_details':{'input':10},'duration_ms':12,'status_message':'CANARY'}
    cleaned = p.observation(observation)
    assert 'CANARY' not in json.dumps(cleaned)
    assert cleaned['usage_details']['input'] == 10
    assert cleaned['metadata']['ttft_ms'] == 5
    raw={'span':{'traceId':'a','spanId':'b','name':'CANARY','attributes':[{'value':'CANARY'}],'events':[{'name':'CANARY'}],'startTimeUnixNano':'1','endTimeUnixNano':'2'},'resource':{'anything':'CANARY'}}
    assert 'CANARY' not in json.dumps(p.trace_data(raw))


def test_invalid_policy_fails_closed():
    with pytest.raises(ValueError): ContentPolicy.from_json('{"input":"mask"}')
    with pytest.raises(ValueError): ContentPolicy.from_json('{"typo":"omit"}')
