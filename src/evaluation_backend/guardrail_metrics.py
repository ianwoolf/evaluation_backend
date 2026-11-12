"""Binding-specific binary classification statistics, independent of UI pagination."""
import math


def latency_summary(values):
    times = sorted(values)
    count = len(times)

    def percentile(p):
        return times[math.ceil(p * count) - 1] if count else None

    return {'average_ms': sum(times) / count if count else None,
            'p50_ms': percentile(.5), 'p95_ms': percentile(.95), 'count': count}


def summarize_requests(requests, bindings):
    groups = {b['binding_id']: {**b, 'TP': 0, 'FP': 0, 'TN': 0, 'FN': 0,
              'errors': 0, 'processed': 0, 'latencies': [], 'cost': 0.0} for b in bindings}
    seen = set()
    for request in requests:
        # Persistence is an upsert; this guard also avoids duplicate delivery counting.
        key = request['request_id']
        if key in seen:
            continue
        seen.add(key)
        for ident in request['binding_ids']:
            if ident not in groups:
                raise ValueError('Unknown request binding')
            g = groups[ident]
            g['processed'] += 1
            if request.get('http_status') is not None or request.get('sent', False):
                latency = request['guardrail_latency_ms']
                if type(latency) not in (float, int) or not math.isfinite(latency) or latency < 0:
                    raise ValueError('Invalid Guardrail latency')
                g['latencies'].append(latency)
            g['cost'] += request.get('cost', {}).get('amount', 0)
            values = [c for c in request.get('classifications', []) if c['binding_id'] == ident]
            if request['status'] == 'ERROR' or not values:
                g['errors'] += 1
            else:
                if len(values) != 1:
                    raise ValueError('Duplicate binding classification')
                g[values[0]['outcome']] += 1
    result = []
    def ratio(a, b):
        return a / b if b else None
    for g in groups.values():
        tp, fp, tn, fn = [g[k] for k in ('TP', 'FP', 'TN', 'FN')]
        valid = tp + fp + tn + fn
        times = g.pop('latencies')
        amount = g.pop('cost')
        result.append({**g, 'valid_count': valid, 'pending': max(0, g.get('sample_count', 0)-g['processed']),
            'accuracy': ratio(tp+tn, valid), 'precision': ratio(tp, tp+fp),
            'recall': ratio(tp, tp+fn), 'f1': ratio(2*tp, 2*tp+fp+fn),
            'fpr': ratio(fp, fp+tn), 'fnr': ratio(fn, fn+tp),
            'coverage': ratio(valid, g['processed']),
            'latency': latency_summary(times),
            'cost': {'amount': amount, 'currency': 'USD', 'source': 'mock'}})
    return result


def aggregate_results(requests, bindings):
    """Micro-average classification counts; latency counts actual sent requests once."""
    ids={b['binding_id'] for b in bindings}
    selected=[];seen=set()
    for r in requests:
        if r['request_id'] in seen or not ids.intersection(r['binding_ids']):continue
        seen.add(r['request_id']);selected.append(r)
    counts={k:0 for k in ('TP','FP','TN','FN')}
    times=[];errors=0
    for r in selected:
        values=[c for c in r.get('classifications',[]) if c['binding_id'] in ids]
        if r['status']=='ERROR' or not values:errors+=1
        else:
            for c in values:counts[c['outcome']]+=1
        if r.get('http_status') is not None or r.get('sent',False):times.append(r['guardrail_latency_ms'])
    tp,fp,tn,fn=[counts[k] for k in ('TP','FP','TN','FN')]
    def ratio(a,b):return a/b if b else None
    valid=tp+fp+tn+fn
    return {**counts,'precision':ratio(tp,tp+fp),'recall':ratio(tp,tp+fn),'accuracy':ratio(tp+tn,valid),'f1':ratio(2*tp,2*tp+fp+fn),
            'processed':len(selected),'errors':errors,'valid_count':valid,'sample_count':sum(b.get('sample_count',0) for b in bindings),
            'pending':max(0,sum(b.get('sample_count',0) for b in bindings)-len(selected)),
            'latency':latency_summary(times)}
