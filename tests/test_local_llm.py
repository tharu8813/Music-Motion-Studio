"""Local model protocols, verified runtime setup and user-facing setup flows."""
import hashlib
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Lock
import time
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from PySide6.QtCore import QSettings, QTimer, Qt
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from app.dialogs.lyrics_translation_dialog import LyricsTranslationDialog
from app.dialogs.local_llm_manager_dialog import LocalLLMManagerDialog
from app.services.local_llm_service import LocalLLMService, local_base, MODEL_GUIDES
from app.services.local_translation_service import LocalTranslationModels
from app.services.lyrics_translation_service import (CLOUD_LLM_PROVIDERS, LOCAL_LLM_PROVIDERS,
    TranslationConfig, TranslationError, TranslationCancelled)
from app.utils.i18n import Translator
from tests.test_local_translation import package_bytes


def installed_row(name="qwen3:4b"):
    return {"id": name, "name": name, "size": 2500000000, "parameters": "4B", "quantization": "Q4_K_M",
        "instances": [], "metadata": {}}


class LocalLLMServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = LocalLLMService(root=self.root)

    def respond(self, callback):
        opener = Mock()
        def opened(request, **kwargs):
            value = callback(request)
            return BytesIO(value if isinstance(value, bytes) else json.dumps(value).encode())
        opener.open.side_effect = opened
        patcher = patch("app.services.local_llm_service.build_opener", return_value=opener)
        patcher.start()
        self.addCleanup(patcher.stop)
        return opener

    def test_only_loopback_on_device_models_are_accepted(self):
        for url in ("https://example.com/v1", "http://192.168.1.2/v1", "http://user:secret@localhost/v1"):
            with self.subTest(url=url), self.assertRaises(TranslationError):
                local_base(url)
        self.assertEqual(local_base("http://127.0.0.1:11434/v1"), "http://127.0.0.1:11434")
        with self.assertRaises(TranslationError):
            TranslationConfig("ollama", model="qwen3:cloud", endpoint="http://localhost:11434/v1").validate()

    def test_installed_list_excludes_remote_cloud_models_and_filters_lmstudio_embeddings(self):
        self.respond(lambda request: {"models": [
            {"name": "qwen3:4b", "size": 2500, "details": {"parameter_size": "4B"}},
            {"name": "qwen3:cloud", "remote_host": "https://ollama.com"},
            {"key": "qwen3-8b", "type": "llm", "params_string": "8B", "quantization": None},
            {"key": "embedding", "type": "embedding"}]})
        self.assertEqual([row["id"] for row in self.service.list_models()], ["qwen3:4b"])
        lm = LocalLLMService("lmstudio", "http://localhost:1234/v1")
        self.assertEqual([row["id"] for row in lm.list_models()], ["qwen3-8b"])

    def test_pull_stream_reports_bounded_progress_and_requires_success(self):
        events, requests = [], []
        stream = b'{"status":"pulling","total":5000000000,"completed":2500000000}\n{"status":"success"}\n'
        def response(request):
            requests.append(request)
            return stream if request.full_url.endswith("/api/pull") else {"models": [{"name": "qwen3:4b"}]}
        self.respond(response)
        self.assertEqual(self.service.download("qwen3:4b", progress=lambda *event: events.append(event))[0]["id"], "qwen3:4b")
        self.assertEqual(events, [(50, 100)])
        pull = next(request for request in requests if request.full_url.endswith("/api/pull"))
        self.assertEqual(json.loads(pull.data), {"model": "qwen3:4b", "stream": True})
        self.respond(lambda request: b'{"status":"pulling"}\n' if request.full_url.endswith("/api/pull") else {"models": []})
        with self.assertRaisesRegex(TranslationError, "interrupted"):
            self.service.download("qwen3:4b")

    def test_pull_exposes_phase_bytes_and_actionable_errors_without_changing_progress_api(self):
        events, fractions = [], []
        stream = (b'{"status":"pulling manifest"}\n'
                  b'{"status":"pulling layer","total":1000,"completed":250}\n'
                  b'{"status":"success"}\n')
        self.respond(lambda request: stream if request.full_url.endswith("/api/pull")
                     else {"models": [{"name": "qwen3:4b"}]})
        self.service.download("qwen3:4b", progress=lambda *args: fractions.append(args),
                              status=events.append)
        self.assertEqual(fractions, [(25, 100)])
        self.assertEqual([event["status"] for event in events],
                         ["pulling manifest", "pulling layer", "success"])
        self.assertEqual(events[1]["completed"], 250)
        self.assertEqual(events[1]["total"], 1000)
        self.respond(lambda request: b'{"error":"pull model manifest: file does not exist"}\n'
                     if request.full_url.endswith("/api/pull") else {"models": []})
        with self.assertRaisesRegex(TranslationError, "모델 이름을 찾을 수 없습니다"):
            self.service.download("missing-model")

    def test_ollama_capabilities_hide_embeddings_and_cloud_aliases(self):
        def response(request):
            if request.full_url.endswith("/api/tags"):
                return {"models": [{"name": name} for name in ("qwen3:4b", "embed:latest", "alias:latest")]}
            name = json.loads(request.data)["model"]
            return {"capabilities": ["embedding"] if name.startswith("embed") else ["completion"],
                "remote_host": "https://ollama.com" if name.startswith("alias") else ""}
        opener = self.respond(response)
        self.assertEqual([row["id"] for row in self.service.list_models()], ["qwen3:4b"])
        self.service.list_models()
        self.assertEqual(opener.open.call_count, 5)  # two tag lists, three cached inspections

    def test_ollama_model_inspection_runs_in_parallel_and_refresh_reuses_cache(self):
        inspected, active, peak = [], 0, 0
        gate = Barrier(2, timeout=2)
        lock = Lock()
        names = ["qwen3:4b", "qwen3:8b", "embed:latest", "remote:latest"]
        def request(path, body=None, **kwargs):
            nonlocal active, peak
            if path == "/api/tags":
                return {"models": [{"name": name, "digest": name + "-sha"} for name in names]}
            self.assertEqual(path, "/api/show")
            name = body["model"]
            with lock:
                inspected.append(name)
                active += 1
                peak = max(peak, active)
            try:
                if len(inspected) <= 2:
                    gate.wait()
                return {"capabilities": ["embedding"] if name.startswith("embed") else ["completion"],
                        "remote_host": bool(name.startswith("remote"))}
            finally:
                with lock:
                    active -= 1
        with patch.object(self.service, "_request", side_effect=request):
            self.assertEqual([r["id"] for r in self.service.list_models()],
                             ["qwen3:4b", "qwen3:8b"])
            self.assertGreaterEqual(peak, 2)
            self.assertEqual(len(inspected), 4)
            self.service.list_models()
            self.assertEqual(len(inspected), 4)
        # A changed digest must trigger a fresh check, even if the name stays.
        with patch.object(self.service, "_request", side_effect=lambda path, body=None, **kwargs:
                {"models": [{"name": "qwen3:4b", "digest": "updated"}]} if path == "/api/tags"
                else {"capabilities": ["embedding"]}) as request:
            self.assertEqual(self.service.list_models(), [])
            self.assertEqual(request.call_count, 2)

    def test_failed_model_inspection_is_retried_on_refresh(self):
        attempts = []
        def request(path, body=None, **kwargs):
            if path == "/api/tags":
                return {"models": [{"name": "qwen3:4b", "digest": "same"}]}
            attempts.append(body["model"])
            if len(attempts) == 1:
                raise TranslationError("Temporary error")
            return {"capabilities": ["completion"]}
        with patch.object(self.service, "_request", side_effect=request):
            self.assertEqual([r["id"] for r in self.service.list_models()], ["qwen3:4b"])
            self.assertEqual([r["id"] for r in self.service.list_models()], ["qwen3:4b"])
            self.assertEqual(len(attempts), 2)
            self.service.list_models()
            self.assertEqual(len(attempts), 2)

    def test_cancelled_pull_closes_response_and_stops_before_final_listing(self):
        cancel = Event()
        class Response(BytesIO):
            def readline(self, *args):
                cancel.set()
                return super().readline(*args)
        response = Response(b'{"status":"pulling","total":10,"completed":2}\n')
        opener = Mock()
        opener.open.return_value = response
        with patch.object(self.service, "ensure_running", return_value=[]), patch(
                "app.services.local_llm_service.build_opener", return_value=opener):
            with self.assertRaises(TranslationCancelled):
                self.service.download("qwen3:4b", cancel)
        self.assertTrue(response.closed)

    def test_delete_copy_and_memory_actions_use_native_model_apis(self):
        requests = []
        self.respond(lambda request: (requests.append(request), {"models": []})[1])
        self.service.delete("qwen3:4b")
        self.service.copy("qwen3:4b", "lyrics:4b")
        self.service.memory("qwen3:4b", False)
        delete = next(row for row in requests if row.full_url.endswith("/api/delete"))
        self.assertEqual(delete.get_method(), "DELETE")
        copy = next(row for row in requests if row.full_url.endswith("/api/copy"))
        self.assertEqual(json.loads(copy.data)["destination"], "lyrics:4b")
        memory = next(row for row in requests if row.full_url.endswith("/api/chat"))
        self.assertEqual(json.loads(memory.data)["keep_alive"], 0)

    def test_lmstudio_download_polls_job_and_unloads_instance_ids(self):
        requests = []
        def response(request):
            requests.append(request)
            if request.full_url.endswith("/download"):
                return {"job_id": "job1", "status": "downloading"}
            if "/download/status/" in request.full_url:
                return {"job_id": "job1", "status": "completed", "total_size_bytes": 10, "downloaded_bytes": 10}
            return {"models": [{"type": "llm", "key": "qwen3", "loaded_instances": [{"id": "instance-1"}]}]}
        self.respond(response)
        service = LocalLLMService("lmstudio", "http://localhost:1234/v1", "local-token")
        progress = []
        self.assertEqual(service.download("qwen3", progress=lambda *args: progress.append(args))[0]["id"], "qwen3")
        service.memory("qwen3", False)
        request = next(row for row in requests if row.full_url.endswith("/unload"))
        self.assertEqual(json.loads(request.data), {"instance_id": "instance-1"})
        self.assertEqual(request.get_header("Authorization"), "Bearer local-token")
        self.assertEqual(progress, [(100, 100)])

    def test_lmstudio_accepts_only_valid_hugging_face_references(self):
        service = LocalLLMService("lmstudio", "http://localhost:1234/v1")
        with patch.object(service, "_request", side_effect=lambda path, *args, **kwargs:
                {"status": "completed"} if path.endswith("/download") else {"models": []}):
            rows = service.download("https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct-GGUF")
        self.assertEqual(rows, [])
        for address in ("https://evil.example/a/b", "https://huggingface.co/a/../b",
                        "https://huggingface.co/a/b?token=secret"):
            with self.subTest(address=address), self.assertRaises(TranslationError):
                service.download(address)

    def test_existing_server_is_reused_without_spawning_or_installing(self):
        with patch.object(self.service, "list_models", return_value=[installed_row()]), patch(
                "app.services.local_llm_service.subprocess.Popen") as spawn, patch.object(self.service, "install_runtime") as install:
            self.assertTrue(self.service.ensure_running(install=True))
        spawn.assert_not_called()
        install.assert_not_called()

    def test_managed_server_starts_hidden_with_local_storage_and_cloud_disabled(self):
        executable = self.root / "runtime/v1.2.3/ollama.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"test fixture, never executed")
        process = Mock()
        process.poll.return_value = None
        with patch.object(self.service, "list_models", side_effect=[TranslationError("offline"), []]), patch.object(
                self.service, "executable", return_value=executable), patch(
                "app.services.local_llm_service.subprocess.Popen", return_value=process) as spawn, patch(
                "app.services.local_llm_service._PROCESSES", []):
            self.assertEqual(self.service.ensure_running(), [])
        self.assertEqual(spawn.call_args.args[0], [str(executable), "serve"])
        self.assertEqual(spawn.call_args.kwargs["env"]["OLLAMA_HOST"], "127.0.0.1:11434")
        self.assertEqual(spawn.call_args.kwargs["env"]["OLLAMA_NO_CLOUD"], "1")
        self.assertEqual(spawn.call_args.kwargs["env"]["OLLAMA_MODELS"], str(self.root / "models"))
        self.assertNotEqual(spawn.call_args.kwargs["creationflags"], 0)

    def test_managed_runtime_is_hash_verified_and_committed_only_after_validation(self):
        buffer = BytesIO()
        with ZipFile(buffer, "w") as archive:
            archive.writestr("ollama.exe", b"test fixture, never executed")
            archive.writestr("LICENSE", "Test license")
        data = buffer.getvalue()
        release = {"tag_name": "v1.2.3", "assets": [{"name": "ollama-windows-amd64.zip", "size": len(data),
            "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
            "browser_download_url": "https://github.com/ollama/ollama/releases/download/v1.2.3/ollama-windows-amd64.zip"}]}
        self.respond(lambda request: data)
        with patch("app.services.local_llm_service.request_json", return_value=release), patch(
                "app.services.local_llm_service.shutil.disk_usage", return_value=Mock(free=20 * 1024**3)):
            executable = self.service.install_runtime()
            self.assertEqual(executable.read_bytes(), b"test fixture, never executed")
            self.assertTrue((executable.parent / "LICENSE").is_file())
            self.assertFalse(any(path.name.startswith(".setup-") for path in executable.parent.parent.iterdir()))
            release["assets"][0]["digest"] = "sha256:" + "0" * 64
            with self.assertRaises(TranslationError):
                self.service.install_runtime()
        self.assertEqual(self.service.executable(), executable)

    def test_runtime_archive_traversal_is_rejected(self):
        data = self.root / "bad.zip"
        with ZipFile(data, "w") as archive:
            archive.writestr("../escape.exe", b"unsafe")
            archive.writestr("ollama.exe", b"test")
        with self.assertRaises(ValueError):
            self.service._extract_runtime(data, self.root / "runtime")
        self.assertFalse((self.root / "escape.exe").exists())


class TranslationSetupUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)

    def until(self, condition):
        deadline = time.monotonic() + 4
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.002)
        self.assertTrue(condition())

    def dialog(self, **kwargs):
        dialog = LyricsTranslationDialog([{"text": "Hello", "start": 0, "end": 1}], Translator(), settings=self.settings, **kwargs)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_three_modes_and_saved_local_llm_are_restored_with_installed_combo(self):
        self.settings.setValue("translation/provider", "ollama")
        self.settings.setValue("translation/config/ollama/model", "qwen3:8b")
        service = Mock()
        service.discover.return_value = [installed_row(), installed_row("qwen3:8b")]
        dialog = self.dialog(local_llm_factory=lambda *args: service)
        self.assertEqual([dialog.mode_combo.itemText(i) for i in range(dialog.mode_combo.count())],
            ["클라우드 LLM", "로컬 LLM", "로컬 번역 모델"])
        self.assertEqual(dialog.mode_combo.currentData(), "local_llm")
        self.until(lambda: dialog.model_combo.count() == 2 and dialog.worker is None)
        self.assertEqual(dialog._model_value(), "qwen3:8b")
        self.assertFalse(dialog.model_combo.isEditable())
        self.assertEqual({dialog.provider_combo.itemData(i) for i in range(dialog.provider_combo.count())}, LOCAL_LLM_PROVIDERS)
        dialog.mode_combo.setCurrentIndex(0)
        self.assertEqual({dialog.provider_combo.itemData(i) for i in range(dialog.provider_combo.count())}, CLOUD_LLM_PROVIDERS)
        self.assertTrue(dialog.local_accuracy_warning.isHidden())

    def test_service_model_refresh_reuses_same_connection_and_capability_cache(self):
        self.settings.setValue("translation/provider", "ollama")
        instances = []
        def factory(*args):
            service = Mock()
            service.discover.return_value = [installed_row()]
            instances.append(service)
            return service
        dialog = self.dialog(local_llm_factory=factory)
        self.until(lambda: dialog.worker is None)
        service_dialog = dialog.service_dialog
        first = service_dialog._llm_service()
        self.assertIs(first, service_dialog._llm_service())
        self.assertEqual(len(instances), 1)
        service_dialog.endpoint_edit.setText("http://localhost:11435/v1")
        self.assertIsNot(first, service_dialog._llm_service())
        self.assertEqual(len(instances), 2)

    def test_languages_filter_models_and_inline_install_preserves_selected_languages(self):
        models = LocalTranslationModels(self.root / "models")
        archive = self.root / "en-ko.argosmodel"
        archive.write_bytes(package_bytes())
        model = models.install(archive)
        from app.services.lyrics_translation_service import LyricsTranslationService
        self.settings.setValue("translation/provider", "local_nmt")
        dialog = self.dialog(service=LyricsTranslationService(local_models=models))
        self.assertEqual(dialog._model_value(), model.id)
        self.assertTrue(dialog.source_combo.isEnabled())
        self.assertFalse(dialog.local_models_button.isEnabled())
        self.assertFalse(dialog.local_accuracy_warning.isHidden())
        self.assertIn("정확도가 크게", dialog.local_accuracy_warning.text())
        self.assertIn("LLM 번역", dialog.local_accuracy_warning.text())
        dialog.source_combo.setCurrentIndex(dialog.source_combo.findData("ja"))
        self.assertEqual(dialog._model_value(), "")
        self.assertTrue(dialog.local_models_button.isEnabled())
        self.assertIn("설치가 필요", dialog.local_pair_note.text())
        with patch.object(models, "download_pair", side_effect=TranslationError("No direct local model")):
            dialog._install_local_pair()
            self.until(lambda: dialog.worker is None)
        self.assertIn("No direct", dialog.status_label.text())
        self.assertEqual(dialog._language_value(dialog.source_combo), "ja")
        dialog.target_combo.setCurrentIndex(dialog.target_combo.findData("ja"))
        self.assertIn("같은 언어", dialog.local_pair_note.text())
        self.assertFalse(dialog.local_models_button.isEnabled())

    def test_language_pair_download_matches_direction_and_latest_version(self):
        models = LocalTranslationModels(self.root / "models")
        rows = [{"from_code": source, "to_code": target, "package_version": version}
            for source, target, version in (("ko", "en", "9.0"), ("en", "ko", "1.2"), ("en", "ko", "1.10"))]
        result = Mock(source="en", target="ko")
        with patch.object(models, "catalog", return_value=rows), patch.object(models, "download", return_value=result) as download:
            self.assertIs(models.download_pair("en", "ko"), result)
            self.assertEqual(download.call_args.args[0]["package_version"], "1.10")
            with self.assertRaisesRegex(TranslationError, "No direct local model"):
                models.download_pair("ja", "ko")
            self.assertEqual(download.call_count, 1)

    def test_close_with_edits_saves_draft_and_apply_never_prompts(self):
        dialog = self.dialog()
        dialog.show()
        dialog.table.item(0, 3).setText("안녕")
        dialog.close()
        self.assertFalse(dialog.isVisible())
        self.assertNotIn("translation", dialog.selected_cues[0])
        reopened = self.dialog()
        self.assertEqual(reopened.table.item(0, 3).text(), "안녕")
        reopened._apply()
        self.assertEqual(reopened.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(reopened.selected_cues[0]["translation"]["text"], "안녕")
        self.assertTrue(self.settings.contains(reopened.draft.key))

    def test_settings_can_close_without_hiding_review_or_losing_edits(self):
        dialog = self.dialog()
        dialog.show()
        review = dialog.review_panel
        review.table.item(0, 3).setText("검토 초안")
        dialog._show_window(dialog.service_dialog)
        dialog.service_dialog.close()
        self.assertEqual(review.table.item(0, 3).text(), "검토 초안")
        self.assertTrue(review.isVisible())
        self.assertFalse(review.isWindow())

    def test_manager_failed_refresh_discards_previous_connection_proof(self):
        self.settings.setValue("translation/provider", "ollama")
        local = Mock(provider="ollama", base="http://localhost:11434")
        local.discover.return_value = [installed_row()]
        dialog = self.dialog(local_llm_factory=lambda *args: local)
        services = dialog.service_dialog
        self.until(lambda: dialog.worker is None and services.verified_connection() is not None)
        self.assertTrue(services.verified_connection()[0])

        services._open_llm_manager()
        manager = services.manager
        self.until(lambda: manager.worker is None and manager.models_verified_at is not None)
        local.discover.side_effect = TranslationError("server disconnected")
        manager._refresh()
        self.until(lambda: manager.worker is None and "server disconnected" in manager.status.text())
        self.assertIsNone(manager.models_verified_at)
        self.assertIn("연결 실패", manager.engine_state.text())
        manager.close()
        self.app.processEvents()
        self.assertIsNone(services.connection)
        self.assertIsNone(services.verified_connection())

    def test_manager_cancelled_refresh_discards_previous_connection_proof(self):
        self.settings.setValue("translation/provider", "ollama")
        local = Mock(provider="ollama", base="http://localhost:11434")
        local.discover.return_value = [installed_row()]
        dialog = self.dialog(local_llm_factory=lambda *args: local)
        services = dialog.service_dialog
        self.until(lambda: dialog.worker is None and services.verified_connection() is not None)
        services._open_llm_manager()
        manager = services.manager
        self.until(lambda: manager.worker is None and manager.models_verified_at is not None)

        entered, release = Event(), Event()
        def delayed(cancel):
            entered.set()
            release.wait(3)
            return [installed_row()]
        local.discover.side_effect = delayed
        manager._refresh()
        self.until(entered.is_set)
        self.assertIsNone(manager.models_verified_at)
        manager._cancel()
        release.set()
        self.until(lambda: manager.worker is None)
        self.assertIsNone(manager.models_verified_at)
        self.assertIn("미확인", manager.engine_state.text())
        manager.close()
        self.app.processEvents()
        self.assertIsNone(services.connection)

    def test_manager_failed_model_operation_invalidates_previous_listing(self):
        self.settings.setValue("translation/provider", "ollama")
        local = Mock(provider="ollama", base="http://localhost:11434")
        local.discover.return_value = [installed_row()]
        dialog = self.dialog(local_llm_factory=lambda *args: local)
        services = dialog.service_dialog
        self.until(lambda: dialog.worker is None and services.verified_connection() is not None)
        services._open_llm_manager()
        manager = services.manager
        self.until(lambda: manager.worker is None and manager.models_verified_at is not None)
        self.assertIsNotNone(manager._selected())
        local.memory.side_effect = TranslationError("memory operation failed")
        manager._memory(True)
        self.until(lambda: manager.worker is None and "memory operation failed" in manager.status.text())
        self.assertIsNone(manager.models_verified_at)
        self.assertIn("미확인", manager.engine_state.text())
        manager.close()
        self.app.processEvents()
        self.assertIsNone(services.verified_connection())


    def test_manager_cancel_drops_late_download_progress_and_activity(self):
        local = Mock(provider="ollama", base="http://localhost:11434")
        local.discover.return_value = [installed_row()]
        manager = LocalLLMManagerDialog(Translator(), service=local)
        self.addCleanup(manager.deleteLater)
        self.until(lambda: manager.worker is None and manager.models_verified_at is not None)
        entered, release = Event(), Event()

        def work(cancel, progress):
            entered.set()
            release.wait(3)
            progress(75, 100)
            manager.worker.activity.emit({
                "status": "pulling manifest", "completed": 75, "total": 100})
            return [installed_row()]

        manager._start("download", work)
        self.until(entered.is_set)
        manager._cancel()
        release.set()
        self.until(lambda: manager.worker is None)
        self.assertEqual(manager.download_detail.text(), "")
        self.assertIn("취소", manager.status.text())
        self.assertEqual(manager.operation, "")
        manager._progress(99, 100)
        manager._download_activity({"status": "late download", "completed": 99, "total": 100})
        self.assertEqual(manager.download_detail.text(), "")

    def test_manager_closing_disallows_new_worker_and_late_activity(self):
        local = Mock(provider="ollama", base="http://localhost:11434")
        local.discover.return_value = [installed_row()]
        manager = LocalLLMManagerDialog(Translator(), service=local)
        self.addCleanup(manager.deleteLater)
        self.until(lambda: manager.worker is None and manager.models_verified_at is not None)
        entered, release = Event(), Event()
        replacement = Mock()

        def work(cancel, progress):
            entered.set()
            release.wait(3)
            manager.worker.activity.emit({
                "status": "pulling manifest", "completed": 1, "total": 2})
            return [installed_row()]

        manager._start("download", work)
        self.until(entered.is_set)
        original_worker = manager.worker
        manager.reject()
        self.assertTrue(manager._closing)
        self.assertTrue(original_worker.cancel.is_set())
        manager._start("refresh", replacement)
        self.assertIs(manager.worker, original_worker)
        release.set()
        self.until(lambda: manager.worker is None)
        replacement.assert_not_called()
        self.assertEqual(manager.download_detail.text(), "")
        self.assertEqual(manager.operation, "")

    def test_manager_close_keeps_original_probe_age(self):
        self.settings.setValue("translation/provider", "ollama")
        local = Mock(provider="ollama", base="http://localhost:11434")
        local.discover.return_value = [installed_row()]
        dialog = self.dialog(local_llm_factory=lambda *args: local)
        services = dialog.service_dialog
        self.until(lambda: dialog.worker is None and services.verified_connection() is not None)
        services._open_llm_manager()
        manager = services.manager
        self.until(lambda: manager.worker is None and manager.models_verified_at is not None)
        old_timestamp = time.monotonic() - 400
        manager.models_verified_at = old_timestamp
        manager.close()
        self.app.processEvents()
        self.assertEqual(services.connection[3], old_timestamp)
        self.assertIsNone(services.verified_connection())

    def test_manager_close_ignores_results_after_server_setting_changes(self):
        self.settings.setValue("translation/provider", "ollama")
        local = Mock(provider="ollama", base="http://localhost:11434")
        local.discover.return_value = [installed_row()]
        dialog = self.dialog(local_llm_factory=lambda *args: local)
        services = dialog.service_dialog
        self.until(lambda: dialog.worker is None and services.verified_connection() is not None)
        services._open_llm_manager()
        manager = services.manager
        self.until(lambda: manager.worker is None and manager.models_verified_at is not None)
        previous_connection = services.connection
        services.endpoint_edit.setText("http://localhost:11435/v1")
        manager.close()
        self.app.processEvents()
        self.assertEqual(services.connection, previous_connection)
        self.assertIsNone(services.verified_connection())


    def test_catalog_filter_with_no_matches_clears_stale_model_choice(self):
        service = Mock(provider="ollama")
        service.discover.return_value = [installed_row()]
        manager = LocalLLMManagerDialog(Translator(), service=service)
        self.addCleanup(manager.deleteLater)
        self.until(lambda: manager.worker is None)
        manager.tabs.setCurrentIndex(1)
        self.assertTrue(manager.download_name.text())
        self.assertTrue(manager.source_link.text())
        manager.catalog_search.setText("not-an-existing-model-000")
        self.assertIn("0개", manager.catalog_count.text())
        self.assertEqual(manager.download_name.text(), "")
        self.assertEqual(manager.source_link.text(), "")
        self.assertIn("검색 결과가 없습니다", manager.description.toPlainText())
        self.assertFalse(manager.download_button.isEnabled())
        manager._download()
        self.assertIn("추천 목록", manager.status.text())
        service.download.assert_not_called()
        manager.catalog_search.clear()
        self.assertTrue(manager.download_name.text())
        self.assertTrue(manager.download_button.isEnabled())
        self.assertTrue(manager.source_link.text())

    def test_manual_model_entry_works_without_visible_recommendations(self):
        service = Mock(provider="ollama")
        service.discover.return_value = []
        service.download.return_value = [installed_row("custom-model:3b")]
        manager = LocalLLMManagerDialog(Translator(), service=service)
        self.addCleanup(manager.deleteLater)
        self.until(lambda: manager.worker is None)
        manager.tabs.setCurrentIndex(1)
        manager.catalog_search.setText("no-results-000")
        manager.manual_toggle.setChecked(True)
        self.assertFalse(manager.download_button.isEnabled())
        manager.download_name.setText("custom-model:3b")
        self.assertTrue(manager.download_button.isEnabled())
        self.assertIn("입력한 모델", manager.download_button.text())
        self.assertIn("직접 입력한 모델", manager.description.toPlainText())
        self.assertEqual(manager.source_link.text(), "")
        manager._download()
        self.until(lambda: manager.worker is None)
        service.download.assert_called_once()
        self.assertEqual(service.download.call_args.args[0], "custom-model:3b")
        self.assertEqual(manager._selected()["id"], "custom-model:3b")

    def test_catalog_and_installed_filters_do_not_leave_old_model_details(self):
        service = Mock(provider="ollama")
        service.discover.return_value = [installed_row()]
        manager = LocalLLMManagerDialog(Translator(), service=service)
        self.addCleanup(manager.deleteLater)
        self.until(lambda: manager.worker is None and manager.table.rowCount() == 1)
        manager.tabs.setCurrentIndex(1)
        catalog_model = manager.download_name.text()
        self.assertTrue(manager.source_link.text())
        manager.manual_toggle.setChecked(True)
        manager.download_name.setText("special:3b")
        self.assertIn("직접 입력한 모델", manager.description.toPlainText())
        manager.manual_toggle.setChecked(False)
        self.assertEqual(manager.download_name.text(), catalog_model)
        self.assertTrue(manager.source_link.text())

        manager.tabs.setCurrentIndex(0)
        manager.table.selectRow(0)
        self.assertIn("qwen3:4b", manager.description.toPlainText())
        manager.search.setText("no-installed-result-000")
        self.assertIsNone(manager._selected())
        self.assertFalse(manager.use_button.isEnabled())
        self.assertEqual(manager.description.toPlainText(), "")
        self.assertEqual(manager.source_link.text(), "")

    def test_manager_download_shows_distinct_transfer_validation_and_installation(self):
        service = Mock(provider="ollama")
        service.discover.return_value = []
        manager = LocalLLMManagerDialog(Translator(), service=service)
        self.addCleanup(manager.deleteLater)
        self.until(lambda: manager.worker is None)
        entered, release = Event(), Event()

        def work(cancel, progress):
            entered.set()
            release.wait(3)
            raise TranslationCancelled()

        manager._start("download", work)
        self.until(entered.is_set)
        self.assertIn("모델 정보를 확인", manager.status.text())

        manager._download_activity({"status": "pulling manifest", "completed": 0, "total": 0})
        self.assertIn("모델 정보를 확인", manager.status.text())
        manager._download_activity({"status": "pulling 61aa...", "completed": 512 * 1024 ** 2,
                                    "total": 1024 ** 3})
        self.assertIn("다운로드 중", manager.status.text())
        self.assertIn("512.0 MiB / 1.0 GiB", manager.download_detail.text())
        manager._progress(50, 100)
        self.assertEqual(manager.progress.maximum(), 100)

        manager._download_activity({"status": "verifying sha256 digest", "completed": 0, "total": 0})
        self.assertIn("파일 검증 중", manager.status.text())
        self.assertEqual(manager.download_detail.text(), "")
        self.assertEqual(manager.progress.maximum(), 0)
        manager._progress(100, 100)
        self.assertEqual(manager.progress.maximum(), 0)
        manager._download_activity({"status": "writing manifest", "completed": 0, "total": 0})
        self.assertIn("설치 마무리", manager.status.text())
        manager._download_activity({"status": "success", "completed": 0, "total": 0})
        self.assertIn("설치된 모델 목록 확인 중", manager.status.text())
        self.assertEqual(manager.progress.maximum(), 0)

        manager._download_activity({"status": "Bearer secret-should-not-render",
                                    "completed": 0, "total": 0})
        self.assertIn("작업 진행 중", manager.status.text())
        self.assertNotIn("secret-should-not-render", manager.status.text())
        manager._cancel()
        release.set()
        self.until(lambda: manager.worker is None)
        self.assertIn("취소", manager.status.text())

    def test_manager_download_error_offers_retry_and_success_requires_listing(self):
        service = Mock(provider="ollama")
        service.discover.return_value = []
        service.download.side_effect = TranslationError("disk full")
        manager = LocalLLMManagerDialog(Translator(), service=service)
        self.addCleanup(manager.deleteLater)
        self.until(lambda: manager.worker is None)
        target = manager.download_name.text()
        self.assertEqual(target, "qwen3.5:9b")

        manager._download()
        self.until(lambda: manager.worker is None)
        self.assertIn("다운로드·설치 실패", manager.status.text())
        self.assertIn("disk full", manager.status.text())
        self.assertIn("재시도", manager.status.text())
        self.assertEqual(manager.download_name.text(), target)

        service.download.side_effect = None
        service.download.return_value = [installed_row(target)]
        manager._download()
        self.until(lambda: manager.worker is None)
        self.assertIn("설치 확인 완료", manager.status.text())
        self.assertEqual(manager._selected()["id"], target)
        self.assertEqual(service.download.call_count, 2)

    def test_manager_download_unlisted_result_does_not_claim_installed(self):
        service = Mock(provider="ollama")
        service.discover.return_value = []
        service.download.return_value = []
        manager = LocalLLMManagerDialog(Translator(), service=service)
        self.addCleanup(manager.deleteLater)
        self.until(lambda: manager.worker is None)
        manager._download()
        self.until(lambda: manager.worker is None)
        self.assertIsNone(manager._selected())
        self.assertIn("설치 모델을 목록에서 확인하지 못했습니다", manager.status.text())
        self.assertNotIn("설치 확인 완료", manager.status.text())


    def test_download_different_ollama_model_does_not_claim_requested_installation(self):
        service = Mock(provider="ollama")
        service.discover.return_value = [installed_row("existing:3b")]
        service.download.return_value = [installed_row("existing:3b"), installed_row("unrelated:7b")]
        manager = LocalLLMManagerDialog(Translator(), service=service)
        self.addCleanup(manager.deleteLater)
        self.until(lambda: manager.worker is None)
        manager._download()
        self.until(lambda: manager.worker is None)
        self.assertEqual(service.download.call_count, 1)
        self.assertEqual(service.download.call_args.args[0], "qwen3.5:9b")
        self.assertIsNone(manager._selected())
        self.assertIn("요청한 설치 모델을 목록에서 확인하지 못했습니다", manager.status.text())
        self.assertNotIn("설치 확인 완료", manager.status.text())

    def test_lmstudio_new_file_id_requires_manual_confirmation_not_verified_claim(self):
        service = Mock(provider="lmstudio")
        service.discover.return_value = []
        service.download.return_value = [installed_row("renamed-local-model.gguf")]
        manager = LocalLLMManagerDialog(Translator(), service=service)
        self.addCleanup(manager.deleteLater)
        self.until(lambda: manager.worker is None)
        manager._download()
        self.until(lambda: manager.worker is None)
        self.assertEqual(manager._selected()["id"], "renamed-local-model.gguf")
        self.assertIn("요청한 모델인지 확인", manager.status.text())
        self.assertNotIn("설치 확인 완료", manager.status.text())

    def test_lmstudio_unchanged_installed_list_is_not_download_success(self):
        service = Mock(provider="lmstudio")
        service.discover.return_value = [installed_row("old-model.gguf")]
        service.download.return_value = [installed_row("old-model.gguf")]
        manager = LocalLLMManagerDialog(Translator(), service=service)
        self.addCleanup(manager.deleteLater)
        self.until(lambda: manager.worker is None)
        manager._download()
        self.until(lambda: manager.worker is None)
        self.assertIsNone(manager._selected())
        self.assertIn("요청한 설치 모델을 목록에서 확인하지 못했습니다", manager.status.text())
        self.assertNotIn("설치 확인 완료", manager.status.text())


    def test_manager_can_use_already_installed_catalog_model_without_redownloading(self):
        service = Mock(provider="ollama")
        service.discover.return_value = [installed_row("qwen2.5-coder:7b")]
        manager = LocalLLMManagerDialog(Translator(), service=service,
                                        preferred_model="qwen2.5-coder:7b")
        self.addCleanup(manager.deleteLater)
        self.until(lambda: manager.worker is None and manager.table.rowCount() == 1)
        manager.tabs.setCurrentIndex(1)
        installed = next(manager.recommended.item(i) for i in range(manager.recommended.count())
            if manager.recommended.item(i).data(Qt.ItemDataRole.UserRole)["id"] == "qwen2.5-coder:7b")
        manager.catalog_category.setCurrentIndex(manager.catalog_category.findData("coder"))
        manager.recommended.setCurrentItem(installed)
        self.assertIn("설치됨", installed.text())
        self.assertIn("바로 사용", manager.download_button.text())
        manager._download()
        self.assertEqual(manager.selected_model, "qwen2.5-coder:7b")
        self.assertEqual(manager.result(), QDialog.DialogCode.Accepted)
        service.download.assert_not_called()

    def test_manager_missing_preference_does_not_silently_select_different_model(self):
        service = Mock(provider="ollama")
        service.discover.return_value = [installed_row("qwen3:4b")]
        manager = LocalLLMManagerDialog(Translator(), service=service,
                                        preferred_model="deleted:7b")
        self.addCleanup(manager.deleteLater)
        self.until(lambda: manager.worker is None and manager.table.rowCount() == 1)
        self.assertIsNone(manager._selected())
        self.assertIn("전에 선택한 모델", manager.status.text())
        manager.table.selectRow(0)
        manager._refresh()
        self.until(lambda: manager.worker is None)
        self.assertEqual(manager._selected()["id"], "qwen3:4b")

    def test_manager_delete_is_basic_action_with_confirmation_and_no_automatic_fallback(self):
        service = Mock(provider="ollama")
        service.discover.return_value = [installed_row("qwen3:4b"), installed_row("qwen2.5-coder:7b")]
        service.delete.return_value = [installed_row("qwen2.5-coder:7b")]
        manager = LocalLLMManagerDialog(Translator(), service=service)
        self.addCleanup(manager.deleteLater)
        self.until(lambda: manager.worker is None and manager.table.rowCount() == 2)
        self.assertIs(manager.delete_button.parentWidget(), manager.tabs.widget(0))
        manager.table.selectRow(0)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            manager._delete()
        self.until(lambda: manager.worker is None and manager.table.rowCount() == 1)
        service.delete.assert_called_once()
        self.assertEqual(service.delete.call_args.args[0], "qwen3:4b")
        self.assertIsNone(manager._selected())
        self.assertIn("다른 모델", manager.status.text())
        manager.table.selectRow(0)
        manager._use()
        self.assertEqual(manager.selected_model, "qwen2.5-coder:7b")

    def test_deleted_saved_model_is_not_replaced_with_first_model(self):
        self.settings.setValue("translation/provider", "ollama")
        self.settings.setValue("translation/config/ollama/model", "qwen3:8b")
        service = Mock(provider="ollama")
        service.discover.return_value = [installed_row("qwen3:4b")]
        dialog = self.dialog(local_llm_factory=lambda *args: service)
        self.until(lambda: dialog.worker is None and dialog.model_combo.count() == 1)
        self.assertEqual(dialog.model_combo.currentIndex(), -1)
        self.assertEqual(dialog._model_value(), "")

    def test_lyric_model_recommendations_download_using_existing_manager(self):
        service = Mock(provider="ollama")
        service.discover.return_value = []
        service.download.return_value = [installed_row("qwen3.5:9b")]
        manager = LocalLLMManagerDialog(Translator(), service=service)
        self.addCleanup(manager.deleteLater)
        self.until(lambda: manager.worker is None)
        self.assertEqual(manager.catalog_category.currentData(), "lyrics")
        self.assertEqual(manager.recommended.currentItem().data(Qt.ItemDataRole.UserRole)["id"], "qwen3.5:9b")
        self.assertIn("가사 번역", manager.recommended.currentItem().text())
        self.assertEqual(manager.download_name.text(), "qwen3.5:9b")
        manager._download()
        self.until(lambda: manager.worker is None)
        self.assertEqual(service.download.call_args.args[0], "qwen3.5:9b")
        self.assertEqual(manager._selected()["id"], "qwen3.5:9b")
        manager._use()
        self.assertEqual(manager.selected_model, "qwen3.5:9b")

    def test_manager_shows_tradeoffs_downloads_and_selects_installed_model(self):
        service = Mock(provider="ollama")
        service.discover.return_value = [installed_row()]
        service.download.return_value = [installed_row(), installed_row("qwen2.5-coder:3b")]
        manager = LocalLLMManagerDialog(Translator(), service=service)
        self.addCleanup(manager.deleteLater)
        self.until(lambda: manager.worker is None and manager.table.rowCount() == 1)
        manager.tabs.setCurrentIndex(1)
        self.assertGreaterEqual(manager.recommended.count(), 10)
        self.assertEqual(manager.catalog_category.currentData(), "lyrics")
        self.assertEqual(manager.recommended.item(0).data(Qt.ItemDataRole.UserRole)["id"], "qwen3.5:9b")
        manager.catalog_category.setCurrentIndex(manager.catalog_category.findData("coder"))
        self.assertTrue(any(manager.recommended.item(i).isHidden() for i in range(manager.recommended.count())))
        coder_small = next(i for i in range(manager.recommended.count())
            if manager.recommended.item(i).data(Qt.ItemDataRole.UserRole)["id"] == "qwen2.5-coder:3b")
        manager.recommended.setCurrentRow(coder_small)
        self.assertEqual(manager.download_name.text(), "qwen2.5-coder:3b")
        self.assertIn("장점:", manager.description.toPlainText())
        self.assertIn("주의:", manager.description.toPlainText())
        manager._download()
        self.until(lambda: manager.worker is None)
        self.assertEqual(manager.table.rowCount(), 2)
        self.assertEqual(manager._selected()["id"], "qwen2.5-coder:3b")
        manager._use()
        self.assertEqual(manager.selected_model, "qwen2.5-coder:3b")


if __name__ == "__main__":
    unittest.main()
