import os
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789abcdef")
os.environ.setdefault("PUBLIC_HOST", "ci.local")
os.environ.setdefault("CF_ACCESS_TEAM_DOMAIN", "https://ci.cloudflareaccess.com")
os.environ.setdefault("CF_ACCESS_AUD", "ci-audience-0123456789abcdef")
os.environ.setdefault("ADMIN_EMAILS", "admin@rwu.edu")
_temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_temp_db.close()
os.environ["DATABASE_PATH"] = _temp_db.name

import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

import core  # noqa: E402
from portal_app import app  # noqa: E402


class AccessAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_key = cls.private_key.public_key()

    def setUp(self):
        self.client = app.test_client()

    def token(self, email="admin@rwu.edu", audience=None, issuer=None, token_type="app", exp_offset=300):
        now = int(time.time())
        payload = {
            "aud": [audience or core.CF_ACCESS_AUD],
            "email": email,
            "exp": now + exp_offset,
            "iat": now,
            "nbf": now - 1,
            "iss": issuer or core.CF_ACCESS_TEAM_DOMAIN,
            "type": token_type,
            "sub": f"access-sub:{email}",
        }
        return jwt.encode(payload, self.private_key, algorithm="RS256", headers={"kid": "test-key"})

    def verified_claims(self, token):
        return core.verify_access_token(token, signing_key=self.public_key)

    def test_valid_rwu_identity_verifies(self):
        claims = self.verified_claims(self.token())
        self.assertTrue(core.access_identity_allowed(claims))

    def test_wrong_audience_rejected(self):
        with self.assertRaises(jwt.InvalidAudienceError):
            self.verified_claims(self.token(audience="wrong-audience"))

    def test_wrong_issuer_rejected(self):
        with self.assertRaises(jwt.InvalidIssuerError):
            self.verified_claims(self.token(issuer="https://evil.cloudflareaccess.com"))

    def test_expired_token_rejected(self):
        with self.assertRaises(jwt.ExpiredSignatureError):
            self.verified_claims(self.token(exp_offset=-10))

    def test_non_rwu_identity_rejected(self):
        claims = self.verified_claims(self.token(email="person@gmail.com"))
        self.assertFalse(core.access_identity_allowed(claims))

    def test_service_token_rejected_for_user_login(self):
        claims = self.verified_claims(self.token(token_type="service"))
        self.assertFalse(core.access_identity_allowed(claims))

    def test_missing_access_header_fails_closed(self):
        response = self.client.get("/", headers={"Host": "ci.local"})
        self.assertEqual(response.status_code, 403)

    def test_valid_access_header_provisions_bootstrap_admin(self):
        token = self.token()
        with patch.object(core._access_jwks, "get_signing_key_from_jwt", return_value=self.public_key):
            response = self.client.get(
                "/dashboard",
                headers={"Host": "ci.local", "Cf-Access-Jwt-Assertion": token},
            )
        self.assertEqual(response.status_code, 200)
        with app.app_context():
            user = core.db().execute("SELECT * FROM users WHERE email='admin@rwu.edu'").fetchone()
            self.assertIsNotNone(user)
            self.assertEqual(user["role"], "ADMIN")


if __name__ == "__main__":
    unittest.main()
