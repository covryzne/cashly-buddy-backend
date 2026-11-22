"""Web Push using RFC 8188, RFC 8291 and RFC 8292."""

import base64
import json
import os
import re
import struct
import time
from urllib.parse import urlsplit

import httpx2
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

TITLE = "Mochi"
BODY = "Ada jajan yang belum kecatet? Yuk, catat bareng Mochi."


def encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def decode(value: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Invalid base64url key")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def receiver_key(value: str):
    key = decode(value)
    if len(key) != 65 or key[0] != 4:
        raise ValueError("Invalid receiver key")
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), key)


def signing_key(value: str):
    raw = decode(value)
    if len(raw) != 32:
        raise ValueError("Invalid VAPID signing key")
    return ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())


def public_bytes(key) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def validate_endpoint(value: str) -> str:
    if any(ord(char) <= 32 or char == "\\" for char in value):
        raise ValueError("Invalid push endpoint")
    url = urlsplit(value)
    host = url.hostname or ""
    allowed = host in {
        "fcm.googleapis.com",
        "updates.push.services.mozilla.com",
        "web.push.apple.com",
    } or host.endswith((".push.apple.com", ".notify.windows.com"))
    if (
        not allowed
        or url.scheme != "https"
        or url.port not in (None, 443)
        or url.username is not None
        or url.password is not None
        or url.fragment
        or not url.path
        or url.path == "/"
    ):
        raise ValueError("Unsupported push endpoint")
    return f"https://{host}"


def encrypt(payload: bytes, key: str, auth: str, sender=None, salt=None) -> bytes:
    receiver = receiver_key(key)
    sender = sender or ec.generate_private_key(ec.SECP256R1())
    salt = salt if salt is not None else os.urandom(16)
    sender_public = public_bytes(sender)
    context = b"WebPush: info\x00" + decode(key) + sender_public
    secret = sender.exchange(ec.ECDH(), receiver)
    ikm = HKDF(hashes.SHA256(), 32, decode(auth), context).derive(secret)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(
        ikm
    )
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    header = salt + struct.pack("!IB", 4096, len(sender_public)) + sender_public
    return header + AESGCM(cek).encrypt(nonce, payload + b"\x02", None)


def authorization(endpoint: str, key, subject: str) -> str:
    claims = {
        "aud": validate_endpoint(endpoint),
        "exp": int(time.time()) + 3600,
        "sub": subject,
    }
    protected = encode(b'{"typ":"JWT","alg":"ES256"}')
    token = f"{protected}.{encode(json.dumps(claims, separators=(',', ':')).encode())}"
    r, s = decode_dss_signature(key.sign(token.encode(), ec.ECDSA(hashes.SHA256())))
    signature = encode(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={token}.{signature}, k={encode(public_bytes(key))}"


async def send(
    client: httpx2.AsyncClient, row, key, subject: str, *, test: bool = False
) -> int:
    notification_id = row["notification_id"]
    endpoint = row["endpoint"]
    credentials = authorization(endpoint, key, subject)
    message = {"id": str(notification_id), "title": TITLE, "body": BODY}
    if test:
        message.update(
            kind="test",
            title="Tes notifikasi Cashly",
            body="Notifikasi tes dari staging berhasil diterima di perangkat ini.",
        )
    payload = json.dumps(message).encode()
    response = await client.post(
        endpoint,
        content=encrypt(payload, row["public_key"], row["auth_secret"]),
        headers={
            "Authorization": credentials,
            "Content-Type": "application/octet-stream",
            "Content-Encoding": "aes128gcm",
            "TTL": "120" if test else "900",
            "Topic": encode(notification_id.bytes),
        },
    )
    return response.status_code
