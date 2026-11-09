"""Web Push: send a notification to a phone or computer browser.

Implements the two standards every browser push service uses, with only the
`cryptography` package (prebuilt wheels everywhere, unlike pywebpush's
http-ece dependency, which fails to build on newer Pythons):

  * RFC 8291 / RFC 8188 -- the message is encrypted for that one browser
    (aes128gcm), so the push service (Google, Apple, Mozilla) cannot read it
  * RFC 8292 (VAPID)    -- the request is signed with this app's key, so the
    push service knows who is sending

Keys are a P-256 key pair: the public half goes to the browser when it
subscribes, the private half stays on the server.
"""
import base64
import hashlib
import hmac
import json
import os
import struct
import time
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_dec(s: str) -> bytes:
    s = s.strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def new_keys():
    """(public_b64u, private_b64u) for a fresh VAPID key pair."""
    k = ec.generate_private_key(ec.SECP256R1())
    pub = k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    priv = k.private_numbers().private_value.to_bytes(32, "big")
    return b64u(pub), b64u(priv)


def _private_key(priv_b64u: str):
    return ec.derive_private_key(int.from_bytes(b64u_dec(priv_b64u), "big"), ec.SECP256R1())


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def encrypt(payload: bytes, p256dh_b64u: str, auth_b64u: str, salt: bytes = None, as_key=None) -> bytes:
    """aes128gcm body for one subscription (RFC 8291). salt / as_key only for tests."""
    ua_public = b64u_dec(p256dh_b64u)
    auth_secret = b64u_dec(auth_b64u)
    as_key = as_key or ec.generate_private_key(ec.SECP256R1())
    as_public = as_key.public_key().public_bytes(serialization.Encoding.X962,
                                                 serialization.PublicFormat.UncompressedPoint)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = as_key.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack("!L", 4096) + bytes([len(as_public)]) + as_public + ciphertext


def vapid_header(endpoint: str, priv_b64u: str, pub_b64u: str, subject: str) -> str:
    """'vapid t=<signed JWT>, k=<public key>' for the push service at endpoint."""
    u = urlparse(endpoint)
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64u(json.dumps({"aud": "%s://%s" % (u.scheme, u.netloc), "exp": int(time.time()) + 12 * 3600,
                              "sub": subject}, separators=(",", ":")).encode())
    signing_input = (head + "." + claims).encode()
    der = _private_key(priv_b64u).sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    sig = b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return "vapid t=%s.%s, k=%s" % (head + "." + claims, sig, pub_b64u)


def send(sub: dict, data: dict, pub_b64u: str, priv_b64u: str, subject: str, timeout: float = 8.0):
    """Send data (dict, sent as JSON) to one subscription
    {"endpoint", "keys": {"p256dh", "auth"}}. Returns the HTTP status code;
    404 or 410 means the subscription is gone and should be deleted."""
    body = encrypt(json.dumps(data).encode(), sub["keys"]["p256dh"], sub["keys"]["auth"])
    r = requests.post(sub["endpoint"], data=body, timeout=timeout, headers={
        "Authorization": vapid_header(sub["endpoint"], priv_b64u, pub_b64u, subject),
        "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
        "TTL": "3600", "Urgency": "high"})
    return r.status_code
