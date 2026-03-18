"""Tests for ProtonMail PGP key management and message decryption."""

from __future__ import annotations

import pgpy
from pgpy.constants import (
    CompressionAlgorithm,
    HashAlgorithm,
    KeyFlags,
    PubKeyAlgorithm,
    SymmetricKeyAlgorithm,
)
import pytest

from email_mcp.crypto import DecryptionError, ProtonKeyRing, derive_mailbox_passphrase


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_key(passphrase: str, name: str, email: str) -> tuple[str, str]:
    """Generate a PGPy key pair. Returns (armored_private_key, passphrase)."""
    key = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    uid = pgpy.PGPUID.new(name, email=email)
    key.add_uid(uid, usage={
        KeyFlags.EncryptCommunications,
        KeyFlags.EncryptStorage,
    }, hashes=[HashAlgorithm.SHA256],
       ciphers=[SymmetricKeyAlgorithm.AES256],
       compression=[CompressionAlgorithm.Uncompressed])
    key.protect(passphrase, SymmetricKeyAlgorithm.AES256, HashAlgorithm.SHA256)
    return str(key), passphrase


def _encrypt_to_armored(plaintext: str, armored_key: str) -> str:
    """Encrypt plaintext to a key. Returns armored PGP message."""
    key, _ = pgpy.PGPKey.from_blob(armored_key)
    msg = pgpy.PGPMessage.new(plaintext)
    return str(key.pubkey.encrypt(msg))


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def key_pair() -> tuple[str, str]:
    return _make_key("test-passphrase-1234567890abcdef", "Test User", "test@example.com")


@pytest.fixture()
def second_key_pair() -> tuple[str, str]:
    return _make_key("second-passphrase-456789abcdefgh", "Second User", "second@example.com")


# ── derive_mailbox_passphrase ────────────────────────────────────────────────


class TestDeriveMailboxPassphrase:
    def test_returns_string(self):
        import base64
        salt_b64 = base64.b64encode(b"A" * 16).decode()
        result = derive_mailbox_passphrase("mypassword", salt_b64)
        assert isinstance(result, str)
        assert len(result) == 31

    def test_deterministic(self):
        import base64
        salt_b64 = base64.b64encode(b"B" * 16).decode()
        a = derive_mailbox_passphrase("password123", salt_b64)
        b = derive_mailbox_passphrase("password123", salt_b64)
        assert a == b

    def test_different_passwords_differ(self):
        import base64
        salt_b64 = base64.b64encode(b"C" * 16).decode()
        a = derive_mailbox_passphrase("password1", salt_b64)
        b = derive_mailbox_passphrase("password2", salt_b64)
        assert a != b

    def test_different_salts_differ(self):
        import base64
        salt_a = base64.b64encode(b"D" * 16).decode()
        salt_b = base64.b64encode(b"E" * 16).decode()
        a = derive_mailbox_passphrase("password", salt_a)
        b = derive_mailbox_passphrase("password", salt_b)
        assert a != b


# ── ProtonKeyRing ────────────────────────────────────────────────────────────


class TestProtonKeyRing:
    def test_init_loads_key(self, key_pair):
        armored, passphrase = key_pair
        kr = ProtonKeyRing(armored, passphrase)
        assert kr is not None

    def test_init_wrong_passphrase(self, key_pair):
        armored, _ = key_pair
        with pytest.raises(Exception):
            ProtonKeyRing(armored, "wrong-passphrase")

    def test_decrypt_message(self, key_pair):
        armored, passphrase = key_pair
        kr = ProtonKeyRing(armored, passphrase)
        encrypted = _encrypt_to_armored("Hello, World!", armored)
        assert kr.decrypt(encrypted) == "Hello, World!"

    def test_decrypt_with_address_key(self, key_pair, second_key_pair):
        user_armored, user_pass = key_pair
        addr_armored, addr_pass = second_key_pair

        kr = ProtonKeyRing(user_armored, user_pass)
        encrypted_token = _encrypt_to_armored(addr_pass, user_armored)
        kr.add_address_key(addr_armored, encrypted_token)

        encrypted = _encrypt_to_armored("Address key message", addr_armored)
        assert kr.decrypt(encrypted) == "Address key message"

    def test_decrypt_tries_user_key_first(self, key_pair):
        armored, passphrase = key_pair
        kr = ProtonKeyRing(armored, passphrase)
        encrypted = _encrypt_to_armored("User key message", armored)
        assert kr.decrypt(encrypted) == "User key message"

    def test_decrypt_no_matching_key(self, key_pair, second_key_pair):
        user_armored, user_pass = key_pair
        other_armored, _ = second_key_pair

        kr = ProtonKeyRing(user_armored, user_pass)
        encrypted = _encrypt_to_armored("Secret", other_armored)

        with pytest.raises(DecryptionError):
            kr.decrypt(encrypted)

    def test_decrypt_corrupt_blob(self, key_pair):
        armored, passphrase = key_pair
        kr = ProtonKeyRing(armored, passphrase)
        with pytest.raises(Exception):
            kr.decrypt("not a pgp message at all")
