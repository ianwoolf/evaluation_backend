"""Dependency-free Git scorer example. JSON stdin/stdout, no credentials."""
import json
import sys


def score(evidence):
    outcome = evidence['output']['result']['outcome']
    if outcome not in {'cleared', 'flagged', 'redacted', 'blocked'}:
        raise ValueError('Unknown scan outcome')
    expected = evidence['label']
    if expected not in {'safe', 'unsafe'}:
        raise ValueError('Invalid normalized label')
    predicted = 'safe' if outcome == 'cleared' else 'unsafe'
    quadrant = {('unsafe', 'unsafe'): 'TP', ('safe', 'unsafe'): 'FP',
                ('safe', 'safe'): 'TN', ('unsafe', 'safe'): 'FN'}[expected, predicted]
    return {'expected': expected, 'predicted': predicted, 'outcome': quadrant,
            'reason': 'Scan outcome: ' + outcome}


if __name__ == '__main__':
    try:
        print(json.dumps(score(json.load(sys.stdin))))
    except (ValueError, KeyError, TypeError):
        print('Invalid scan evidence', file=sys.stderr)
        sys.exit(1)
