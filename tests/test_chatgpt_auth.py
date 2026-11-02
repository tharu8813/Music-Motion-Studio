"""Official OAuth loopback flow with real signatures and no external requests."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

from PySide6.QtCore import QSettings

from app.services.chatgpt_auth_service import ChatGPTAuthService, ISSUER, TOKEN_URL
from app.services.lyrics_translation_service import TranslationCancelled, TranslationError
from app.services.translation_credentials import TranslationCredentials, _SESSION


class ChatGPTAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        cls.jwt = jwt
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key())), "kid": "test", "alg": "RS256"}

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = QSettings(str(Path(self.temp.name) / "oauth.ini"), QSettings.Format.IniFormat)
        self.credentials = TranslationCredentials(self.settings)
        _SESSION.clear()
        self.params = {}
        self.requests = []
        self.bad_nonce = False
        self.wrong_audience = False
        self.scope = "openid email profile offline_access resource.invoke chatgpt.tokens.use.direct"

    def browser(self, url):
        self.params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        callback = self.params["redirect_uri"] + "?" + urlencode({"state": self.params["state"],
            "code": "test-code", "client_id": "oaiapp_test"})
        def deliver():
            with urlopen(callback, timeout=5) as response:
                response.read()
        self.callback_thread = Thread(target=deliver, daemon=True)
        self.callback_thread.start()
        return True

    def transport(self, url, body=None, headers=None, **kwargs):
        self.requests.append((url, body))
        if url == TOKEN_URL and body.get("grant_type") == "refresh_token":
            return {"access_token": "renewed", "refresh_token": "rotated", "expires_in": 3600}
        if url == TOKEN_URL:
            self.assertEqual(body["client_id"], "oaiapp_test")
            self.assertEqual(body["redirect_uri"], self.params["redirect_uri"])
            self.assertTrue(body["code_verifier"])
            token = self.jwt.encode({"iss": ISSUER, "aud": "wrong" if self.wrong_audience else "oaiapp_test",
                "sub": "user-test", "email": "test@example.com", "iat": time.time(), "exp": time.time() + 3600,
                "nonce": "wrong" if self.bad_nonce else self.params["nonce"]}, self.key,
                algorithm="RS256", headers={"kid": "test"})
            return {"id_token": token, "access_token": "access", "refresh_token": "refresh",
                    "expires_in": 3600, "scope": self.scope}
        if url.endswith("jwks.json"):
            return {"keys": [self.jwk]}
        if url.endswith("openid-configuration"):
            return {"revocation_endpoint": ISSUER + "/revoke"}
        return {}

    def auth(self):
        return ChatGPTAuthService(self.credentials, self.settings, self.transport)

    def test_dynamic_registration_pkce_verified_identity_and_stable_host(self):
        with patch("app.services.chatgpt_auth_service.webbrowser.open", self.browser):
            profile = self.auth().sign_in(Event())
        self.callback_thread.join(2)
        self.assertEqual(profile["sub"], "user-test")
        self.assertEqual(profile["client_id"], "oaiapp_test")
        self.assertEqual(self.params["client_id"], "dynamic_agent_client")
        self.assertEqual(self.params["code_challenge_method"], "S256")
        self.assertEqual(self.params["agent_name_hint"], "Music Motion Studio")
        self.assertEqual(self.params["ext_agent_host_id"], self.settings.value("translation/chatgpt_host_id"))
        self.assertTrue(self.params["ext_agent_host_id"].startswith("urn:uuid:"))
        self.assertEqual(self.credentials.load("chatgpt")["access_token"], "access")
        self.settings.sync()
        self.assertNotIn("access_token", Path(self.settings.fileName()).read_text())

    def test_invalid_nonce_and_audience_never_save_credentials(self):
        for attribute in ("bad_nonce", "wrong_audience"):
            with self.subTest(attribute=attribute):
                self.bad_nonce = self.wrong_audience = False
                setattr(self, attribute, True)
                with patch("app.services.chatgpt_auth_service.webbrowser.open", self.browser):
                    with self.assertRaises(TranslationError):
                        self.auth().sign_in(Event())
                self.callback_thread.join(2)
                self.assertEqual(self.credentials.load("chatgpt"), {})

    def test_identity_without_plan_permission_cannot_translate(self):
        self.scope = "openid email profile"
        with patch("app.services.chatgpt_auth_service.webbrowser.open", self.browser):
            with self.assertRaisesRegex(TranslationError, "authorized"):
                self.auth().sign_in(Event())
        self.callback_thread.join(2)
        self.assertEqual(self.credentials.load("chatgpt"), {})

    def test_refresh_uses_issued_client_and_saves_rotated_token(self):
        self.credentials.save("chatgpt", {"client_id": "oaiapp_test", "access_token": "old",
            "refresh_token": "refresh", "scope": self.scope, "expires_at": time.time() - 1})
        self.assertEqual(self.auth().access_token(), "renewed")
        self.assertEqual(self.requests[0][1]["client_id"], "oaiapp_test")
        self.assertEqual(self.credentials.load("chatgpt")["refresh_token"], "rotated")

    def test_signout_failure_still_clears_local_credentials(self):
        self.credentials.save("chatgpt", {"client_id": "oaiapp_test", "refresh_token": "refresh"})
        auth = self.auth()
        auth.request = lambda *a, **kw: (_ for _ in ()).throw(TranslationError("offline"))
        with self.assertRaisesRegex(TranslationError, "Disconnected locally"):
            auth.sign_out()
        self.assertEqual(self.credentials.load("chatgpt"), {})

    def test_cancelled_refresh_or_model_load_does_not_request(self):
        self.credentials.save("chatgpt", {"client_id": "oaiapp_test", "access_token": "old",
            "refresh_token": "refresh", "scope": self.scope, "expires_at": time.time() - 1})
        cancel = Event()
        cancel.set()
        for action in (self.auth().access_token, self.auth().models):
            with self.assertRaises(TranslationCancelled):
                action(cancel)
        self.assertEqual(self.requests, [])

    def test_cancel_after_refresh_prevents_model_request(self):
        self.credentials.save("chatgpt", {"client_id": "oaiapp_test", "access_token": "old",
            "refresh_token": "refresh", "scope": self.scope, "expires_at": time.time() - 1})
        cancel = Event()
        def transport(*args, **kwargs):
            cancel.set()
            return self.transport(*args, **kwargs)
        auth = ChatGPTAuthService(self.credentials, self.settings, transport)
        with self.assertRaises(TranslationCancelled):
            auth.models(cancel)
        self.assertEqual(len(self.requests), 1)


if __name__ == "__main__":
    unittest.main()
