"""Guardrail report projection over run-owned request evidence."""
from evaluation_backend.guardrail_metrics import summarize_requests, aggregate_results
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
    dataset_ids=list(dict.fromkeys(b['dataset_id'] for b in bindings))
    names={d['dataset_id']:d.get('name',d['dataset_id']) for d in context.get('datasets',context.get('scoring_selections',[]))}
    target=context.get('target',{});connection=context.get('guardrail_connection',{})
    identity={'type':target.get('kind','GUARDRAIL'),'provider':connection.get('provider',target.get('provider')),
              'id':connection.get('id',target.get('guardrail_id') or target.get('id')),
              'name':connection.get('name') or target.get('guardrail_id') or target.get('name'),
              'registry_revision':connection.get('registry_revision')}
    return {'evaluation_config':{'id':context.get('evaluation_config_id',context.get('target_id',target.get('id'))),'version':context.get('evaluation_config_version',target.get('version'))},'guardrail':identity,'overall':aggregate_results(requests,bindings),
            'dataset_results':[{'dataset_id':ident,'name':names.get(ident,ident),'dataset_version':next(b['dataset_version'] for b in bindings if b['dataset_id']==ident),
                                **aggregate_results(requests,[b for b in bindings if b['dataset_id']==ident])} for ident in dataset_ids],
            'binding_results': summarize_requests(requests, bindings),
            'strategy_results': [{'strategy': strategy, 'bindings': summarize_requests(
                [r for r in requests if r['strategy'] == strategy],
                [{**b, 'sample_count': sum(b['binding_id'] in r['binding_ids'] for r in requests if r['strategy'] == strategy)} for b in bindings])}
                for strategy in strategies]}
