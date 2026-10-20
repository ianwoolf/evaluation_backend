import pytest
from evaluation_backend.config import load_config, LangfuseConfig


def test_file_configuration_and_environment_precedence(tmp_path, monkeypatch):
    for key in ('LANGFUSE_BASE_URL', 'LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY'):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / '.env'
    path.write_text('LANGFUSE_BASE_URL=http://localhost:3000/\nLANGFUSE_PUBLIC_KEY=file-public\nLANGFUSE_SECRET_KEY=file-secret\n')
    monkeypatch.setenv('LANGFUSE_PUBLIC_KEY', 'env-public')
    load_config(path)
    config = LangfuseConfig.from_env()
    assert config.base_url == 'http://localhost:3000'
    assert config.public_key == 'env-public'
    assert config.secret_key == 'file-secret'
    assert 'file-secret' not in repr(config)
    # load_dotenv modifies the process environment outside monkeypatch tracking.
    monkeypatch.delenv('LANGFUSE_BASE_URL')
    monkeypatch.delenv('LANGFUSE_SECRET_KEY')


def test_invalid_url_and_missing_config(monkeypatch, tmp_path):
    monkeypatch.setenv('LANGFUSE_BASE_URL', 'http://user:secret@localhost')
    monkeypatch.setenv('LANGFUSE_PUBLIC_KEY', 'public')
    monkeypatch.setenv('LANGFUSE_SECRET_KEY', 'secret')
    with pytest.raises(ValueError):
        LangfuseConfig.from_env()
    with pytest.raises(ValueError):
        load_config(tmp_path / 'missing')
