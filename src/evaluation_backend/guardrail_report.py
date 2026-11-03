"""Guardrail report projection over run-owned request evidence."""
from evaluation_backend.guardrail_metrics import summarize_requests
from evaluation_backend.storage import NotFound


def guardrail_report(service, project, evaluation):
    try:
        context = service.storage.get('run-contexts', project, evaluation)
    except NotFound:
        return {}
    bindings = context.get('guardrail_bindings', [])
    if not bindings:
        return {}
    requests, cursor = [], 0
    while True:
        page = service.storage.search('samples', project, filters={'evaluation_id': evaluation}, offset=cursor, limit=100)
        requests.extend(s['guardrail_result'] for s in page['items'] if s.get('guardrail_result'))
        cursor += len(page['items'])
        if not page['items'] or cursor >= page['total']:
            break
    strategies = sorted({r['strategy'] for r in requests})
    return {'binding_results': summarize_requests(requests, bindings),
            'strategy_results': [{'strategy': strategy, 'bindings': summarize_requests(
                [r for r in requests if r['strategy'] == strategy],
                [{**b, 'sample_count': sum(b['binding_id'] in r['binding_ids'] for r in requests if r['strategy'] == strategy)} for b in bindings])}
                for strategy in strategies]}
