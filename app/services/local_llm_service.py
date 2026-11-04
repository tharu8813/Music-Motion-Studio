"""Loopback-only LLM discovery, model management and managed Ollama runtime."""
from __future__ import annotations

import atexit
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
from time import monotonic
from tempfile import TemporaryDirectory
from threading import Event, Lock
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener
from zipfile import ZipFile, BadZipFile

from app.services.lyrics_translation_service import (TranslationError, check_cancel, validate_endpoint, _NoRedirect,
    request_json, LOCAL_LLM_PROVIDERS, CLOUD_LLM_PROVIDERS)
from app.services.local_translation_service import _HTTPSRedirect

RELEASE_URL = "https://api.github.com/repos/ollama/ollama/releases/latest"
_PROCESSES, _START_LOCK = [], Lock()

# Sizes are approximate downloads, not RAM requirements or quality guarantees.
MODEL_GUIDES = (
    {"id":"qwen2.5-coder:7b","size":"≈ 4.7 GB","focus":"coder","tier":"medium","lmstudio":"https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct-GGUF","url":"https://ollama.com/library/qwen2.5-coder","ko":["Qwen2.5 Coder 7B · 권장 시작","코딩 특화 · JSON 구조 요청을 시험하기 좋은 균형형","가사 번역의 자연스러움과 언어별 정확도는 직접 확인해야 합니다."],"en":["Qwen2.5 Coder 7B · suggested","Coding-focused, balanced choice for structured JSON tasks.","Verify lyric quality and language coverage on your own content."]},
    {"id":"qwen2.5-coder:3b","size":"≈ 1.9 GB","focus":"coder","tier":"small","lmstudio":"https://huggingface.co/Qwen/Qwen2.5-Coder-3B-Instruct-GGUF","url":"https://ollama.com/library/qwen2.5-coder","ko":["Qwen2.5 Coder 3B · 가볍게 시작","용량이 작아 낮은 사양에서 형식 지시를 빠르게 시험","복잡한 가사와 문맥·번역 정확도에 한계가 있을 수 있습니다."],"en":["Qwen2.5 Coder 3B · lightweight","Small coding model for testing format instructions.","Less reliable for complex lyric contexts and languages."]},
    {"id":"qwen2.5-coder:14b","size":"≈ 9.0 GB","focus":"coder","tier":"large","lmstudio":"https://huggingface.co/Qwen/Qwen2.5-Coder-14B-Instruct-GGUF","url":"https://ollama.com/library/qwen2.5-coder","ko":["Qwen2.5 Coder 14B · 더 큰 모델","코딩·JSON 작업을 위한 더 큰 모델","다운로드와 실행 메모리 부담이 커집니다."],"en":["Qwen2.5 Coder 14B · larger","Larger model for coding and structured output.","Greater download and RAM needs."]},
    {"id":"qwen2.5-coder:32b","size":"≈ 20 GB","focus":"coder","tier":"large","lmstudio":"","url":"https://ollama.com/library/qwen2.5-coder","ko":["Qwen2.5 Coder 32B · 고사양","대규모 코딩 특화 모델","가용 메모리와 속도를 반드시 확인하세요."],"en":["Qwen2.5 Coder 32B · demanding","Large coding-specialized model.","Check available RAM and speed."]},
    {"id":"qwen3-coder:30b","size":"≈ 19 GB","focus":"coder","tier":"large","lmstudio":"","url":"https://ollama.com/library/qwen3-coder","ko":["Qwen3 Coder 30B · 고사양","최신 계열 코딩 지시와 구조화 출력 비교용","가사 번역 품질은 별도 검증이 필요합니다."],"en":["Qwen3 Coder 30B · demanding","For coding instructions and structured-output comparisons.","Lyric quality requires independent testing."]},
    {"id":"devstral-small-2:24b","size":"≈ 15 GB","focus":"coder","tier":"large","lmstudio":"","url":"https://ollama.com/library/devstral-small-2","ko":["Devstral Small 2 24B · 다른 코딩 계열","코딩 작업 특화 모델 계열을 비교할 때","다국어 가사 번역 모델로 검증된 것은 아닙니다."],"en":["Devstral Small 2 24B · coding alternative","Alternative software-oriented model family.","Not validated for multilingual lyrics."]},
    {"id":"qwen3.5:4b","size":"≈ 3.4 GB","focus":"multilingual","tier":"small","lmstudio":"","url":"https://ollama.com/library/qwen3.5","ko":["Qwen3.5 4B · 다국어 경량","다국어 문맥을 중시할 때 비교할 범용 모델","코딩 전용은 아니며 JSON 준수 여부를 확인하세요."],"en":["Qwen3.5 4B · multilingual compact","General model for multilingual context.","Not coder-specific; test its JSON output."]},
    {"id":"qwen3.5:9b","size":"≈ 6.6 GB","focus":"multilingual","tier":"medium","lmstudio":"","url":"https://ollama.com/library/qwen3.5","ko":["Qwen3.5 9B · 다국어 균형","여러 언어의 가사와 구조화 지시를 함께 시험","메모리·속도와 언어별 편차를 고려하세요."],"en":["Qwen3.5 9B · multilingual balance","Try multilingual lyrics and structured instructions.","Consider RAM, speed, and uneven language quality."]},
    {"id":"qwen3.5:27b","size":"≈ 17 GB","focus":"multilingual","tier":"large","lmstudio":"","url":"https://ollama.com/library/qwen3.5","ko":["Qwen3.5 27B · 고사양 다국어","복잡한 다국어 번역을 시도할 범용 대형 모델","메모리 요구량이 크고 정확도를 보장하지 않습니다."],"en":["Qwen3.5 27B · multilingual large","Larger general model for complex multilingual tasks.","High memory needs; results are not guaranteed."]},
    {"id":"qwen3:4b","size":"≈ 2.5 GB","focus":"multilingual","tier":"small","lmstudio":"https://huggingface.co/Qwen/Qwen3-4B-GGUF","url":"https://ollama.com/library/qwen3","ko":["Qwen3 4B · 기존 경량","다국어 지시와 일반 번역용 범용 대안","작은 모델은 JSON 형식·문맥을 놓칠 수 있습니다."],"en":["Qwen3 4B · existing compact","Small general multilingual alternative.","May miss JSON rules or nuance."]},
    {"id":"qwen3:8b","size":"≈ 5.2 GB","focus":"multilingual","tier":"medium","lmstudio":"https://huggingface.co/Qwen/Qwen3-8B-GGUF","url":"https://ollama.com/library/qwen3","ko":["Qwen3 8B · 기존 모델","일반 다국어 지시를 시험할 균형형","코딩 전용이 아니며 가사 품질을 보장하지 않습니다."],"en":["Qwen3 8B · existing model","Balanced general multilingual alternative.","Not coder-specific; lyric quality is unverified."]},
    {"id":"gemma3:4b","size":"≈ 3.4 GB","focus":"multilingual","tier":"small","lmstudio":"https://huggingface.co/lmstudio-community/gemma-3-4b-it-GGUF","url":"https://ollama.com/library/gemma3","ko":["Gemma 3 4B · 다른 계열","다양한 언어를 지원하는 비교용 대안","언어별 품질과 이용 조건을 확인하세요."],"en":["Gemma 3 4B · alternative","Alternative multilingual family.","Language quality varies; check model terms."]},
)


def model_guide(name, korean=True):
    lowered = name.lower()
    guide = next((row for row in MODEL_GUIDES if lowered == row["id"] or lowered.startswith(row["id"] + "-")), None)
    if guide:
        return guide["ko" if korean else "en"][1:]
    return (("설치된 모델의 정보와 라이선스를 확인해 사용할 수 있습니다.",
        "이 모델의 가사 번역 성능은 검증하지 않았습니다. 언어 지원·JSON 출력·메모리 요구량을 확인하세요.") if korean else
        ("Inspect the installed model's metadata and license before use.",
         "Lyric translation quality is unverified. Check language support, JSON output and memory needs."))


def local_base(endpoint):
    endpoint = validate_endpoint(endpoint)
    if urlsplit(endpoint).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise TranslationError("로컬 LLM은 이 컴퓨터의 서버 주소만 사용할 수 있습니다. / Local LLMs require a loopback server URL.")
    return endpoint[:-3] if endpoint.endswith("/v1") else endpoint


def validate_local_model(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", name) or ".." in name:
        raise TranslationError("올바른 모델 이름을 입력하세요. / Enter a valid model name.")
    if "cloud" in name.lower().split(":")[-1]:
        raise TranslationError("클라우드 전용 모델은 클라우드 LLM에서 사용하세요. / Cloud models are not local LLMs.")
    return name


def _shutdown():
    for process in _PROCESSES:
        if process.poll() is None:
            process.terminate()


atexit.register(_shutdown)


class LocalLLMService:
    def __init__(self, provider="ollama", endpoint="http://localhost:11434/v1", key="", *, root=None):
        if provider not in LOCAL_LLM_PROVIDERS:
            raise TranslationError("지원하지 않는 로컬 LLM 실행 방식입니다. / Unsupported local LLM engine.")
        self.provider, self.base, self.key = provider, local_base(endpoint), key
        self.root = Path(root) if root is not None else Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PlaylistCanvas" / "local-llm"
        self._capabilities = {}

    def _request(self, path, body=None, *, method=None, cancel=None, stream=False, progress=None, timeout=45):
        check_cancel(cancel)
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        request = Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
            headers=headers, method=method)
        try:
            with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
                if stream:
                    success = False
                    while True:
                        check_cancel(cancel)
                        line = response.readline(65537)
                        if not line:
                            break
                        if len(line) > 65536:
                            raise ValueError("Oversized progress record")
                        row = json.loads(line)
                        if row.get("error"):
                            raise TranslationError("모델 다운로드에 실패했습니다. 모델 이름·디스크 공간을 확인하세요. / Model download failed; check its name and disk space.")
                        total, done = int(row.get("total", 0)), int(row.get("completed", 0))
                        if progress and total > 0:
                            progress(min(100, max(0, round(done / total * 100))), 100)
                        success = row.get("status") == "success"
                    check_cancel(cancel)
                    if not success:
                        raise TranslationError("모델 다운로드가 끝나기 전에 연결이 끊겼습니다. 다시 시도하면 이어받습니다. / Download interrupted; retry to resume.")
                    return {}
                data = response.read(8 * 1024 * 1024 + 1)
                if len(data) > 8 * 1024 * 1024:
                    raise ValueError("Oversized response")
                check_cancel(cancel)
                return json.loads(data) if data.strip() else {}
        except HTTPError as error:
            if error.code in {401, 403}:
                raise TranslationError("로컬 서버의 API 토큰을 확인하세요. / Check the local server's API token.") from None
            raise TranslationError(f"로컬 LLM 요청에 실패했습니다(HTTP {error.code}). 모델 이름과 서버 버전을 확인하세요. / Local LLM request failed (HTTP {error.code}); check model and server version.") from None
        except TranslationError:
            raise
        except (OSError, ValueError):
            raise TranslationError("로컬 LLM 서버에 연결할 수 없거나 응답이 올바르지 않습니다. 실행 구성요소 설치·시작 또는 서버 주소를 확인하세요. / Cannot reach the local LLM server; install/start the runtime or check the URL.") from None

    def list_models(self, cancel=None):
        data = self._request("/api/tags" if self.provider == "ollama" else "/api/v1/models", cancel=cancel, timeout=5)
        rows = []
        for item in data.get("models", []):
            name = item.get("name") if self.provider == "ollama" else item.get("key")
            if not isinstance(name, str) or not name:
                continue
            if self.provider == "lmstudio" and item.get("type") != "llm":
                continue
            if item.get("remote_host") or "cloud" in name.lower().split(":")[-1]:
                continue
            if self.provider == "ollama":
                cache_key = (name, item.get("digest"))
                if cache_key not in self._capabilities:
                    try:
                        metadata = self._request("/api/show", {"model": name}, cancel=cancel, timeout=5)
                        self._capabilities[cache_key] = (metadata.get("capabilities"), bool(metadata.get("remote_host")))
                    except TranslationError:
                        # Older servers may not expose capabilities. Keep their
                        # existing model list usable, without guessing a family.
                        self._capabilities[cache_key] = (None, False)
                capabilities, remote = self._capabilities[cache_key]
                if remote or capabilities is not None and "completion" not in capabilities:
                    continue
            details = item.get("details", {})
            rows.append({"id": name, "name": item.get("display_name", name),
                "size": item.get("size", item.get("size_bytes", 0)),
                "parameters": details.get("parameter_size", item.get("params_string", "")),
                "quantization": details.get("quantization_level", (item.get("quantization") or {}).get("name", "")),
                "instances": item.get("loaded_instances", []), "metadata": item})
        return sorted(rows, key=lambda row: row["id"].casefold())

    def discover(self, cancel=None):
        if self.provider == "ollama":
            return self.ensure_running(cancel=cancel)
        return self.list_models(cancel)

    def details(self, name, cancel=None):
        validate_local_model(name)
        if self.provider == "ollama":
            data = self._request("/api/show", {"model": name}, cancel=cancel)
            if data.get("remote_host"):
                raise TranslationError("이 모델은 클라우드에서 실행됩니다. 로컬 모델을 선택하세요. / Select an on-device model.")
            return data
        return next((row["metadata"] for row in self.list_models(cancel) if row["id"] == name), {})

    def download(self, name, cancel=None, progress=None):
        if self.provider == "lmstudio" and isinstance(name, str) and name.startswith("https://"):
            address = urlsplit(name)
            if (address.hostname != "huggingface.co" or address.username or address.password
                    or address.port or address.query or address.fragment
                    or not re.fullmatch(r"/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/?", address.path)
                    or ".." in address.path):
                raise TranslationError("허용된 Hugging Face 모델 주소를 입력하세요. / Enter a valid Hugging Face model URL.")
        else:
            validate_local_model(name)
        if self.provider == "ollama":
            self.ensure_running(cancel=cancel)
            self._request("/api/pull", {"model": name, "stream": True}, stream=True, cancel=cancel, progress=progress)
        else:
            data = self._request("/api/v1/models/download", {"model": name}, cancel=cancel)
            while data.get("status") not in {"completed", "already_downloaded"}:
                if data.get("status") in {"failed", "paused"} or not data.get("job_id"):
                    raise TranslationError("LM Studio 다운로드 상태를 확인하세요. / Check the download in LM Studio.")
                if cancel is not None:
                    cancel.wait(.5)
                else:
                    Event().wait(.5)
                check_cancel(cancel)
                data = self._request("/api/v1/models/download/status/" + quote(data["job_id"], safe=""), cancel=cancel)
                total, done = data.get("total_size_bytes", 0), data.get("downloaded_bytes", 0)
                if progress and total:
                    progress(min(100, round(done / total * 100)), 100)
        return self.list_models(cancel)

    def delete(self, name, cancel=None):
        validate_local_model(name)
        if self.provider != "ollama":
            raise TranslationError("LM Studio의 모델 화면에서 파일을 삭제하세요. / Delete model files in LM Studio.")
        self._request("/api/delete", {"model": name}, method="DELETE", cancel=cancel)
        return self.list_models(cancel)

    def copy(self, name, destination, cancel=None):
        validate_local_model(name)
        validate_local_model(destination)
        if self.provider != "ollama":
            raise TranslationError("모델 별칭은 Ollama에서 지원합니다. / Model aliases are supported by Ollama.")
        if any(row["id"] == destination for row in self.list_models(cancel)):
            raise TranslationError("이미 있는 모델 이름입니다. 다른 이름을 입력하세요. / Choose a new model name.")
        self._request("/api/copy", {"source": name, "destination": destination}, cancel=cancel)
        return self.list_models(cancel)

    def memory(self, name, load, cancel=None):
        validate_local_model(name)
        if self.provider == "ollama":
            self._request("/api/chat", {"model": name, "messages": [], "stream": False,
                "keep_alive": "5m" if load else 0}, cancel=cancel, timeout=120)
        elif load:
            self._request("/api/v1/models/load", {"model": name}, cancel=cancel, timeout=120)
        else:
            row = next((row for row in self.list_models(cancel) if row["id"] == name), {})
            for instance in row.get("instances", []):
                self._request("/api/v1/models/unload", {"instance_id": instance["id"]}, cancel=cancel)
        return self.list_models(cancel)

    def executable(self):
        pointer = self.root / "runtime/active.json"
        try:
            version = json.loads(pointer.read_text(encoding="utf-8"))["version"]
            if re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:[-.A-Za-z0-9]*)", version):
                path = self.root / "runtime" / version / "ollama.exe"
                if path.is_file():
                    return path
        except (OSError, ValueError, KeyError, TypeError):
            pass
        installed = shutil.which("ollama")
        path = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Programs/Ollama/ollama.exe"
        return Path(installed) if installed else path if path.is_file() else None

    def install_runtime(self, cancel=None, progress=None):
        check_cancel(cancel)
        if os.name != "nt" or platform.machine().lower() not in {"amd64", "x86_64"}:
            raise TranslationError("자동 설치는 Windows x64를 지원합니다. Ollama 공식 설치 안내를 이용하세요. / Automatic setup supports Windows x64; use the official Ollama installer on this platform.")
        release = request_json(RELEASE_URL, headers={"User-Agent": "MusicMotionStudio/1.0"}, cancel=cancel)
        version = release.get("tag_name", "")
        asset = next((row for row in release.get("assets", []) if row.get("name") == "ollama-windows-amd64.zip"), {})
        digest, url, size = asset.get("digest", ""), asset.get("browser_download_url", ""), asset.get("size", 0)
        if (not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:[-.A-Za-z0-9]*)", version)
                or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
                or url != f"https://github.com/ollama/ollama/releases/download/{version}/ollama-windows-amd64.zip"
                or not isinstance(size, int) or not 0 < size <= 4 * 1024**3):
            raise TranslationError("공식 배포 파일·검증 정보를 확인할 수 없습니다. / Cannot verify the official Ollama release.")
        runtime = self.root / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(runtime).free < size * 3 + 1024**3:
            raise TranslationError("실행 구성요소를 설치할 디스크 공간이 부족합니다. / Not enough disk space to install the runtime.")
        try:
            with TemporaryDirectory(prefix=".setup-", dir=runtime) as temporary:
                stage = Path(temporary)
                archive, unpacked = stage / "runtime.zip", stage / "unpacked"
                checksum, downloaded = hashlib.sha256(), 0
                request = Request(url, headers={"User-Agent": "MusicMotionStudio/1.0"})
                with build_opener(_HTTPSRedirect()).open(request, timeout=45) as response, archive.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        check_cancel(cancel)
                        downloaded += len(chunk)
                        if downloaded > size:
                            raise ValueError("Oversized runtime")
                        checksum.update(chunk)
                        output.write(chunk)
                        if progress:
                            progress(round(downloaded / size * 100), 100)
                if downloaded != size or checksum.hexdigest() != digest[7:]:
                    raise ValueError("Runtime checksum mismatch")
                self._extract_runtime(archive, unpacked, cancel)
                destination = runtime / version
                if not destination.resolve().is_relative_to(runtime.resolve()):
                    raise ValueError("Unsafe destination")
                check_cancel(cancel)
                if destination.exists():
                    raise TranslationError("같은 버전의 실행 구성요소가 이미 있습니다. 설치된 구성요소로 시작하세요. / This runtime version is already installed; start it instead.")
                unpacked.rename(destination)
                pointer = runtime / "active.tmp"
                pointer.write_text(json.dumps({"version": version}), encoding="utf-8")
                pointer.replace(runtime / "active.json")
        except TranslationError:
            raise
        except (OSError, ValueError, BadZipFile):
            raise TranslationError("Ollama 실행 구성요소를 설치하지 못했습니다. 네트워크·디스크 공간을 확인하고 다시 시도하세요. / Runtime setup failed; check network and disk space.") from None
        return self.executable()

    @staticmethod
    def _extract_runtime(archive, destination, cancel=None):
        with ZipFile(archive) as package:
            entries = package.infolist()
            if len(entries) > 10000 or sum(row.file_size for row in entries) > 8 * 1024**3:
                raise ValueError("Oversized archive")
            seen = set()
            for row in entries:
                check_cancel(cancel)
                path = PurePosixPath(row.filename)
                if (path.is_absolute() or ".." in path.parts or "\\" in row.orig_filename or ":" in row.filename
                        or stat.S_ISLNK(row.external_attr >> 16) or row.flag_bits & 1
                        or any(part.rstrip(". ") != part or part.split(".")[0].upper() in
                            {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))} for part in path.parts)):
                    raise ValueError("Unsafe runtime path")
                target = destination.joinpath(*path.parts)
                if not target.resolve().is_relative_to(destination.resolve()):
                    raise ValueError("Unsafe runtime path")
                if row.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                key = str(target).casefold()
                if key in seen:
                    raise ValueError("Duplicate path")
                seen.add(key)
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.open(row) as source, target.open("wb") as output:
                    while chunk := source.read(1024 * 1024):
                        check_cancel(cancel)
                        output.write(chunk)
        if not (destination / "ollama.exe").is_file():
            raise ValueError("Missing executable")

    def ensure_running(self, *, install=False, cancel=None, progress=None):
        try:
            return self.list_models(cancel)
        except TranslationError:
            pass
        address = urlsplit(self.base)
        if (self.provider != "ollama" or address.port != 11434 or address.path or address.scheme != "http"
                or address.hostname not in {"localhost", "127.0.0.1"}):
            raise TranslationError("설정한 로컬 서버를 시작하세요. 자동 시작은 기본 Ollama 주소를 지원합니다. / Start the configured server; automatic startup supports default Ollama only.")
        with _START_LOCK:
            executable = self.executable()
            if not executable and install:
                executable = self.install_runtime(cancel, progress)
            if not executable:
                raise TranslationError("Ollama 실행 구성요소가 필요합니다. 로컬 LLM 관리자에서 자동 설치·시작을 누르세요. / Open the local LLM manager and choose automatic setup.")
            check_cancel(cancel)
            if not any(process.poll() is None for process in _PROCESSES):
                environment = os.environ.copy()
                environment["OLLAMA_HOST"] = "127.0.0.1:11434"
                environment["OLLAMA_NO_CLOUD"] = "1"
                if executable.resolve().is_relative_to(self.root.resolve()):
                    environment["OLLAMA_MODELS"] = str(self.root / "models")
                try:
                    _PROCESSES.append(subprocess.Popen([str(executable), "serve"], env=environment,
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
                except OSError:
                    raise TranslationError("Ollama를 실행할 수 없습니다. 설치 안내와 실행 권한을 확인하세요. / Cannot start Ollama; check setup and executable permissions.") from None
            deadline = monotonic() + 25
            while monotonic() < deadline:
                check_cancel(cancel)
                try:
                    return self.list_models(cancel)
                except TranslationError:
                    if cancel is not None:
                        cancel.wait(.25)
                    else:
                        Event().wait(.25)
        raise TranslationError("실행 구성요소를 시작하지 못했습니다. 포트 사용 상태와 설치 안내를 확인하세요. / Could not start Ollama; check the port and setup guide.")
