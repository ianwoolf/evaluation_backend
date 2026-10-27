"""Local runner configuration; environment overrides the selected dotenv file."""
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


def load_config(path=None):
    from dotenv import load_dotenv
    selected = Path(path or os.getenv("EVAL_CONFIG_FILE", ".env"))
    if path is not None and not selected.is_file():
        raise ValueError("configuration file does not exist")
    load_dotenv(selected, override=False)
    # Compose secrets are mounted files; Kubernetes may inject the same keys as env.
    for key in ('EVAL_TRIGGER_TOKEN', 'EVAL_MODEL_RESOLVER_TOKEN', 'EVAL_ES_PASSWORD',
                'EVAL_ES_API_KEY', 'OTEL_EXPORTER_OTLP_LOGS_HEADERS'):
        path = os.getenv(key + '_FILE')
        if path:
            os.environ[key] = Path(path).read_text().rstrip('\r\n')
    if os.getenv('EVAL_MODEL_TOKENS_FILE'):
        import json, re
        tokens = json.loads(Path(os.environ['EVAL_MODEL_TOKENS_FILE']).read_text())
        if not isinstance(tokens, dict) or any(not re.fullmatch(r'EVAL_MODEL_TOKEN_[A-F0-9]{12}', key) or not isinstance(value, str) for key,value in tokens.items()):
            raise ValueError('Invalid model token secret file')
        os.environ.update(tokens)


@dataclass(frozen=True)
class LangfuseConfig:
    base_url: str
    public_key: str = field(repr=False)
    secret_key: str = field(repr=False)

    @classmethod
    def from_env(cls):
        values = [os.environ.get(k, "").strip() for k in
                  ("LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")]
        if not all(values):
            raise ValueError("Langfuse URL, public key and secret key are required")
        parsed = urlparse(values[0])
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("invalid LANGFUSE_BASE_URL")
        return cls(values[0].rstrip("/"), values[1], values[2])
