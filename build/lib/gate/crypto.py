"""Signing helpers for Attest. Ed25519 keys, canonical JSON, hex encoding."""
from __future__ import annotations

import hashlib
import json
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)
from cryptography.exceptions import InvalidSignature


def canonical(obj) -> bytes:
    """Stable byte form of a JSON object, so signatures survive re-serialisation."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(obj) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


def keygen() -> tuple[str, str]:
    """Return (private_hex, public_hex)."""
    priv = Ed25519PrivateKey.generate()
    priv_hex = priv.private_bytes(serialization.Encoding.Raw,
                                  serialization.PrivateFormat.Raw,
                                  serialization.NoEncryption()).hex()
    pub_hex = priv.public_key().public_bytes(serialization.Encoding.Raw,
                                             serialization.PublicFormat.Raw).hex()
    return priv_hex, pub_hex


def sign(priv_hex: str, body: dict) -> str:
    priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(priv_hex))
    return priv.sign(canonical(body)).hex()


def verify(pub_hex: str, body: dict, sig_hex: str) -> bool:
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex))
        pub.verify(bytes.fromhex(sig_hex), canonical(body))
        return True
    except (InvalidSignature, ValueError):
        return False


def signed(priv_hex: str, body: dict) -> dict:
    """Return a copy of body with an id (hash of body) and a signature."""
    body = dict(body)
    body["id"] = digest(body)
    return {**body, "sig": sign(priv_hex, body)}


def body_of(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k != "sig"}


def id_ok(doc: dict) -> bool:
    """The id is the hash of the body without id and signature."""
    return digest({k: v for k, v in doc.items() if k not in ("id", "sig")}) == doc.get("id")
