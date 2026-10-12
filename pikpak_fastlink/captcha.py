"""Official web-client captcha metadata; access-token authentication remains server-side."""
from __future__ import annotations

import base64
import hashlib
import json

CLIENT_ID = "YUMx5nI8ZU8Ap8pm"
CLIENT_VERSION = "2.0.0"
PACKAGE_NAME = "mypikpak.com"
# shortcut: web-bundle signing constants, update when PikPak changes its signing config.
TIMESTAMP = "1791533770303"
SALTS = (
    "C9qPpZLN8ucRTaTiUMWYS9cQvWOE", "+r6CQVxjzJV6LCV", "F", "pFJRC",
    "9WXYIDGrwTCz2OiVlgZa90qpECPD6olt", "/750aCr4lm/Sly/c", "RB+DT/gZCrbV", "",
    "CyLsf7hdkIRxRm215hl", "7xHvLi2tOYP0Y92b", "ZGTXXxu8E/MIWaEDB+Sm/", "1UI3",
    "E7fP5Pfijd+7K+t6Tg/NhuLq0eEUVChpJSkrKxpO", "ihtqpG6FMt65+Xk+tWUH2", "NhXXU9rg4XXdzo7u5o",
)


def captcha_body(authorization: str, device_id: str, action: str, previous_token: str) -> dict:
    try:
        payload = authorization[7:].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        user_id = claims["sub"]
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError()
    except (ValueError, IndexError, KeyError, TypeError):
        raise ValueError("captcha renewal requires an access token with a user subject") from None
    # The decoded subject supplies metadata, not proof of authentication.
    value = CLIENT_ID + CLIENT_VERSION + PACKAGE_NAME + device_id + TIMESTAMP
    for salt in SALTS:
        value = hashlib.md5((value + salt).encode("utf-8")).hexdigest()
    return {
        "client_id": CLIENT_ID, "action": action, "device_id": device_id,
        "captcha_token": previous_token,
        "meta": {"captcha_sign": "1." + value, "client_version": CLIENT_VERSION,
                 "package_name": PACKAGE_NAME, "timestamp": TIMESTAMP, "user_id": user_id},
    }
