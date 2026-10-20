import pytest
from evaluation_backend.llm_target import parse_classification


@pytest.mark.parametrize('value', ['{"has_issue":true}', '```json\n{"has_issue": true}\n```', '```\n{"has_issue":true}\n```'])
def test_json_and_complete_code_fence(value):
    assert parse_classification(value) == {'has_issue': True}


@pytest.mark.parametrize('value', ['{"has_issue":"true"}', '[]', '{"other":true}', 'answer: {"has_issue":true}', '```json\n{"has_issue":true}', None])
def test_invalid_output_is_still_an_error(value):
    with pytest.raises(ValueError):
        parse_classification(value)
