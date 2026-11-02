"""Official Sign in with ChatGPT OSS flow (PKCE + validated OpenID identity).

Uses this app's own dynamic client; never reads another app's credentials.
https://developers.openai.com/siwc/token-sharing-open-source/sign-in
"""
from __future__ import annotations

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import secrets
from threading import Event, Lock
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID, uuid4
import webbrowser

from PySide6.QtCore import QSettings

from app.services.lyrics_translation_service import TranslationError, check_cancel, request_json
from app.services.translation_credentials import TranslationCredentials

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
_REFRESH_LOCK = Lock()


class ChatGPTAuthService:
    def __init__(self, credentials=None, settings=None, transport=request_json):
        self.settings = settings if settings is not None else QSettings()
        self.credentials = credentials or TranslationCredentials(self.settings)
        self.request = transport

    def sign_in(self, cancel: Event, *, remember=True) -> dict:
        check_cancel(cancel)
        try:
            import jwt
        except ImportError:
            raise TranslationError("ChatGPT 로그인에 필요한 PyJWT[crypto]를 설치하세요. / Install PyJWT[crypto] to sign in.") from None
        host_id = self.settings.value("translation/chatgpt_host_id", "")
        if not host_id:
            host_id = "urn:uuid:" + str(uuid4())
            self.settings.setValue("translation/chatgpt_host_id", host_id)
        else:
            try:
                # Earlier drafts saved a bare UUID; keep the identity while
                # normalizing it to the accepted URI form.
                host_id = "urn:uuid:" + str(UUID(str(host_id)))
                self.settings.setValue("translation/chatgpt_host_id", host_id)
            except ValueError:
                pass
        previous = self.credentials.load("chatgpt")
        client = previous.get("client_id") or self.settings.value("translation/chatgpt_client_id", "dynamic_agent_client")
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        callback = {}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                params = parse_qs(parsed.query)
                if parsed.path != "/auth/callback" or not secrets.compare_digest(params.get("state", [""])[0], state):
                    self.send_error(400)
                    return
                callback.update({key: value[0] for key, value in params.items()})
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'none'")
                self.end_headers()
                self.wfile.write("Music Motion Studio로 돌아가세요. / Return to Music Motion Studio.".encode())
        class CallbackServer(HTTPServer):
            def get_request(self):
                connection, address = super().get_request()
                connection.settimeout(1)
                return connection, address
        with CallbackServer(("127.0.0.1", 0), Handler) as server:
            server.timeout = .25
            redirect = f"http://127.0.0.1:{server.server_port}/auth/callback"
            params = {"client_id": client, "ext_agent_host_id": host_id, "response_type": "code",
                      "redirect_uri": redirect, "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
                      "resource": RESOURCE, "state": state, "nonce": nonce,
                      "code_challenge_method": "S256", "code_challenge": challenge}
            if client == "dynamic_agent_client":
                params["agent_name_hint"] = "Music Motion Studio"
            if not webbrowser.open(ISSUER + "/api/accounts/authorize?" + urlencode(params)):
                raise TranslationError("로그인 브라우저를 열 수 없습니다. / Cannot open the sign-in browser.")
            deadline = time.monotonic() + 180
            while not callback:
                check_cancel(cancel)
                if time.monotonic() > deadline:
                    raise TranslationError("로그인 시간이 초과되었습니다. / Sign-in timed out.")
                server.handle_request()
        check_cancel(cancel)
        if callback.get("error") or not callback.get("code"):
            raise TranslationError("로그인이 취소되었거나 승인되지 않았습니다. / Sign-in was declined.")
        issued = callback.get("client_id", client)
        if issued == "dynamic_agent_client" or (client != "dynamic_agent_client" and issued != client):
            raise TranslationError("ChatGPT 앱 등록 정보를 확인할 수 없습니다. / Invalid ChatGPT client registration.")
        # Registration is distinct from an authenticated session. Retain the
        # issued ID if exchange fails, without granting or storing any identity.
        self.settings.setValue("translation/chatgpt_client_id", issued)
        tokens = self.request(TOKEN_URL, {"grant_type": "authorization_code", "client_id": issued,
            "code": callback["code"], "code_verifier": verifier, "redirect_uri": redirect, "resource": RESOURCE}, form=True, cancel=cancel)
        try:
            token = tokens["id_token"]
            # Fetch a fresh JWKS through our bounded, no-redirect transport.
            jwks = self.request(ISSUER + "/.well-known/jwks.json", cancel=cancel)
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in {"RS256", "ES256"}:
                raise ValueError()
            key = next(key for key in jwks["keys"] if key.get("kid") == header.get("kid"))
            identity = jwt.decode(token, jwt.PyJWK.from_dict(key).key, algorithms=[header["alg"]],
                audience=issued, issuer=ISSUER, leeway=5, options={"require": ["exp", "iat", "sub", "nonce"]})
            if not isinstance(identity["sub"], str) or not identity["sub"] or identity["nonce"] != nonce:
                raise ValueError()
            if previous and identity["sub"] != previous.get("sub"):
                raise TranslationError("다른 ChatGPT 계정입니다. 연결 해제 후 다시 로그인하세요. / Disconnect before switching ChatGPT accounts.")
            if "chatgpt.tokens.use.direct" not in tokens.get("scope", "").split():
                raise TranslationError("ChatGPT 구독 사용 권한이 승인되지 않았습니다. / ChatGPT plan usage was not authorized.")
            if not isinstance(tokens.get("access_token"), str) or not tokens["access_token"]:
                raise ValueError()
            profile = {**tokens, "client_id": issued, "sub": identity["sub"], "email": identity.get("email", ""),
                       "expires_at": time.time() + float(tokens.get("expires_in", 3600)), "remember": remember}
        except TranslationError:
            raise
        except Exception:
            raise TranslationError("ChatGPT 로그인 응답의 서명·계정 정보를 검증하지 못했습니다. / ChatGPT identity validation failed.") from None
        check_cancel(cancel)
        self.credentials.save("chatgpt", profile, remember=remember)
        return profile

    def access_token(self, cancel: Event | None = None) -> str:
        with _REFRESH_LOCK:
            check_cancel(cancel)
            profile = self.credentials.load("chatgpt")
            if not profile or "chatgpt.tokens.use.direct" not in profile.get("scope", "").split():
                raise TranslationError("ChatGPT에 로그인하고 구독 사용을 허용하세요. / Sign in and enable ChatGPT plan usage.")
            if float(profile.get("expires_at", 0)) < time.time() + 60:
                if not profile.get("refresh_token"):
                    raise TranslationError("ChatGPT에 다시 로그인하세요. / Sign in to ChatGPT again.")
                renewed = self.request(TOKEN_URL, {"grant_type": "refresh_token", "client_id": profile["client_id"],
                    "refresh_token": profile["refresh_token"], "resource": RESOURCE}, form=True, cancel=cancel)
                if not isinstance(renewed.get("access_token"), str) or not renewed["access_token"]:
                    raise TranslationError("ChatGPT 로그인을 갱신하지 못했습니다. / Cannot refresh ChatGPT sign-in.")
                profile.update(renewed)
                profile["expires_at"] = time.time() + float(renewed.get("expires_in", 3600))
                profile["remember"] = True
                self.credentials.save("chatgpt", profile, remember=True)
            check_cancel(cancel)
            if "chatgpt.tokens.use.direct" not in profile.get("scope", "").split():
                raise TranslationError("ChatGPT 구독 사용 권한이 없습니다. 다시 로그인하세요. / ChatGPT plan permission is missing; sign in again.")
            return profile["access_token"]

    def models(self, cancel: Event | None = None) -> list[tuple[str, str]]:
        data = self.request(RESOURCE + "/models", headers={"Authorization": "Bearer " + self.access_token(cancel)}, cancel=cancel)
        check_cancel(cancel)
        return [(model["slug"], model.get("display_name", model["slug"])) for model in data.get("models", [])
                if model.get("visibility") == "list" and isinstance(model.get("slug"), str)]

    def profile(self):
        """Upgrade an existing session to restart-safe storage without a request."""
        profile = self.credentials.load("chatgpt")
        if profile and not profile.get("remember"):
            profile["remember"] = True
            self.credentials.save("chatgpt", profile, remember=True)
        return profile

    def sign_out(self, cancel: Event | None = None) -> None:
        profile = self.credentials.load("chatgpt")
        try:
            if profile.get("refresh_token"):
                check_cancel(cancel)
                discovery = self.request(ISSUER + "/.well-known/openid-configuration", cancel=cancel)
                endpoint = discovery.get("revocation_endpoint", "")
                if urlsplit(endpoint).netloc != "auth.openai.com" or not endpoint.startswith("https://"):
                    raise TranslationError("ChatGPT 연결 해제 주소를 확인할 수 없습니다. / Invalid revocation endpoint.")
                self.request(endpoint, {"token": profile["refresh_token"], "token_type_hint": "refresh_token",
                    "client_id": profile["client_id"]}, form=True, cancel=cancel)
        except Exception:
            raise TranslationError("이 기기의 연결은 해제했습니다. 원격 해제를 확인하지 못했으므로 ChatGPT 설정에서도 앱 연결을 해제하세요. / Disconnected locally; remote revocation could not be confirmed. Disconnect the app in ChatGPT settings.") from None
        finally:
            self.credentials.clear("chatgpt")
            self.settings.remove("translation/chatgpt_client_id")
