"""ProtonMail SRP authentication — pure Python, no OpenSSL/GPG required.

Extracted and adapted from proton-python-client (MIT licence).
Implements the ProtonMail SRP-6a variant with bcrypt+PMHash password hashing.
"""

from __future__ import annotations

import base64
import hashlib
import os

import bcrypt

# ── PMHash (4× SHA-512 expansion) ────────────────────────────────────────────


class _PMHash:
    digest_size = 256

    def __init__(self, b: bytes = b"") -> None:
        self.b = b

    def update(self, b: bytes) -> None:
        self.b += b

    def digest(self) -> bytes:
        return (
            hashlib.sha512(self.b + b"\x00").digest()
            + hashlib.sha512(self.b + b"\x01").digest()
            + hashlib.sha512(self.b + b"\x02").digest()
            + hashlib.sha512(self.b + b"\x03").digest()
        )


def _pmhash(b: bytes = b"") -> _PMHash:
    return _PMHash(b)


# ── Byte / integer helpers ────────────────────────────────────────────────────


def _long_length(n: int) -> int:
    return (n.bit_length() + 7) // 8


def _bytes_to_long(s: bytes) -> int:
    return int.from_bytes(s, "little")


def _long_to_bytes(n: int) -> bytes:
    return n.to_bytes(_long_length(n), "little")


def _get_random_of_length(nbytes: int) -> int:
    offset = (nbytes * 8) - 1
    return _bytes_to_long(os.urandom(nbytes)) | (1 << offset)


# ── Password hashing ──────────────────────────────────────────────────────────

_BCRYPT_B64 = b"./ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_STD_B64 = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def _bcrypt_b64_encode(s: bytes) -> bytes:
    return base64.b64encode(s).translate(bytes.maketrans(_STD_B64, _BCRYPT_B64))


def _hash_password(password: bytes, salt: bytes, modulus: bytes, version: int) -> bytes:
    if version not in (3, 4):
        raise ValueError(f"Unsupported auth version: {version}")
    salt = (salt + b"proton")[:16]
    salt = _bcrypt_b64_encode(salt)[:22]
    hashed = bcrypt.hashpw(password, b"$2y$10$" + salt)
    return _pmhash(hashed + modulus).digest()


# ── SRP User ──────────────────────────────────────────────────────────────────


class SRPUser:
    def __init__(self, password: str, modulus: bytes) -> None:
        self._N = _bytes_to_long(modulus)
        self._g = 2
        self._password = password.encode()
        self._modulus = modulus

        # k = PMHash(g || N)  (little-endian, width-padded)
        w = _long_length(self._N)
        h = _pmhash()
        h.update(self._g.to_bytes(w, "little"))
        h.update(self._N.to_bytes(w, "little"))
        self._k = _bytes_to_long(h.digest())

        self._a = _get_random_of_length(32)
        self._A = pow(self._g, self._a, self._N)

        self._M: bytes | None = None
        self._K: bytes | None = None
        self._expected_server_proof: bytes | None = None
        self._authenticated = False

    def authenticated(self) -> bool:
        return self._authenticated

    def get_challenge(self) -> bytes:
        return _long_to_bytes(self._A)

    def process_challenge(self, salt: bytes, server_ephemeral: bytes, version: int) -> bytes | None:
        B = _bytes_to_long(server_ephemeral)  # noqa: N806 — SRP standard notation
        if (B % self._N) == 0:
            return None

        # u = PMHash(A || B)
        h = _pmhash()
        h.update(_long_to_bytes(self._A))
        h.update(_long_to_bytes(B))
        u = _bytes_to_long(h.digest())
        if u == 0:
            return None

        x = _bytes_to_long(_hash_password(self._password, salt, self._modulus, version))
        v = pow(self._g, x, self._N)
        S = pow(B - self._k * v, self._a + u * x, self._N)  # noqa: N806
        K = _long_to_bytes(S)  # noqa: N806

        # M = PMHash(A || B || K)
        h = _pmhash()
        h.update(_long_to_bytes(self._A))
        h.update(_long_to_bytes(B))
        h.update(K)
        self._M = h.digest()

        # expected server proof = PMHash(A || M || K)
        h = _pmhash()
        h.update(_long_to_bytes(self._A))
        h.update(self._M)
        h.update(K)
        self._expected_server_proof = h.digest()
        self._K = K

        return self._M

    def verify_session(self, server_proof: bytes) -> None:
        if self._expected_server_proof == server_proof:
            self._authenticated = True


# ── Modulus extraction ────────────────────────────────────────────────────────


def extract_modulus(pgp_signed_modulus: str) -> bytes:
    """Extract the raw modulus bytes from a PGP-signed modulus string.

    ProtonMail returns the modulus as a PGP cleartext signature. We skip
    signature verification (we're trusting the TLS connection) and just
    extract the base64 payload.
    """
    # The modulus is the content between the PGP header and the signature
    lines = pgp_signed_modulus.strip().splitlines()
    payload_lines = []
    in_body = False
    for line in lines:
        if line.startswith("-----BEGIN PGP SIGNED MESSAGE-----"):
            in_body = False
            continue
        if line.startswith("Hash:"):
            in_body = True
            continue
        if line.startswith("-----BEGIN PGP SIGNATURE-----"):
            break
        if in_body and line.strip():
            payload_lines.append(line.strip())

    return base64.b64decode("".join(payload_lines))
