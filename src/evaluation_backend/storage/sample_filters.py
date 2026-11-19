"""Source-backed table filters also work with historical sample documents."""
COLUMNS = {
    'sample_id': "if (params._source.sample_id != null) emit(params._source.sample_id);",
    'dataset_id': "if (params._source.dataset_id != null) emit(params._source.dataset_id);",
    'status': "if (params._source.status != null) emit(params._source.status);",
    'strategy': "def g=params._source.guardrail_result; if (g != null && g.strategy != null) emit(g.strategy); else emit('—');",
    'outcome': "def g=params._source.guardrail_result; if (params._source.status == 'ERROR') emit('ERROR'); else if (g != null && g.classifications != null) for (def c:g.classifications) emit(c.outcome);",
    'http_status': "def g=params._source.guardrail_result; if (g != null && g.http_status != null) emit(g.http_status.toString()); else emit('—');",
    'guardrail_latency': "def g=params._source.guardrail_result; if (g != null && g.guardrail_latency_ms != null) emit((Math.round(g.guardrail_latency_ms * 10.0) / 10.0).toString()); else emit('—');",
    'duration': "def v=params._source.latency_ms; if (v != null) emit(Math.round(v).toString()); else emit('—');",
    'scores': "def scores=params._source.scores; if (scores != null && !scores.isEmpty()) for (def s:scores) emit(s.scorer_id + ': ' + s.value); else emit('—');",
}


def keyword(column):
    return {'type': 'keyword', 'script': {'source': COLUMNS[column]}}


def apply_columns(columns, runtime, terms):
    for column, values in columns.items():
        field = 'column_' + column
        if column in ('input', 'response'):
            source_field = 'output' if column == 'response' else 'input'
            runtime[field] = {'type': 'boolean', 'script': {
                'source': "def v=params._source[params.field]; if (v != null) emit(v.toString().toLowerCase(Locale.ROOT).contains(params.text));",
                'params': {'field': source_field, 'text': values[0].lower()},
            }}
            terms.append({'term': {field: True}})
        else:
            runtime[field] = keyword(column)
            terms.append({'terms': {field: values}})


SORT_COLUMNS = {
    'duration': "def v=params._source.latency_ms; if (v != null) emit(v);",
    'guardrail_latency': "def g=params._source.guardrail_result; if (g != null && g.guardrail_latency_ms != null) emit(g.guardrail_latency_ms);",
    'scores': "def scores=params._source.scores; double total=0; int count=0; if (scores != null) for (def s:scores) if (s.value != null) {total+=s.value; count++;} if (count>0) emit(total/count);",
}


def apply_sort(sort, runtime):
    column, order = sort
    field = 'sample_sort'
    runtime[field] = {'type': 'double', 'script': {'source': SORT_COLUMNS[column]}}
    return [{field: {'order': order, 'missing': '_last'}}, {'id': 'asc'}]
