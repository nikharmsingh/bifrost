"""Bring-your-own-key: each user's provider API keys, encrypted at rest.

Keys are sealed with AES-GCM under KEY_ENCRYPTION_SECRET (32 random bytes, urlsafe base64). The
user id and provider are bound in as associated data, so a ciphertext copied to another user's row
fails to decrypt. Only the last four characters are ever shown back.
"""

import base64
import binascii
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import sqlalchemy as sa
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException

from ._schema import user_keys_table

KEY_VERSION = "v1"


@dataclass(frozen=True)
class Provider:
    name: str
    label: str
    console_url: str
    models_url: str


PROVIDERS = {
    "groq": Provider("groq", "Groq", "https://console.groq.com/keys", "https://api.groq.com/openai/v1/models"),
}


def provider(name: str) -> Provider:
    if name not in PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown provider '{name}'")
    return PROVIDERS[name]


def _cipher() -> AESGCM:
    try:
        key = base64.urlsafe_b64decode(os.getenv("KEY_ENCRYPTION_SECRET", ""))
    except (ValueError, binascii.Error):
        key = b""
    if len(key) != 32:
        raise HTTPException(status_code=503, detail="Saving API keys is not configured (KEY_ENCRYPTION_SECRET)")
    return AESGCM(key)


def _associated_data(user_id: str, provider_name: str) -> bytes:
    return f"{user_id}:{provider_name}".encode()


def encrypt(user_id: str, provider_name: str, plaintext: str) -> str:
    nonce = secrets.token_bytes(12)
    sealed = _cipher().encrypt(nonce, plaintext.encode(), _associated_data(user_id, provider_name))
    return f"{KEY_VERSION}:{base64.urlsafe_b64encode(nonce + sealed).decode()}"


def decrypt(user_id: str, provider_name: str, token: str) -> str:
    version, _, payload = token.partition(":")
    try:
        if version != KEY_VERSION:
            raise ValueError(version)
        raw = base64.urlsafe_b64decode(payload)
        return _cipher().decrypt(raw[:12], raw[12:], _associated_data(user_id, provider_name)).decode()
    except (ValueError, binascii.Error, InvalidTag):
        # Wrong secret, tampered row, or a row moved between users — ask for the key again
        raise HTTPException(
            status_code=428, detail="Your saved key can't be read any more — please add it again in Settings"
        ) from None


def validate_with_provider(provider_name: str, api_key: str) -> None:
    """One free call (list models) so a typo or revoked key is caught at save time."""
    p = provider(provider_name)
    try:
        response = httpx.get(p.models_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=10)
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail=f"Couldn't reach {p.label} to check the key") from None
    if response.status_code in (401, 403):
        raise HTTPException(status_code=400, detail=f"{p.label} rejected that key — check it and try again")
    if response.status_code != 200:
        raise HTTPException(
            status_code=502, detail=f"{p.label} couldn't verify the key right now ({response.status_code})"
        )


def save(conn: sa.Connection, user_id: str, provider_name: str, api_key: str) -> dict:
    row = {
        "user_id": user_id,
        "provider": provider_name,
        "ciphertext": encrypt(user_id, provider_name, api_key),
        "last4": api_key[-4:],
        "updated_at": datetime.now(UTC).isoformat(),
    }
    owned = sa.and_(user_keys_table.c.user_id == user_id, user_keys_table.c.provider == provider_name)
    conn.execute(sa.delete(user_keys_table).where(owned))
    conn.execute(sa.insert(user_keys_table).values(**row))
    return row


def load(conn: sa.Connection, user_id: str, provider_name: str) -> dict | None:
    owned = sa.and_(user_keys_table.c.user_id == user_id, user_keys_table.c.provider == provider_name)
    row = conn.execute(sa.select(user_keys_table).where(owned)).mappings().first()
    return dict(row) if row else None


def remove(conn: sa.Connection, user_id: str, provider_name: str) -> bool:
    owned = sa.and_(user_keys_table.c.user_id == user_id, user_keys_table.c.provider == provider_name)
    return conn.execute(sa.delete(user_keys_table).where(owned)).rowcount > 0
