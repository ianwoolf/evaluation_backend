"""Guardrail report projection over run-owned request evidence."""
from evaluation_backend.guardrail_metrics import summarize_requests, aggregate_results


def label_distribution(samples, bindings):
    """Count complete dataset references using the run's pinned label mappings."""
    expected=sum(binding.get('sample_count',0) for binding in bindings)
    if not expected or len(samples)!=expected:
        return None
    mappings={binding['binding_id']:binding.get('label_mapping',{}) for binding in bindings}
    counts={'positive':0,'negative':0,'total':expected}
    for sample in samples:
        reference=sample.get('reference') or {}
        raw=reference.get('label') if isinstance(reference,dict) else None
        result=sample.get('guardrail_result') or {}
        binding_ids=result.get('binding_ids') or []
        if raw is None or len(binding_ids)!=1 or binding_ids[0] not in mappings:
            return None
        normalized=mappings[binding_ids[0]].get(str(raw),str(raw))
        if normalized not in ('safe','unsafe'):
            return None
        counts['positive' if normalized=='unsafe' else 'negative']+=1
    return counts


def guardrail_report(context, samples):
    bindings = (context or {}).get('guardrail_bindings', [])
    if not bindings:
        return {}
    requests = [sample['guardrail_result'] for sample in samples if sample.get('guardrail_result')]
    strategies = sorted({r['strategy'] for r in requests})
    dataset_ids=list(dict.fromkeys(b['dataset_id'] for b in bindings))
    datasets = {d['dataset_id']: d for d in context.get('datasets', context.get('scoring_selections', []))}
    dataset_samples={ident:[] for ident in dataset_ids}
    dataset_by_binding={b['binding_id']:b['dataset_id'] for b in bindings}
    for sample in samples:
        result=sample.get('guardrail_result') or {}
        for ident in {dataset_by_binding[b] for b in result.get('binding_ids',[]) if b in dataset_by_binding}:
            dataset_samples[ident].append(sample)
    target=context.get('target',{});connection=context.get('guardrail_connection',{})
    identity={'type':target.get('kind','GUARDRAIL'),'provider':connection.get('provider',target.get('provider')),
              'id':connection.get('id',target.get('guardrail_id') or target.get('id')),
              'name':connection.get('name') or target.get('guardrail_id') or target.get('name'),
              'registry_revision':connection.get('registry_revision')}
    return {'evaluation_config':{'id':context.get('evaluation_config_id',context.get('target_id',target.get('id'))),'version':context.get('evaluation_config_version',target.get('version'))},'guardrail':identity,'overall':aggregate_results(requests,bindings),
            'dataset_results':[{'dataset_id':ident,'name':datasets.get(ident, {}).get('name', ident),'dataset_version':next(b['dataset_version'] for b in bindings if b['dataset_id']==ident),
                                'input_source': 'upload' if datasets.get(ident, {}).get('upload_id') else 'published',
                                'label_distribution':label_distribution(dataset_samples[ident],[b for b in bindings if b['dataset_id']==ident]),
                                **aggregate_results(requests,[b for b in bindings if b['dataset_id']==ident])} for ident in dataset_ids],
            'binding_results': summarize_requests(requests, bindings),
            'strategy_results': [{'strategy': strategy, 'bindings': summarize_requests(
                [r for r in requests if r['strategy'] == strategy],
                [{**b, 'sample_count': sum(b['binding_id'] in r['binding_ids'] for r in requests if r['strategy'] == strategy)} for b in bindings])}
                for strategy in strategies]}
