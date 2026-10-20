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
