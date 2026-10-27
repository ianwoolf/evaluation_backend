"""Create missing file secrets. Does not print or rotate existing credentials."""
import json
import os
from pathlib import Path
import secrets

root=Path(__file__).resolve().parent
directory=root/'.secrets'
directory.mkdir(mode=0o700,exist_ok=True)
os.chmod(directory,0o700)
values={'trigger_token':os.getenv('EVAL_TRIGGER_TOKEN') or secrets.token_urlsafe(32),
        'resolver_token':os.getenv('EVAL_MODEL_RESOLVER_TOKEN') or secrets.token_urlsafe(32),
        'es_password':os.getenv('EVAL_ES_PASSWORD') or secrets.token_urlsafe(32),
        'es_api_key':os.getenv('EVAL_ES_API_KEY',''),
        'otel_headers':os.getenv('OTEL_EXPORTER_OTLP_LOGS_HEADERS',''), 'model_tokens':'{}'}
if os.getenv('EVAL_MODEL_REGISTRY'):
    from evaluation_backend.deployment_config import split_models
    public,tokens=split_models(json.loads(Path(os.environ['EVAL_MODEL_REGISTRY']).read_text()))
    target=root/'config/models.json'
    # Explicit source registry imports config; token values only enter the Secret file.
    target.write_text(json.dumps(public,indent=2),encoding='utf-8')
    existing=directory/'model_tokens'
    if existing.exists():
        saved=json.loads(existing.read_text())
        for key,value in tokens.items():saved.setdefault(key,value)
        existing.write_text(json.dumps(saved),encoding='utf-8')
    values['model_tokens']=json.dumps(tokens)
for name,value in values.items():
    path=directory/name
    if not path.exists():
        path.write_text(value,encoding='utf-8')
        # Parent is private; file mounts must be readable by API UID 10001 / ES UID 1000.
        path.chmod(0o644)
print('Secret files ready; existing credentials preserved. No credentials printed.')
