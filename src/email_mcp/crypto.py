"""ProtonMail PGP key management and message decryption.

Handles the full decryption chain:
1. Derive mailbox passphrase from password + KeySalt (bcrypt)
2. Load and unlock user/address PGP private keys
3. Decrypt message bodies encrypted to those keys
"""

from __future__ import annotations

import base64

import bcrypt
import pgpy

from email_mcp.srp import _BCRYPT_B64, _STD_B64


class DecryptionError(Exception):
    """No available key could decrypt the message."""


def derive_mailbox_passphrase(password: str, key_salt_b64: str) -> str:
    """Derive the mailbox passphrase from login password and KeySalt.

    ProtonMail's key passphrase derivation:
      1. Base64-decode the KeySalt (16 bytes)
      2. Encode salt for bcrypt: base64 with bcrypt alphabet, strip padding (22 chars)
      3. bcrypt(password, "$2y$10$" + encoded_salt)
      4. Take the last 31 characters of the bcrypt hash as the passphrase

    This is different from SRP's _hash_password which applies PMHash expansion.
    """
    salt = base64.b64decode(key_salt_b64)
    # Encode salt with bcrypt's custom base64 alphabet, strip padding
    encoded_salt = (
        base64.b64encode(salt).translate(bytes.maketrans(_STD_B64, _BCRYPT_B64)).rstrip(b"=")
    )
    hashed = bcrypt.hashpw(password.encode("utf-8"), b"$2y$10$" + encoded_salt)
    return hashed.decode("utf-8")[-31:]


class ProtonKeyRing:
    """Loads and caches unlocked PGP keys for message decryption.

    Usage:
        kr = ProtonKeyRing(user_key_armored, passphrase)
        kr.add_address_key(addr_key_armored, encrypted_token)
        plaintext = kr.decrypt(encrypted_body)
    """

    def __init__(self, user_key_armored: str, passphrase: str) -> None:
        """Load and unlock the primary user key.

        Args:
            user_key_armored: Armored PGP private key from GET /core/v4/users.
            passphrase: Mailbox passphrase from derive_mailbox_passphrase().
        """
        self._user_key = self._load_and_unlock(user_key_armored, passphrase)
        self._address_keys: list[pgpy.PGPKey] = []
        self._keys_by_email: dict[str, pgpy.PGPKey] = {}

    def signing_key_for(self, email: str) -> pgpy.PGPKey:
        """Return the private key for a specific email address.

        Falls back to the first address key, then user key.
        """
        key = self._keys_by_email.get(email.lower())
        if key:
            return key
        return self._address_keys[0] if self._address_keys else self._user_key

    def add_address_key(self, armored_key: str, encrypted_token: str, email: str = "") -> None:
        """Add an address key. The token (passphrase) is encrypted with the user key.

        Args:
            armored_key: Armored PGP private key from the address.
            encrypted_token: PGP-encrypted passphrase for unlocking the address key.
            email: Email address this key belongs to (for signing key lookup).
        """
        # Decrypt the token using the user key to get the address key passphrase
        token_passphrase = self._decrypt_with_key(self._user_key, encrypted_token)
        addr_key = self._load_and_unlock(armored_key, token_passphrase)
        self._address_keys.append(addr_key)
        if email:
            self._keys_by_email[email.lower()] = addr_key

    def decrypt(self, armored_pgp_message: str) -> str:
        """Decrypt a PGP-encrypted message body.

        Tries the user key first, then each address key.
        Raises DecryptionError if no key can decrypt.
        """
        # Try user key first
        try:
            return self._decrypt_with_key(self._user_key, armored_pgp_message)
        except Exception:
            pass

        # Try address keys
        for addr_key in self._address_keys:
            try:
                return self._decrypt_with_key(addr_key, armored_pgp_message)
            except Exception:
                continue

        raise DecryptionError("No available key could decrypt the message")

    @staticmethod
    def _load_and_unlock(armored_key: str, passphrase: str) -> pgpy.PGPKey:
        """Load an armored PGP key and verify it can be unlocked."""
        key, _ = pgpy.PGPKey.from_blob(armored_key)
        # Verify the passphrase works by doing a test unlock
        with key.unlock(passphrase):
            pass
        # Store passphrase for later use
        key._passphrase = passphrase
        return key

    def decrypt_binary(self, data: bytes) -> bytes:
        """Decrypt binary PGP data (e.g., attachment content).

        Tries user key first, then address keys.
        Returns raw decrypted bytes.
        """
        msg = pgpy.PGPMessage.from_blob(data)

        for key in [self._user_key] + self._address_keys:
            try:
                with key.unlock(key._passphrase):  # type: ignore[attr-defined]
                    decrypted = key.decrypt(msg)
                result = decrypted.message
                if isinstance(result, str):
                    return result.encode("utf-8")
                return bytes(result)
            except Exception:
                continue

        raise DecryptionError("No available key could decrypt the data")

    def decrypt_session_key(self, key_packets_b64: str) -> tuple[bytes, pgpy.PGPMessage]:
        """Decrypt a session key from base64-encoded KeyPackets.

        ProtonMail attachments are encrypted with a session key.
        The KeyPackets field contains the session key encrypted to the user's public key.

        Returns the decrypted session key info needed for attachment decryption.
        """
        key_packets = base64.b64decode(key_packets_b64)
        msg = pgpy.PGPMessage.from_blob(key_packets)

        for key in [self._user_key] + self._address_keys:
            try:
                with key.unlock(key._passphrase):  # type: ignore[attr-defined]
                    # Extract the session key by decrypting the key packet
                    sk = msg._sessionkeys[0]
                    subkeys = list(key.subkeys.values())
                    if subkeys:
                        alg, session_key = sk.decrypt_sk(subkeys[0]._key)
                    else:
                        alg, session_key = sk.decrypt_sk(key._key)
                    return session_key, msg
            except Exception:
                continue

        raise DecryptionError("Could not decrypt session key")

    @staticmethod
    def _decrypt_with_key(key: pgpy.PGPKey, armored_message: str) -> str:
        """Decrypt an armored PGP message with the given key."""
        msg = pgpy.PGPMessage.from_blob(armored_message)
        with key.unlock(key._passphrase):  # type: ignore[attr-defined]
            try:
                decrypted = key.decrypt(msg)
            except Exception as e:
                raise DecryptionError(str(e)) from e
        return str(decrypted.message)
