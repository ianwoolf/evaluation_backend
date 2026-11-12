"""Run-owned scorer definitions and case-level aspect pass rates."""
from copy import deepcopy
from evaluation_backend.storage import NotFound


def snapshot_scorers(service, project, ids, metadata):
    configs = metadata.get('evaluation', {}).get('scorers', {})
    result = []
    for ident in ids:
        config = configs.get(ident, {})
        version = str(config.get('version', ''))
        spec = None
        try:
            doc = service.storage.get('scorers', project, ident)
            spec = next((v for v in doc['versions'] if str(v['version']) == version), None)
        except NotFound:
            pass
        result.append({'id': ident, 'version': version or None, 'name': spec['name'] if spec else ident,
            'aspect': (spec.get('criteria') or {}).get('aspect', 'unclassified') if spec else 'unclassified',
            'definition': deepcopy(spec), 'execution': deepcopy(config),
            'threshold': config.get('threshold', .8), 'provenance': 'snapshot' if spec else 'unavailable'})
    return result


def aspect_results(context, samples):
    if context is None:
        return {'bindings': [], 'aspects': []}
    selections = context.get('scoring_selections') or context.get('datasets') or []
    bindings = [{**b, 'dataset_id': d['dataset_id'], 'dataset_version': d['dataset_version']}
                for d in selections for b in d.get('scorer_bindings', [])]
    groups = {}
    dataset_groups = {}
    def group(aspect, source):
        return groups.setdefault((aspect, source), dict(aspect=aspect, source=source, evaluated=0, passed=0, errors=0, missing=0, expected=0))
    mode = 'mock' if context.get('feature_modes', {}).get('scoring_enabled') is False or context.get('scoring_mode') == 'mock' else 'real'
    for d in selections:
        for aspect in {b['aspect'] for b in d.get('scorer_bindings', [])}:
            group(aspect, mode)['expected'] += d.get('sample_count', 0)
            dataset_groups[(d['dataset_id'],aspect,mode)] = dict(dataset_id=d['dataset_id'],aspect=aspect,source=mode,evaluated=0,passed=0,errors=0,missing=0)
    for sample in samples:
        selected = [b for b in bindings if b['dataset_id'] == sample.get('dataset_id', context.get('dataset_id'))]
        scores = {s['scorerId']: s for s in sample.get('scores', [])}
        aspects = {}
        for b in selected: aspects.setdefault(b['aspect'], []).append(b['id'])
        if not selected and scores: aspects['unclassified'] = list(scores)
        for aspect, ids in aspects.items():
            values = [scores.get(i) for i in ids]
            sources = {s.get('source', mode) for s in values if s}
            source = next(iter(sources)) if len(sources) == 1 else 'mixed' if sources else mode
            g = group(aspect, source); g['evaluated'] += 1
            missing = any(s is None for s in values)
            error = sample['status'] == 'ERROR' or any(s and s.get('status') == 'error' for s in values)
            g['missing'] += int(missing); g['errors'] += int(error)
            g['passed'] += int(not missing and not error and all(s['passed'] for s in values))
            dg=dataset_groups.setdefault((sample.get('dataset_id',context.get('dataset_id')),aspect,source),dict(dataset_id=sample.get('dataset_id',context.get('dataset_id')),aspect=aspect,source=source,evaluated=0,passed=0,errors=0,missing=0))
            dg['evaluated']+=1; dg['missing']+=int(missing); dg['errors']+=int(error)
            dg['passed']+=int(not missing and not error and all(s['passed'] for s in values))
    totals = {}
    for g in groups.values(): totals[g['aspect']] = totals.get(g['aspect'],0)+g['evaluated']
    return {'dataset_aspects': [{**g,'pass_rate':g['passed']/g['evaluated'] if g['evaluated'] else None} for g in dataset_groups.values()], 'bindings': bindings, 'aspects': [{**g, 'pending': max(0,g['expected']-totals[g['aspect']]),
        'pass_rate': g['passed']/g['evaluated'] if g['evaluated'] else None} for g in groups.values()]}
