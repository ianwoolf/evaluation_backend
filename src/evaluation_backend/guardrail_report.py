"""Guardrail report projection over run-owned request evidence."""
from evaluation_backend.guardrail_metrics import summarize_requests, aggregate_results


def guardrail_report(context, samples):
    bindings = (context or {}).get('guardrail_bindings', [])
    if not bindings:
        return {}
    requests = [sample['guardrail_result'] for sample in samples if sample.get('guardrail_result')]
    strategies = sorted({r['strategy'] for r in requests})
    dataset_ids=list(dict.fromkeys(b['dataset_id'] for b in bindings))
    datasets = {d['dataset_id']: d for d in context.get('datasets', context.get('scoring_selections', []))}
    target=context.get('target',{});connection=context.get('guardrail_connection',{})
    identity={'type':target.get('kind','GUARDRAIL'),'provider':connection.get('provider',target.get('provider')),
              'id':connection.get('id',target.get('guardrail_id') or target.get('id')),
              'name':connection.get('name') or target.get('guardrail_id') or target.get('name'),
              'registry_revision':connection.get('registry_revision')}
    return {'evaluation_config':{'id':context.get('evaluation_config_id',context.get('target_id',target.get('id'))),'version':context.get('evaluation_config_version',target.get('version'))},'guardrail':identity,'overall':aggregate_results(requests,bindings),
            'dataset_results':[{'dataset_id':ident,'name':datasets.get(ident, {}).get('name', ident),'dataset_version':next(b['dataset_version'] for b in bindings if b['dataset_id']==ident),
                                'input_source': 'upload' if datasets.get(ident, {}).get('upload_id') else 'published',
                                **aggregate_results(requests,[b for b in bindings if b['dataset_id']==ident])} for ident in dataset_ids],
            'binding_results': summarize_requests(requests, bindings),
            'strategy_results': [{'strategy': strategy, 'bindings': summarize_requests(
                [r for r in requests if r['strategy'] == strategy],
                [{**b, 'sample_count': sum(b['binding_id'] in r['binding_ids'] for r in requests if r['strategy'] == strategy)} for b in bindings])}
                for strategy in strategies]}
