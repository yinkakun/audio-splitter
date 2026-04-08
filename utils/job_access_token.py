from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


class JobAccessTokenManager:
    def __init__(self, secret: str, ttl_seconds: int):
        if not secret:
            raise ValueError("Job access token secret is required")
        self.secret = secret.encode("utf-8")
        self.ttl_seconds = ttl_seconds

    def create_token(self, track_id: str, now: float | None = None) -> tuple[str, int]:
        issued_at = int(now or time.time())
        expires_at = issued_at + self.ttl_seconds
        payload = {
            "iat": issued_at,
            "exp": expires_at,
            "track_id": track_id,
        }
        payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        payload_b64 = _b64url_encode(payload_json)
        signature = hmac.new(self.secret, payload_b64.encode("ascii"), hashlib.sha256).digest()
        token = f"{payload_b64}.{_b64url_encode(signature)}"
        return token, expires_at

    def verify_token(self, token: str, track_id: str, now: float | None = None) -> dict[str, Any]:
        try:
            payload_b64, signature_b64 = token.split(".", 1)
        except ValueError as exc:
            raise ValueError("Malformed access token") from exc

        expected_signature = hmac.new(
            self.secret, payload_b64.encode("ascii"), hashlib.sha256
        ).digest()
        provided_signature = _b64url_decode(signature_b64)

        if not hmac.compare_digest(expected_signature, provided_signature):
            raise ValueError("Invalid access token signature")

        try:
            payload = json.loads(_b64url_decode(payload_b64))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Malformed access token payload") from exc

        expires_at = int(payload.get("exp", 0))
        payload_track_id = payload.get("track_id")

        if payload_track_id != track_id:
            raise ValueError("Access token is not valid for this track")

        if int(now or time.time()) >= expires_at:
            raise ValueError("Access token has expired")

        return payload
