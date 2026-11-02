"""Session credentials, with optional Windows user-bound DPAPI persistence."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import sys

from PySide6.QtCore import QSettings

from app.services.lyrics_translation_service import TranslationError

_SESSION: dict[tuple[str, str], dict] = {}


def _crypt(data: bytes, *, decrypt=False) -> bytes:
    if sys.platform != "win32":
        raise TranslationError("이 환경에서는 인증 정보의 영구 저장을 지원하지 않습니다. / Credential persistence requires Windows.")
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    api = ctypes.WinDLL("crypt32", use_last_error=True)
    fn = api.CryptUnprotectData if decrypt else api.CryptProtectData
    # No machine-wide flag: only this Windows user can decrypt the credential.
    if not fn(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise TranslationError("인증 정보를 보호하거나 읽을 수 없습니다. / Cannot protect or read credentials.")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel = ctypes.WinDLL("kernel32")
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(ctypes.cast(outgoing.data, ctypes.c_void_p))


class TranslationCredentials:
    def __init__(self, settings=None):
        self.settings = settings if settings is not None else QSettings()

    def load(self, provider: str) -> dict:
        session_key = (self.settings.fileName(), provider)
        if session_key in _SESSION:
            return dict(_SESSION[session_key])
        encoded = self.settings.value("translation/secrets/" + provider, "")
        if not encoded:
            return {}
        try:
            value = json.loads(_crypt(base64.b64decode(encoded), decrypt=True))
            return value if isinstance(value, dict) else {}
        except (ValueError, OSError, TranslationError):
            return {}

    def save(self, provider: str, value: dict, *, remember=False) -> None:
        if remember:
            protected = base64.b64encode(_crypt(json.dumps(value).encode())).decode()
            self.settings.setValue("translation/secrets/" + provider, protected)
        else:
            self.settings.remove("translation/secrets/" + provider)
        self.settings.sync()
        if self.settings.status() != QSettings.Status.NoError:
            raise TranslationError("인증 정보를 저장할 수 없습니다. 설정 폴더 권한을 확인하세요. / Cannot save credentials; check settings permissions.")
        _SESSION[(self.settings.fileName(), provider)] = dict(value)

    def clear(self, provider: str) -> None:
        _SESSION.pop((self.settings.fileName(), provider), None)
        self.settings.remove("translation/secrets/" + provider)
        self.settings.sync()
