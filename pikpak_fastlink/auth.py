from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

OFFICIAL_HOST = "https://api-drive.mypikpak.com"


def validate_https_origin(value: str) -> str:
    if value not in {OFFICIAL_HOST, OFFICIAL_HOST + "/"}:
        raise ValueError("PIKPAK_HOST must be the official PikPak API origin")
    return OFFICIAL_HOST


def _validate_secret(value: str, key: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    if any(not 32 <= ord(character) <= 126 for character in value):
        raise ValueError(f"{key} contains invalid characters")
    if value != value.strip():
        raise ValueError(f"{key} contains surrounding whitespace")


@dataclass(frozen=True)
class AuthConfig:
    host: str
    authorization: str = field(repr=False)
    device_id: str = field(repr=False)
    captcha_token: str = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "host", validate_https_origin(self.host))
        for key, value in (
            ("PIKPAK_AUTHORIZATION", self.authorization),
            ("PIKPAK_DEVICE_ID", self.device_id),
            ("PIKPAK_CAPTCHA_TOKEN", self.captcha_token),
        ):
            _validate_secret(value, key)
        if not self.authorization.startswith("Bearer ") or not self.authorization[7:].strip():
            raise ValueError("PIKPAK_AUTHORIZATION must include Bearer and a token")
        if any(character.isspace() for character in self.authorization[7:]):
            raise ValueError("PIKPAK_AUTHORIZATION contains invalid token whitespace")


def load_auth_config(
    env: Mapping[str, str] | None = None,
    dotenv_path: str | Path | None = None,
) -> AuthConfig:
    from dotenv import dotenv_values

    path = Path(dotenv_path) if dotenv_path is not None else Path(__file__).parent.parent / ".env"
    values = dict(dotenv_values(path, interpolate=False))
    values.update(os.environ if env is None else env)
    return AuthConfig(
        host=values.get("PIKPAK_HOST", OFFICIAL_HOST),
        authorization=values.get("PIKPAK_AUTHORIZATION", ""),
        device_id=values.get("PIKPAK_DEVICE_ID", ""),
        captcha_token=values.get("PIKPAK_CAPTCHA_TOKEN", ""),
    )
