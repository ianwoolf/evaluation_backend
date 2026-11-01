from copy import deepcopy
from test_runs import setup

ROOT = '/api/v1/evaluations/scorers'

def test_scorer_catalog_persistence_versions_and_scope(setup):
    c, _, _, h = setup
    criteria = dict(aspect='grounding', subject='answer', expectation='Claims follow supplied context', requirements='Reject unsupported claims', evidence='retrieval_context, actual_output')
    draft = dict(name='Grounded answer', description='', kind='LLM_JUDGE', definition='Compare claims to context', threshold=.8, notes='Initial', criteria=criteria)
    body = dict(id='grounding', source='CUSTOM', enabled=True, draft=draft, versions=[{**draft, 'version':1, 'publishedAt':'2026-11-01T00:00:00Z'}])
    saved = c.post(ROOT+'/grounding', headers=h, json=body)
    assert saved.status_code == 200, saved.text
    value = saved.json()
    assert c.get(ROOT, headers=h).json()['items'][0]['draft']['criteria'] == criteria
    assert c.get(ROOT+'/grounding', headers={**h, 'X-Project-ID':'other'}).status_code == 404
    changed = deepcopy(value); changed['versions'][0]['criteria']['expectation'] = 'Changed'
    assert c.post(ROOT+'/grounding', headers=h, json=changed).status_code == 409
    changed = deepcopy(value); changed['draft']['criteria']['expectation'] = 'New expectation'
    changed['versions'].append({**changed['draft'], 'version':2, 'publishedAt':'2026-11-01T01:00:00Z'})
    result = c.post(ROOT+'/grounding', headers=h, json=changed)
    assert result.status_code == 200, result.text
    assert result.json()['versions'][0]['criteria'] == criteria
    assert c.post(ROOT+'/grounding', headers=h, json=changed).status_code == 409
