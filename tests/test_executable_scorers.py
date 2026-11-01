import pytest
from evaluation_backend.executable_scorers import validate_execution, evaluate_execution


def test_rule_types_missing_and_no_model():
    rule={'engine':'rule','operator':'equals','actual':'actual_output.has_issue','expected_path':'expected_output.has_issue'}
    assert evaluate_execution(rule,{'actual_output':{'has_issue':False},'expected_output':{'has_issue':False}}).value==1
    assert evaluate_execution(rule,{'actual_output':{'has_issue':0},'expected_output':{'has_issue':False}}).value==0
    with pytest.raises(ValueError):evaluate_execution(rule,{'actual_output':{},'expected_output':{'has_issue':False}})


def test_prompt_binding_and_strict_response():
    config={'engine':'llm_judge','strategy':'prompt','model_id':'glm-dev','inputs':['input','actual_output'],'prompt':'Assess {{actual_output.answer}} against {{input.question}}'}
    seen=[]
    def judge(model,messages,timeout):
        seen.append((model,messages));return '{"score":0.7,"reason":"Partial answer"}'
    assert evaluate_execution(config,{'input':{'question':'Q'},'actual_output':{'answer':'A'}},judge=judge).value==.7
    assert seen[0][0]=='glm-dev' and '"A"' in seen[0][1][1]['content']
    with pytest.raises(ValueError):validate_execution({**config,'prompt':'{{secret.token}}'})
    with pytest.raises(ValueError):evaluate_execution(config,{'input':{},'actual_output':{'answer':'A'}},judge=judge)
    with pytest.raises(ValueError):evaluate_execution(config,{'input':{'question':'Q'},'actual_output':{'answer':'A'}},judge=lambda *a:'{"score":2,"reason":"bad"}')
