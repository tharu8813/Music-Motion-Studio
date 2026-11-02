"""Managed Argos/CTranslate2 packages and in-process CPU translation."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
from threading import Event
import unicodedata
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import BadZipFile, ZipFile

from PySide6.QtCore import QLockFile

from app.services.lyrics_translation_service import IncompleteTranslation, TranslationError, check_cancel, request_json, same_language

CATALOG_URL = "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json"
MAX_ARCHIVE = 1024 * 1024 * 1024
MAX_EXPANDED = 2 * MAX_ARCHIVE


def _retry_install_io(operation, cancel=None):
    """Bounded, cancellable retries for Windows sharing/access-denied races."""
    waiter = cancel if cancel is not None else Event()
    for attempt, delay in enumerate((.05, .1, .2, .4, .8, .8, 0)):
        check_cancel(cancel)
        try:
            return operation()
        except PermissionError as error:
            if os.name != "nt" or getattr(error, "winerror", None) not in {5, 32, 33} or attempt == 6:
                raise
            waiter.wait(delay)
            check_cancel(cancel)


@contextmanager
def _publication_lock(target, cancel):
    """Coordinate this model's atomic publication across threads and app processes."""
    lock = QLockFile(str(target.parent / ("." + target.name + ".lock")))
    waiter = cancel if cancel is not None else Event()
    for _ in range(100):
        check_cancel(cancel)
        if lock.tryLock(0):
            break
        if lock.error() != QLockFile.LockError.LockFailedError:
            raise TranslationError("모델 설치 잠금을 만들 수 없습니다. 폴더 권한을 확인하세요. / Cannot lock model installation; check folder permissions.")
        waiter.wait(.05)
    else:
        raise TranslationError("같은 모델을 다른 작업에서 설치하고 있습니다. 잠시 후 다시 시도하세요. / Another operation is installing this model; retry shortly.")
    try:
        yield
    finally:
        lock.unlock()


class _HTTPSRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise TranslationError("모델 다운로드는 HTTPS만 지원합니다. / Model downloads require HTTPS.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True)
class LocalTranslationModel:
    id: str
    directory: Path
    source: str
    target: str
    name: str
    prefix: str = ""


class LocalTranslationModels:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PlaylistCanvas" / "translation-models"

    @staticmethod
    def _read(directory):
        if (directory / "metadata.json").stat().st_size > 64 * 1024:
            raise ValueError("Model metadata is too large")
        data = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Invalid model metadata")
        for key in ("from_code", "to_code"):
            if not isinstance(data.get(key), str) or len(data[key]) > 64 or not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", data[key]):
                raise ValueError("Invalid model language")
        if data.get("type", "translate") != "translate":
            raise ValueError("Not a translation package")
        for name in ("model/model.bin", "sentencepiece.model"):
            if not (directory / name).is_file():
                raise ValueError("Requires CTranslate2 and SentencePiece model files")
        if not any((directory / "model" / name).is_file() for name in
                ("shared_vocabulary.txt", "shared_vocabulary.json", "source_vocabulary.txt", "source_vocabulary.json")):
            raise ValueError("Missing model vocabulary")
        prefix = data.get("target_prefix", "")
        if not isinstance(prefix, str) or len(prefix) > 128:
            raise ValueError("Invalid target prefix")
        return LocalTranslationModel(directory.name, directory, data["from_code"], data["to_code"],
            f"{str(data.get('from_name', data['from_code']))[:80]} → {str(data.get('to_name', data['to_code']))[:80]} · {str(data.get('package_version', ''))[:32]}", prefix)

    def installed(self):
        models = []
        if self.root.is_dir():
            for directory in self.root.iterdir():
                if not directory.is_dir() or directory.name.startswith("."):
                    continue
                try:
                    models.append(self._read(directory))
                except (OSError, ValueError, TypeError):
                    continue
        return sorted(models, key=lambda model: model.name)

    def get(self, model_id):
        model = next((model for model in self.installed() if model.id == model_id), None)
        if model is None:
            raise TranslationError("로컬 번역 모델을 가져오거나 내려받은 뒤 선택하세요. / Import or download a local translation model first.")
        return model

    def for_pair(self, source, target):
        return [model for model in self.installed()
            if same_language(source, model.source) and same_language(target, model.target)]

    def download_pair(self, source, target, cancel=None, progress=None):
        check_cancel(cancel)
        if same_language(source, target):
            raise TranslationError("같은 언어는 모델 설치 없이 번역을 생략합니다. / No model is needed for identical languages.")
        entries = [row for row in self.catalog(cancel)
            if same_language(row["from_code"], source) and same_language(row["to_code"], target)]
        if not entries:
            raise TranslationError(f"{source} → {target} 방향의 공식 로컬 모델이 없습니다. 언어를 바꾸거나 클라우드 LLM·로컬 LLM을 사용하세요. / No direct local model for {source} → {target}; change languages or use an LLM.")
        entry = max(entries, key=lambda row: tuple(int(part) for part in re.findall(r"\d+", row["package_version"])))
        model = self.download(entry, cancel, progress)
        if not same_language(model.source, source) or not same_language(model.target, target):
            raise TranslationError("내려받은 모델의 언어 방향이 요청과 다릅니다. / Downloaded model language direction does not match.")
        return model

    def catalog(self, cancel=None):
        data = request_json(CATALOG_URL, cancel=cancel)
        if not isinstance(data, list):
            raise TranslationError("모델 목록을 읽을 수 없습니다. / Invalid model catalog.")
        rows = []
        for entry in data:
            if not isinstance(entry, dict) or entry.get("type", "translate") != "translate":
                continue
            links = entry.get("links", [])
            if (all(isinstance(entry.get(key), str) for key in ("from_code", "to_code", "package_version"))
                    and isinstance(links, list) and any(isinstance(url, str) and url.startswith("https://") for url in links)):
                rows.append(entry)
        return sorted(rows, key=lambda row: (row.get("from_name", row["from_code"]), row.get("to_name", row["to_code"])))

    def install(self, archive, cancel=None, progress=None):
        """Extract data only, in a temporary sibling; preserve package licenses."""
        check_cancel(cancel)
        archive = Path(archive)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            if archive.stat().st_size > MAX_ARCHIVE:
                raise TranslationError("모델 파일이 너무 큽니다(최대 1 GB). / Model package exceeds 1 GB.")
            with TemporaryDirectory(prefix=".install-", dir=self.root, ignore_cleanup_errors=True) as temporary:
                stage = Path(temporary)
                try:
                    with ZipFile(archive) as package:
                        entries = package.infolist()
                        if len(entries) > 10_000 or sum(entry.file_size for entry in entries) > MAX_EXPANDED:
                            raise ValueError("Oversized package")
                        seen = set()
                        for number, entry in enumerate(entries):
                            check_cancel(cancel)
                            path = PurePosixPath(entry.filename)
                            if (path.is_absolute() or ".." in path.parts or "\\" in entry.orig_filename or ":" in entry.filename
                                    or any(part.rstrip(". ") != part or part.split(".")[0].upper() in
                                        {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))} for part in path.parts)
                                    or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1):
                                raise ValueError("Unsafe package path")
                            destination = stage.joinpath(*path.parts)
                            if not destination.resolve().is_relative_to(stage.resolve()):
                                raise ValueError("Unsafe package path")
                            if entry.is_dir():
                                destination.mkdir(parents=True, exist_ok=True)
                                continue
                            key = str(destination).casefold()
                            if key in seen:
                                raise ValueError("Duplicate package path")
                            seen.add(key)
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            with package.open(entry) as incoming, destination.open("wb") as outgoing:
                                while chunk := incoming.read(1024 * 1024):
                                    check_cancel(cancel)
                                    outgoing.write(chunk)
                            if progress:
                                progress(number + 1, len(entries))
                    candidates = list(stage.glob("metadata.json")) + list(stage.glob("*/metadata.json"))
                    if len(candidates) != 1:
                        raise ValueError("Expected one translation package")
                    model = self._read(candidates[0].parent)
                    with archive.open("rb") as handle:
                        digest = hashlib.file_digest(handle, "sha256").hexdigest()[:16]
                    target = self.root / f"{model.source}-{model.target}-{digest}"
                    check_cancel(cancel)
                    with _publication_lock(target, cancel):
                        if not target.exists():
                            ready = model.directory
                            if ready == stage:
                                ready = stage / ".ready"
                                ready.mkdir()
                                for child in tuple(stage.iterdir()):
                                    if child != ready:
                                        _retry_install_io(lambda source=child: source.rename(ready / source.name), cancel)
                            if not ready.resolve().is_relative_to(self.root.resolve()) or not target.resolve().is_relative_to(self.root.resolve()):
                                raise ValueError("Model path leaves managed directory")
                            _retry_install_io(lambda: ready.rename(target), cancel)
                    return self._read(target)
                finally:
                    self._cleanup_stage(stage)
        except TranslationError:
            raise
        except OSError:
            raise TranslationError("모델 설치 폴더에 접근할 수 없습니다. 파일을 사용하는 프로그램과 폴더 권한·디스크 공간을 확인한 뒤 다시 시도하세요. 기존 모델은 유지됩니다. / Cannot access model installation folder; check file locks, permissions and disk space. Existing models are preserved.") from None
        except (ValueError, KeyError, TypeError, RuntimeError, BadZipFile):
            raise TranslationError("지원하는 .argosmodel 파일이 아닙니다. CTranslate2·SentencePiece 모델을 확인하세요. / Invalid or unsupported Argos model package.") from None

    def _cleanup_stage(self, stage):
        # Delete only the private directory created for this installation.
        if not stage.resolve().is_relative_to(self.root.resolve()) or not stage.name.startswith(".install-"):
            raise ValueError("Unexpected installation cleanup directory")
        try:
            _retry_install_io(lambda: shutil.rmtree(stage))
        except FileNotFoundError:
            pass
        except OSError:
            # A published model remains successful even if an external handle prevents
            # temporary cleanup. Hidden leftovers are never discovered as models.
            logging.getLogger(__name__).warning("Could not remove locked temporary model directory: %s", stage)

    def download(self, entry, cancel=None, progress=None):
        check_cancel(cancel)
        url = next((url for url in entry.get("links", []) if isinstance(url, str) and urlsplit(url).scheme == "https"), "")
        if not url:
            raise TranslationError("모델 다운로드 주소를 확인하세요. / Missing HTTPS model URL.")
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            with TemporaryDirectory(prefix=".download-", dir=self.root) as temporary:
                archive = Path(temporary) / "model.argosmodel"
                with build_opener(_HTTPSRedirect()).open(Request(url, headers={"User-Agent": "MusicMotionStudio/1.0"}), timeout=45) as response, archive.open("wb") as output:
                    if urlsplit(response.url).scheme != "https":
                        raise TranslationError("안전한 모델 다운로드 주소가 아닙니다. / Model download requires HTTPS.")
                    total = int(response.headers.get("Content-Length", 0))
                    if total > MAX_ARCHIVE:
                        raise TranslationError("모델 파일이 너무 큽니다. / Model package is too large.")
                    size = 0
                    while chunk := response.read(1024 * 1024):
                        check_cancel(cancel)
                        size += len(chunk)
                        if size > MAX_ARCHIVE:
                            raise TranslationError("모델 파일이 너무 큽니다. / Model package is too large.")
                        output.write(chunk)
                        if progress:
                            progress(size, total)
                if total and size != total:
                    raise TranslationError("모델 다운로드가 중단되었습니다. / Model download ended early.")
                return self.install(archive, cancel)
        except OSError:
            raise TranslationError("모델을 내려받을 수 없습니다. 연결 상태를 확인하세요. / Cannot download the model; check your connection.") from None


class LocalTranslationEngine:
    def __init__(self, models):
        self.models = models
        self.model = self.translator = self.tokenizer = None

    def translate(self, texts, config, cancel=None):
        model = self.models.get(config.model)
        if not same_language(config.source, model.source) or not same_language(config.target, model.target):
            raise TranslationError("선택한 모델의 원문·번역 언어와 설정이 다릅니다. / Selected model language pair does not match.")
        check_cancel(cancel)
        def already_target(text):
            letters = [unicodedata.name(char, "") for char in text if char.isalpha()]
            if not letters:
                return False
            target = config.target.lower()
            if target == "ko":
                return all(name.startswith("HANGUL") for name in letters)
            if target == "ja":
                return (any(name.startswith(("HIRAGANA", "KATAKANA")) for name in letters)
                    and all(name.startswith(("HIRAGANA", "KATAKANA", "CJK")) for name in letters))
            return False
        try:
            if self.model != model:
                import ctranslate2
                import sentencepiece
                self.tokenizer = sentencepiece.SentencePieceProcessor(model_file=str(model.directory / "sentencepiece.model"))
                self.translator = ctranslate2.Translator(str(model.directory / "model"), device="cpu",
                    compute_type="int8", inter_threads=1, intra_threads=min(4, os.cpu_count() or 1))
                self.model = model
            result = []
            for text in texts:
                if already_target(text):
                    result.append(None)
                    continue
                lines, missing = [], []
                needs_translation = False
                for number, line in enumerate(text.split("\n"), 1):
                    check_cancel(cancel)
                    # Lyrics imported from subtitle files can contain an otherwise
                    # invisible BOM/zero-width separator. SentencePiece can reduce
                    # these lines to no tokens at all.
                    line = line.translate({0x200b: None, 0xfeff: None, 0x2060: None}).strip()
                    if not line:
                        lines.append("")
                        continue
                    if already_target(line) or not any(char.isalpha() for char in line):
                        lines.append(line)
                        continue
                    needs_translation = True
                    tokens = self.tokenizer.encode(line, out_type=str)
                    if len(tokens) > 512:
                        raise TranslationError("로컬 모델의 한 줄 길이를 초과했습니다. 가사 줄을 나눠 주세요. / Split lyric lines longer than 512 model tokens.")
                    value = ""
                    if tokens:
                        # A beam can terminate with only a whitespace token. Try
                        # greedy decoding once, without forcing invented content.
                        for beam_size in (4, 1):
                            check_cancel(cancel)
                            translated = self.translator.translate_batch([tokens], beam_size=beam_size, max_decoding_length=768,
                                max_input_length=0, replace_unknowns=True, length_penalty=.2,
                                target_prefix=[[model.prefix]] if model.prefix else None)[0]
                            check_cancel(cancel)
                            if len(translated.hypotheses[0]) >= 768:
                                raise TranslationError("로컬 번역 결과가 너무 깁니다. 가사 줄을 나눠 주세요. / Local output limit reached; split the lyric line.")
                            value = self.tokenizer.decode(translated.hypotheses[0])
                            if model.prefix and value.startswith(model.prefix):
                                value = value[len(model.prefix):]
                            if value.strip():
                                break
                    if not value.strip():
                        missing.append(number)
                    lines.append(value.strip())
                value = "\n".join(lines)
                if missing:
                    result.append(IncompleteTranslation(value, tuple(missing)))
                else:
                    result.append(value if needs_translation else None)
            return result
        except TranslationError:
            raise
        except ImportError:
            raise TranslationError("로컬 번역 실행 구성요소가 없습니다. 앱을 다시 설치하세요. / Local translation runtime is missing; reinstall the app.") from None
        except (RuntimeError, ValueError, OSError):
            raise TranslationError("로컬 번역 모델을 실행할 수 없습니다. 모델 파일을 확인하세요. / Cannot run the local model; check its files.") from None
