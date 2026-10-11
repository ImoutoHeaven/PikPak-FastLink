from __future__ import annotations

import pytest

from pikpak_fastlink import auth
from pikpak_fastlink.auth import AuthConfig, OFFICIAL_HOST, load_auth_config


ENV = {
    "PIKPAK_AUTHORIZATION": "Bearer authorization-secret",
    "PIKPAK_DEVICE_ID": "device-secret",
    "PIKPAK_CAPTCHA_TOKEN": "captcha-secret",
}


def test_auth_uses_official_origin_and_never_exposes_secrets_in_repr(tmp_path):
    config = load_auth_config(env=ENV, dotenv_path=tmp_path / "missing.env")
    assert config.host == OFFICIAL_HOST
    assert config.authorization == ENV["PIKPAK_AUTHORIZATION"]
    assert config.device_id == ENV["PIKPAK_DEVICE_ID"]
    assert config.captcha_token == ENV["PIKPAK_CAPTCHA_TOKEN"]
    assert "secret" not in repr(config)


def test_default_dotenv_is_project_root_not_working_directory(tmp_path, monkeypatch):
    project = tmp_path / "project"
    package = project / "pikpak_fastlink"
    package.mkdir(parents=True)
    root_env = project / ".env"
    root_env.write_text("\n".join(f'{key}="{value}"' for key, value in ENV.items()), encoding="utf-8")
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    (elsewhere / ".env").write_text("PIKPAK_AUTHORIZATION=Bearer wrong-cwd-secret\n", encoding="utf-8")
    monkeypatch.setattr(auth, "__file__", str(package / "auth.py"))
    monkeypatch.chdir(elsewhere)
    config = load_auth_config(env={})
    assert config.authorization == ENV["PIKPAK_AUTHORIZATION"]
    assert config.device_id == ENV["PIKPAK_DEVICE_ID"]
    assert config.captcha_token == ENV["PIKPAK_CAPTCHA_TOKEN"]


def test_process_env_overrides_explicit_dotenv_without_mutating_process(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("\n".join(f'{key}=dotenv-value' for key in ENV), encoding="utf-8")
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("PIKPAK_HOST", raising=False)
    config = load_auth_config(dotenv_path=path)
    assert config.authorization == ENV["PIKPAK_AUTHORIZATION"]
    assert config.device_id == ENV["PIKPAK_DEVICE_ID"]
    assert config.captcha_token == ENV["PIKPAK_CAPTCHA_TOKEN"]
    assert auth.os.environ["PIKPAK_AUTHORIZATION"] == ENV["PIKPAK_AUTHORIZATION"]


@pytest.mark.parametrize("key", list(ENV))
@pytest.mark.parametrize("value", ["", " ", "header-secret\r\nInjected: value", "header-secret\n", "header-secret\x00"])
def test_required_secret_validation_reports_key_not_secret(tmp_path, key, value):
    with pytest.raises(ValueError) as exc:
        load_auth_config(env={**ENV, key: value}, dotenv_path=tmp_path / "missing.env")
    assert key in str(exc.value)
    assert "header-secret" not in str(exc.value)


@pytest.mark.parametrize("authorization", ["token-secret", "Bearer ", "bearer token-secret", "Bearer token secret", "Bearer token-secret "])
def test_authorization_requires_full_bearer_header(authorization):
    with pytest.raises(ValueError, match="PIKPAK_AUTHORIZATION"):
        AuthConfig(OFFICIAL_HOST, authorization, "device", "captcha")


@pytest.mark.parametrize("host", ["http://api-drive.mypikpak.com", "https://attacker.invalid", "https://api-drive.mypikpak.com@attacker.invalid", OFFICIAL_HOST + "/path", OFFICIAL_HOST + "?secret=secret"])
def test_origin_override_cannot_redirect_credentials(tmp_path, host):
    with pytest.raises(ValueError, match="PIKPAK_HOST") as exc:
        load_auth_config(env={**ENV, "PIKPAK_HOST": host}, dotenv_path=tmp_path / "missing.env")
    assert "secret" not in str(exc.value)


def test_empty_process_override_does_not_fall_back_to_dotenv_secret(tmp_path):
    path = tmp_path / ".env"
    path.write_text("\n".join(f'{key}="{value}"' for key, value in ENV.items()), encoding="utf-8")
    with pytest.raises(ValueError, match="PIKPAK_AUTHORIZATION"):
        load_auth_config(env={"PIKPAK_AUTHORIZATION": ""}, dotenv_path=path)
