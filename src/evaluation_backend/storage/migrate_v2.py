"""Offline, restartable v1 -> v2 migration. Stop API writes and finish Jobs first.

Run: python -m evaluation_backend.storage.migrate_v2 [--apply]
Uses the same EVAL_ES_* configuration as API/collector. Never deletes old indices.
"""
import argparse
import json

from . import Conflict, NotFound
from .elastic import ElasticsearchStorage


def scan(storage, collection):
    index = f'{storage.prefix}-{collection}-v1'
    scroll_id = None
    try:
        try:
            result = storage.call('POST', '/' + index + '/_search', params={'scroll': '2m'},
                                  json={'size': 100, 'sort': ['_doc'], 'query': {'match_all': {}}})
        except NotFound:
            return
        while True:
            scroll_id = result.get('_scroll_id')
            hits = result['hits']['hits']
            if not hits:
                break
            for hit in hits:
                yield hit['_source']
            result = storage.call('POST', '/_search/scroll', json={'scroll': '2m', 'scroll_id': scroll_id})
    finally:
        if scroll_id:
            storage.call('DELETE', '/_search/scroll', json={'scroll_id': [scroll_id]})


def migrate(storage, *, apply=False):
    if apply:
        storage.initialize()
    counts = {}
    for collection in ('datasets', 'environments', 'target-configs', 'run-contexts', 'experiments', 'samples'):
        counts[collection] = {'source': 0, 'created': 0, 'existing': 0}
        for source in scan(storage, collection):
            counts[collection]['source'] += 1
            if not apply:
                continue
            project, ident, value = source['project_id'], source['id'], source['payload']
            try:
                storage.put(collection, project, ident, value, create=True)
                counts[collection]['created'] += 1
            except Conflict:
                # Restarting never overwrites a v2 record, including newer edits.
                counts[collection]['existing'] += 1
            restored = storage.get(collection, project, ident)
            if collection == 'samples':
                # Score identity/value must survive migration; trace is loaded by reference.
                expected = [(s.get('scorerId'), s.get('value')) for s in value.get('scores', [])]
                actual = [(s.get('scorerId'), s.get('value')) for s in restored.get('scores', [])]
                if expected != actual:
                    raise RuntimeError('Migrated sample scores differ; old indices were preserved')
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='write v2 documents (default: count only)')
    args = parser.parse_args()
    storage = ElasticsearchStorage.from_env()
    print(json.dumps({'apply': args.apply, 'indices': storage.indices, 'counts': migrate(storage, apply=args.apply)}))


if __name__ == '__main__':
    main()
