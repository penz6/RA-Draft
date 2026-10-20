"""Credential encryption, compatibility, and rejection of altered ciphertext."""

import base64
import hashlib
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from test_admin_management import app
from groupme_bot import _cipher


class GroupMeCryptoTests(unittest.TestCase):
    def legacy_cipher(self):
        secret = app.config["SECRET_KEY"]
        if isinstance(secret, str):
            secret = secret.encode()
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret + b"\0groupme-bot").digest()))

    def test_new_credentials_use_hkdf_key_not_legacy_key(self):
        with app.app_context():
            plaintext = b"private-groupme-credential"
            encrypted = _cipher().encrypt(plaintext)
            secret = app.config["SECRET_KEY"].encode()
            key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                       info=b"ra-draft/groupme/credential-encryption/v2").derive(secret)
            self.assertEqual(Fernet(base64.urlsafe_b64encode(key)).decrypt(encrypted), plaintext)
            with self.assertRaises(InvalidToken):
                self.legacy_cipher().decrypt(encrypted)

    def test_existing_credentials_remain_readable_and_can_rotate(self):
        with app.app_context():
            legacy = self.legacy_cipher().encrypt(b"existing-bot-id")
            self.assertEqual(_cipher().decrypt(legacy), b"existing-bot-id")
            rotated = _cipher().rotate(legacy)
            self.assertEqual(_cipher().decrypt(rotated), b"existing-bot-id")
            with self.assertRaises(InvalidToken):
                self.legacy_cipher().decrypt(rotated)

    def test_fresh_randomized_encryption_and_tampering_rejected(self):
        with app.app_context():
            cipher = _cipher()
            first, second = cipher.encrypt(b"same-token"), cipher.encrypt(b"same-token")
            self.assertNotEqual(first, second)
            raw = bytearray(base64.urlsafe_b64decode(first))
            raw[len(raw) // 2] ^= 1
            with self.assertRaises(InvalidToken):
                cipher.decrypt(base64.urlsafe_b64encode(raw))

    def test_different_master_secret_cannot_decrypt(self):
        with app.app_context():
            token = _cipher().encrypt(b"private-token")
            with patch.dict(app.config, SECRET_KEY="different-random-master-secret-0123456789"):
                with self.assertRaises(InvalidToken):
                    _cipher().decrypt(token)

    def test_bytes_master_secret_is_supported(self):
        with app.app_context(), patch.dict(app.config, SECRET_KEY=b"byte-master-secret-0123456789abcdef"):
            cipher = _cipher()
            self.assertEqual(cipher.decrypt(cipher.encrypt(b"token")), b"token")


if __name__ == "__main__":
    unittest.main()
