"""Resolve dataset-local scorers without changing legacy shared selections."""
from copy import deepcopy
from evaluation_backend.capabilities import scoring_enabled
from evaluation_backend.experiment import Dataset


def resolve_scorers(ident, content, *, selection, mode, shared, service=None, project=None):
    if service and service.is_archived(project,'datasets',ident):raise ValueError('Dataset is archived: '+ident)
    ids = ['mock-score'] if mode == 'mock' else list(content.get('scorerIds', [])) if selection == 'dataset' else list(shared)
    if not ids or len(ids) != len(set(ids)) or len(ids) > 100:
        raise ValueError(f'Dataset {ident} requires nonempty, unique scorer IDs (maximum 100)')
    if mode == 'mock' and shared != ['mock-score']:
        raise ValueError('Mock mode requires only mock-score')
    allowed = content.get('scorerIds', [])
    if mode != 'mock' and allowed and any(s not in allowed for s in ids):
        raise ValueError(f'Scorer is not enabled for dataset {ident}')
    metadata = deepcopy(content.get('metadata', {}))
    if service and mode != 'mock':
        from evaluation_backend.storage import NotFound
        configs=metadata.setdefault('evaluation',{}).setdefault('scorers',{})
        for scorer_id in ids:
            if service.is_archived(project,'scorers',scorer_id):raise ValueError('Scorer is archived: '+scorer_id)
            config=configs.get(scorer_id,{})
            try:doc=service.storage.get('scorers',project,scorer_id)
            except NotFound:continue
            spec=next((v for v in doc['versions'] if str(v['version'])==str(config.get('version'))),None)
            if spec and spec.get('execution'):
                if not doc.get('enabled',True):raise ValueError('Scorer is disabled: '+scorer_id)
                configs[scorer_id]={'version':str(spec['version']),'threshold':spec['threshold'],'execution':deepcopy(spec['execution'])}
    if mode != 'mock' and scoring_enabled():
        from evaluation_backend.scorers import InstalledScorerLibrary
        InstalledScorerLibrary().get_scorers(Dataset(ident, content['name'], metadata), ids)
    return {'scorer_ids': ids, 'scorer_metadata': metadata}
