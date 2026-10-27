"""Read credentials from environment; pipe stdout to kubectl, never commit it."""
import argparse
import os
import sys
import yaml


def render(namespace='evaluation'):
    username = os.getenv('EVAL_ES_USERNAME', 'elastic')
    password = os.getenv('EVAL_ES_PASSWORD', '')
    if not username or not password:
        raise ValueError('Set EVAL_ES_USERNAME and EVAL_ES_PASSWORD (username defaults to elastic)')
    if os.getenv('EVAL_ES_API_KEY'):
        raise ValueError('Unset EVAL_ES_API_KEY before selecting password authentication')
    if ':' in username:
        raise ValueError('ES username must not contain a colon')
    # Merge-patch the consolidated Secret, preserving signing/provider credentials.
    # Username is non-secret configuration in evaluation-storage ConfigMap.
    return {'stringData': {'EVAL_ES_PASSWORD': password, 'EVAL_ES_API_KEY': ''}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--namespace', default='evaluation')
    args = parser.parse_args()
    if sys.stdout.isatty():
        parser.error('Pipe output directly to kubectl; credentials must not be printed to the terminal')
    try:
        result = render(args.namespace)
    except ValueError as error:
        parser.error(str(error))
    yaml.safe_dump(result, sys.stdout, sort_keys=False)


if __name__ == '__main__':
    main()
