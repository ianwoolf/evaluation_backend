"""Split model metadata and credentials for both deployment formats."""
import hashlib


def split_models(records):
    if not isinstance(records, dict): raise ValueError('Model registry must be an object')
    public, tokens = {}, {}
    for ident, record in records.items():
        key = record.get('token_env') or 'EVAL_MODEL_TOKEN_' + hashlib.sha256(ident.encode()).hexdigest()[:12].upper()
        public[ident] = {k:v for k,v in record.items() if k != 'token'}
        public[ident]['token_env'] = key
        if record.get('token'): tokens[key] = record['token']
    return public, tokens
