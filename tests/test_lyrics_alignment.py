"""Acoustic timestamps, safe partial results, cancellation and UI integration."""

from copy import deepcopy
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication, QDialog

from app.dialogs.lyrics_alignment_dialog import LyricsAlignmentDialog
from app.dialogs.track_details_dialog import TrackDetailsDialog
from app.models.playlist import PlaylistTrack
from app.services.lyrics_alignment_service import (
    AlignmentCancelled, AlignmentResult, LyricsAlignmentService, _AlignmentCache,
    _download, _run, align_recognition,
    ensure_runtime, MODEL_URL, RUNTIME_FILES,
)
from app.services.lyrics_service import LyricsService
from app.utils.i18n import Translator


def recognition(words):
    return {"transcription": [{"offsets": {"to": end * 1000}, "tokens": [
        {"id": index, "text": text, "t_dtw": end * 100, "p": probability,
         "offsets": {"from": start * 1000, "to": end * 1000}}
    ]} for index, (text, start, end, probability) in enumerate(words)]}


class LyricsAlignmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_karaoke_is_marked_beta_in_both_languages(self):
        from app.inspector.editors.lyrics_editor import LyricsSection

        track = PlaylistTrack("missing.wav", "Song", duration_seconds=5,
                              lyrics=[{"start": 0, "end": 5, "text": "Hello"}])
        for korean, marker in ((True, "베타"), (False, "Beta")):
            with self.subTest(korean=korean):
                dialog = LyricsAlignmentDialog(track, None, korean)
                self.addCleanup(dialog.deleteLater)
                self.addCleanup(dialog.close)
                self.assertIn(marker, dialog.windowTitle())
                self.assertIn(marker, dialog.display_group.title())
                self.assertIn(marker.lower(), dialog.label.text().lower())
                label = LyricsSection.LABELS["subtitle_karaoke_enabled"][0 if korean else 1]
                self.assertIn(marker, label)

    def test_word_times_follow_audio_and_preserve_text(self):
        cues = [{"start": 0, "end": 5, "text": "Hello, world!"}]
        before = deepcopy(cues)
        result, count = align_recognition(cues, recognition([
            (" Hello", 0.7, 1.2, 0.8), (" world!", 2.2, 2.9, 0.9)]))
        self.assertEqual(count, 1)
        self.assertEqual(cues, before)
        self.assertEqual([(word["start"], word["end"]) for word in result[0]["words"]], [(0.7, 1.2), (2.2, 2.9)])
        self.assertEqual([word["offset"] for word in result[0]["words"]], [0, 7])
        self.assertEqual(PlaylistTrack.from_dict(PlaylistTrack("", "Track", lyrics=result).to_dict()).lyrics, result)

    def test_dtw_output_boundaries_highlight_before_the_token_finishes(self):
        cues = [{"start": 0, "end": 8, "text": "hello world again"}]
        # Legacy token offsets can all precede DTW; the DTW values close each token.
        payload = recognition([("hello", 0.1, 1, 0.9), ("world", 0.1, 2, 0.9),
                               ("again", 0.1, 3, 0.9)])
        payload["transcription"][-1]["offsets"]["to"] = 8000  # Silence after the last word.
        result, count = align_recognition(cues, payload)
        self.assertEqual(count, 1)
        self.assertEqual([(word["start"], word["end"]) for word in result[0]["words"]],
                         [(0.1, 1), (1, 2), (2, 3)])
        self.assertGreater(LyricsService.word_reveal(result[0], 0.5)[0], 0)
        self.assertAlmostEqual(LyricsService.word_reveal(result[0], 1.1)[0], 6.5)
        self.assertEqual(LyricsService.word_reveal(result[0], 3), (17,))

    def test_split_utf8_korean_tokens_and_time_offset(self):
        encoded = " 하나".encode("utf-8")
        first, second = (part.decode("utf-8", errors="surrogateescape") for part in (encoded[:2], encoded[2:]))
        payload = recognition([(first, 0.5, 1, 0.8), (second, 0.5, 1, 0.9), (" 둘", 1.4, 2, 0.9)])
        result, count = align_recognition([{"start": 3, "end": 8, "text": "하나 둘"}], payload, 2, 1)
        self.assertEqual(count, 1)
        self.assertEqual([word["text"] for word in result[0]["words"]], ["하나", "둘"])
        self.assertAlmostEqual(result[0]["words"][0]["start"], 3.5)
        self.assertAlmostEqual(result[0]["words"][1]["start"], 4.4)

    def test_uncertain_or_missing_words_keep_entire_cue(self):
        cues = [{"start": 0, "end": 5, "text": "Hello world"}]
        for words in ([("hello", 0.5, 1, 0.8)], [("hello", 0.5, 1, 0.1), ("world", 1, 2, 0.8)]):
            with self.subTest(words=words):
                self.assertEqual(align_recognition(cues, recognition(words)), (cues, 0))
        with self.assertRaises(ValueError):
            align_recognition(cues, recognition([("Hello world", float("nan"), 1, 0.8)]))

    def test_partial_matches_preserve_cue_times_and_ignore_invalid_utf8(self):
        cues = [{"start": 1, "end": 3, "text": "hello bright world\n안녕 세상"}]
        payload = recognition([("hello", 1.2, 1.6, 0.9), ("\udcff", 1.6, 2, 0.8), ("world", 2.2, 2.7, 0.9)])
        result, count = align_recognition(cues, payload)
        self.assertEqual(count, 1)
        self.assertEqual([word["text"] for word in result[0]["words"]], ["hello", "world"])
        self.assertEqual((result[0]["start"], result[0]["end"]), (1, 3))
        self.assertEqual(PlaylistTrack.from_dict(PlaylistTrack("", "Track", lyrics=result).to_dict()).lyrics, result)
        self.assertEqual(LyricsService.word_reveal(result[0], 3)[1], 0)

    def test_recognition_drift_cannot_reorder_or_overlap_existing_cues(self):
        cues = [{"start": 1, "end": 2.5, "text": "hello world"},
                {"start": 2.5, "end": 5, "text": "next"}]
        result, count = align_recognition(cues, recognition([
            ("hello", 0.8, 1.7, 0.9), ("world", 1.7, 2.7, 0.9)]))
        self.assertEqual(count, 1)
        self.assertEqual([(cue["start"], cue["end"]) for cue in result], [(1, 2.5), (2.5, 5)])
        self.assertEqual(result[0]["words"][0]["start"], 1)
        self.assertEqual(result[0]["words"][-1]["end"], 2.5)
        self.assertEqual(PlaylistTrack.from_dict(PlaylistTrack("", "Track", lyrics=result).to_dict()).lyrics, result)

    def test_repeated_lines_preserve_order_and_imported_timings(self):
        cues = [{"start": 0, "end": 2, "text": "Hello"}, {"start": 2, "end": 5, "text": "Hello"}]
        result, count = align_recognition(cues, recognition([("hello", 0.2, 1, 0.9), ("hello", 2.5, 4, 0.9)]))
        self.assertEqual(count, 2)
        self.assertEqual([cue["words"][0]["start"] for cue in result], [0.2, 2.5])
        self.assertEqual(align_recognition(result, recognition([("wrong", 0.5, 2, 0.9)])), (result, 0))

    def test_original_and_translation_are_independent_and_can_be_added_later(self):
        cues = [{"start": 0, "end": 5, "text": "hello world\n안녕 세상"}]
        payload = recognition([("hello", 0.5, 1, 0.9), ("world", 1.5, 2, 0.9)])
        original, count = align_recognition(cues, payload, line_positions=(0, 1))
        self.assertEqual(count, 1)  # Unsung translation cannot prevent a correct original.
        self.assertEqual(LyricsService.word_reveal(original[0], 3), (11, 0))
        translated = recognition([("안녕", 0.5, 1, 0.9), ("세상", 1.5, 2, 0.9)])
        second, count = align_recognition(cues, translated, line_positions=(1,))
        self.assertEqual(count, 1)
        self.assertEqual(LyricsService.word_reveal(second[0], 3), (0, 5))
        both, count = align_recognition(original, translated, line_positions=(1,))
        self.assertEqual(count, 1)
        self.assertEqual(both[0]["words"][:2], original[0]["words"])
        self.assertEqual(PlaylistTrack.from_dict(PlaylistTrack("", "Track", lyrics=both).to_dict()).lyrics, both)
        self.assertEqual(LyricsService.word_reveal(both[0], 3), (11, 5))
        for result in (original, second, both):
            restored = LyricsService._parse_lrc(LyricsService.format_lrc(result))
            self.assertEqual(restored[0]["text"], cues[0]["text"])
            self.assertEqual(LyricsService.word_reveal(restored[0], 3), LyricsService.word_reveal(result[0], 3))

    def test_download_hash_failure_and_cancel_preserve_existing_file(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "model.bin"
            target.write_bytes(b"existing")
            cancel = threading.Event()
            response = io.BytesIO(b"download")
            response.headers = {"Content-Length": "8"}
            with patch("app.services.lyrics_alignment_service.urlopen", return_value=response):
                with self.assertRaises(RuntimeError):
                    _download("https://example.com/model", target, "wrong", 16, cancel, lambda *args: None)
            self.assertEqual(target.read_bytes(), b"existing")
            self.assertFalse(target.with_suffix(".bin.part").exists())
            cancel.set()
            response = io.BytesIO(b"download")
            response.headers = {}
            with patch("app.services.lyrics_alignment_service.urlopen", return_value=response), self.assertRaises(AlignmentCancelled):
                _download("https://example.com/model", target, sha256(b"download").hexdigest(), 16, cancel, lambda *args: None)
            self.assertEqual(target.read_bytes(), b"existing")

    def test_cancel_reaps_running_native_process(self):
        cancel = threading.Event()
        process = subprocess.Popen
        captured = []
        def observe(*args, **kwargs):
            child = process(*args, **kwargs)
            captured.append(child)
            return child
        timer = threading.Timer(0.2, cancel.set)
        timer.start()
        try:
            with patch("app.services.lyrics_alignment_service.subprocess.Popen", side_effect=observe), self.assertRaises(AlignmentCancelled):
                _run([sys.executable, "-c", "import time; time.sleep(30)"], cancel)
            self.assertIsNotNone(captured[0].poll())
        finally:
            timer.join()

    def test_tiny_download_is_verified_and_runtime_is_reused(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (*RUNTIME_FILES, "runtime.ready", "ggml-tiny-q5_1.bin"):
                (root / name).write_bytes(b"existing")
            with patch("app.services.lyrics_alignment_service.sys.platform", "win32"), \
                 patch("app.services.lyrics_alignment_service.MODEL_SHA256", sha256(b"tiny").hexdigest()), \
                 patch("app.services.lyrics_alignment_service._download",
                       side_effect=lambda _url, target, *_args: target.write_bytes(b"tiny")) as download:
                _executable, model = ensure_runtime(root, threading.Event(), lambda *args: None)
                self.assertEqual(model.name, "ggml-tiny-q5_1.bin")
                self.assertEqual(download.call_count, 1)
                self.assertEqual(download.call_args.args[2], sha256(b"tiny").hexdigest())
                self.assertEqual(download.call_args.args[0], MODEL_URL)
                self.assertEqual(ensure_runtime(root, threading.Event(), lambda *args: None)[1], model)
                self.assertEqual(download.call_count, 1)

    def test_service_cache_and_no_recognition_during_playback(self):
        with TemporaryDirectory() as directory:
            audio = Path(directory) / "audio.wav"
            audio.write_bytes(b"source")
            track = PlaylistTrack(str(audio), "Track", duration_seconds=5,
                                  lyrics=[{"start": 0, "end": 5, "text": "hello world"}])
            before = track.to_dict()
            payload = recognition([("hello", 0.8, 1.3, 0.9), ("world", 2, 3, 0.9)])
            def run(command, *args):
                if "-ojf" in command:
                    Path(command[command.index("-of") + 1]).with_suffix(".json").write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(_AlignmentCache, "default_root", return_value=Path(directory) / "cache"), \
                 patch("app.services.lyrics_alignment_service.ensure_runtime", return_value=(Path("whisper.exe"), Path("tiny.bin"))) as runtime, \
                 patch.object(LyricsAlignmentService, "__init__", return_value=None), \
                 patch("app.services.lyrics_alignment_service.FFmpegRenderer.find_executable", return_value=Path("ffmpeg.exe")), \
                 patch("app.services.lyrics_alignment_service._run", side_effect=run) as execute:
                service = LyricsAlignmentService()
                service.root = Path(directory)
                result = service.analyze(track, None, threading.Event(), lambda *args: None)
                self.assertEqual(result.matched, 1)
                self.assertEqual(execute.call_count, 2)
                with patch("app.services.lyrics_alignment_service.FFmpegRenderer.find_executable",
                           side_effect=RuntimeError("FFmpeg unavailable")):
                    self.assertEqual(service.analyze(track, None, threading.Event(), lambda *args: None), result)
                self.assertEqual(execute.call_count, 2)
                self.assertEqual(runtime.call_count, 1)
                command = execute.call_args.args[0]
                self.assertEqual(command[command.index("-dtw") + 1], "tiny")
                self.assertEqual(track.to_dict(), before)
                timed = deepcopy(track)
                timed.lyrics = deepcopy(result.cues)
                timed.lyrics[0]["text"] += "\n번역"
                translation = {"offset": 12, "text": "번역", "start": 1, "end": 3}
                timed.lyrics[0]["words"].append(translation)
                timed.lyrics[0]["words"][0]["start"] = 1
                timed_before = timed.to_dict()
                refreshed = service.analyze(timed, None, threading.Event(), lambda *args: None, use_cache=False)
                self.assertEqual(refreshed.matched, 1)
                self.assertEqual(refreshed.cues[0]["words"][0]["start"], 0.8)
                self.assertEqual(refreshed.cues[0]["words"][-1], translation)
                self.assertEqual(timed.to_dict(), timed_before)
                self.assertEqual(PlaylistTrack.from_dict(timed.to_dict() | {"lyrics": refreshed.cues}).lyrics,
                                 refreshed.cues)
                execute.reset_mock()
                with patch.object(_AlignmentCache, "load", return_value=AlignmentResult(track.lyrics, 0, 1).to_cache_fields()):
                    self.assertEqual(service.analyze(track, None, threading.Event(), lambda *args: None).matched, 1)
                    self.assertEqual(execute.call_count, 2)  # Never reuse a cached zero result.
                track.duration_seconds = 35
                track.lyrics.append({"start": 30, "end": 35, "text": "hello world"})
                attempts = []
                def fail_one_window(command, *args):
                    if "-ojf" in command:
                        attempts.append(command)
                        if len(attempts) == 1:
                            raise RuntimeError("Unusable window")
                        run(command, *args)
                execute.side_effect = fail_one_window
                partial = service.analyze(track, None, threading.Event(), lambda *args: None, use_cache=False)
                self.assertEqual((partial.matched, partial.total), (1, 2))
                self.assertNotIn("words", partial.cues[0])
                self.assertIn("words", partial.cues[1])
                execute.side_effect = AlignmentCancelled
                with self.assertRaises(AlignmentCancelled):
                    service.analyze(track, None, threading.Event(), lambda *args: None, use_cache=False)

    def test_progress_cancel_does_not_apply_partial_results(self):
        def recognize(_self, track, ffmpeg, cancel, progress, line_positions, **_kwargs):
            while not cancel.wait(0.01):
                pass
            raise AlignmentCancelled()
        track = PlaylistTrack("", "Track", lyrics=[{"start": 0, "end": 5, "text": "hello"}])
        with patch.object(LyricsAlignmentService, "analyze", recognize):
            dialog = LyricsAlignmentDialog(track, None, True)
            self.assertIsNone(dialog.worker)  # Opening the dialog never downloads or analyzes.
            dialog._start()
            QTimer.singleShot(20, dialog.reject)
            self.assertEqual(dialog.exec(), QDialog.DialogCode.Rejected)
            self.assertFalse(dialog.worker.isRunning())
            self.assertIsNone(dialog.result_data)

    def test_review_can_apply_only_second_line_or_discard_everything(self):
        track = PlaylistTrack("", "Track", lyrics=[{"start": 0, "end": 5, "text": "Hello\nHello"}])
        aligned, count = align_recognition(track.lyrics, recognition([("hello", 1, 2, 0.9)]), line_positions=(0, 1))
        result = AlignmentResult(aligned, count, 2)
        def recognize(*_args, **_kwargs):
            return deepcopy(result)
        with patch.object(LyricsAlignmentService, "analyze", recognize):
            dialog = LyricsAlignmentDialog(track, None, True)
            self.assertEqual(dialog.table.item(1, 0).checkState(), Qt.CheckState.Unchecked)
            dialog.table.item(1, 0).setCheckState(Qt.CheckState.Checked)
            dialog._start()
            # Wait for the result and native thread cleanup without accepting the dialog.
            dialog.worker.wait(3000)
            self.app.processEvents()
            self.assertEqual(len(dialog.review_rows), 2)
            self.assertIsNone(dialog.result_data)
            dialog.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
            dialog._apply()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            self.assertEqual(dialog.result_data.matched, 1)
            self.assertEqual(LyricsService.word_reveal(dialog.result_data.cues[0], 3), (0, 5))
            self.assertNotIn("words", track.lyrics[0])
            discard = LyricsAlignmentDialog(track, None, True)
            discard._start()
            discard.worker.wait(3000)
            self.app.processEvents()
            discard.reject()
            self.assertEqual(discard.result(), QDialog.DialogCode.Rejected)
            self.assertIsNone(discard.result_data)

    def test_review_replaces_selected_existing_word_times_without_duplicates(self):
        track = PlaylistTrack("", "Track", lyrics=[{"start": 0, "end": 5, "text": "hello world\n번역", "words": [
            {"offset": 0, "text": "hello", "start": 1, "end": 2},
            {"offset": 6, "text": "world", "start": 2, "end": 3},
            {"offset": 12, "text": "번역", "start": 1, "end": 3},
        ]}])
        before = track.to_dict()
        cues = deepcopy(track.lyrics)
        cues[0]["words"][0].update(start=0.2, end=0.7)
        cues[0]["words"][1].update(start=0.7, end=1.3)
        with patch.object(LyricsAlignmentService, "analyze", return_value=AlignmentResult(cues, 1, 1)) as analyze:
            dialog = LyricsAlignmentDialog(track, None, True)
            dialog._start()
            dialog.worker.wait(3000)
            self.app.processEvents()
            self.assertFalse(analyze.call_args.kwargs["use_cache"])
            self.assertEqual(len(dialog.review_rows[0][3]), 2)
            dialog._apply()
        self.assertEqual(dialog.result_data.cues, cues)
        self.assertEqual(track.to_dict(), before)
        self.assertEqual(PlaylistTrack.from_dict(track.to_dict() | {"lyrics": dialog.result_data.cues}).lyrics, cues)

    def test_simple_display_applies_colors_without_any_recognition(self):
        track = PlaylistTrack("", "Track", lyrics=[{"start": 0, "end": 5, "text": "Hello\n안녕"}])
        dialog = LyricsAlignmentDialog(track, None, True)
        self.assertTrue(dialog.simple_button.isDefault())
        dialog.colors.widgets["subtitle_karaoke_pending_brightness"].setValue(25)
        self.assertNotIn("subtitle_karaoke_analysis_model", dialog.colors.widgets)
        dialog.colors.widgets["subtitle_karaoke_offset"].setValue(0.2)
        with patch.object(LyricsAlignmentService, "analyze") as analyze:
            dialog._use_cue_times()
            analyze.assert_not_called()
        self.assertEqual(dialog.result_data.cues, track.lyrics)
        settings = dialog.karaoke_settings()
        self.assertTrue(settings["subtitle_karaoke_enabled"])
        self.assertEqual(settings["subtitle_karaoke_lines"], [0])
        self.assertEqual(settings["subtitle_karaoke_pending_brightness"], 0.25)
        self.assertNotIn("subtitle_karaoke_analysis_model", settings)
        self.assertEqual(settings["subtitle_karaoke_offset"], 0.2)

    def test_failed_or_empty_analysis_can_apply_fallback_and_colors(self):
        track = PlaylistTrack("", "Track", lyrics=[{"start": 0, "end": 5, "text": "Hello\n안녕"}])
        for outcome in (RuntimeError("Download unavailable"), AlignmentResult(deepcopy(track.lyrics), 0, 1)):
            with self.subTest(outcome=outcome), patch.object(LyricsAlignmentService, "analyze") as analyze:
                if isinstance(outcome, Exception):
                    analyze.side_effect = outcome
                else:
                    analyze.return_value = outcome
                dialog = LyricsAlignmentDialog(track, None, True)
                dialog.show()
                dialog._start()
                dialog.worker.wait(3000)
                self.app.processEvents()
                self.assertTrue(dialog.isVisible())
                self.assertFalse(dialog.display_group.isHidden())
                self.assertTrue(dialog.apply_button.isEnabled())
                self.assertIn("줄 시간으로 추정", dialog.table.item(0, 3).text())
                dialog._apply()
                self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
                self.assertEqual(dialog.result_data.cues, track.lyrics)
                self.assertEqual(dialog.result_data.matched, 0)
                self.assertEqual(dialog.karaoke_settings()["subtitle_karaoke_lines"], [0])

    def test_track_dialog_stages_word_times_until_apply(self):
        track = PlaylistTrack("", "Track", duration_seconds=5, lyrics=[{"start": 0, "end": 5, "text": "hello"}])
        aligned = deepcopy(track.lyrics)
        aligned[0]["words"] = [{"offset": 0, "text": "hello", "start": 1, "end": 2}]
        class FakeDialog:
            result_data = AlignmentResult(aligned, 1, 1)
            error = ""
            def __init__(self, *args, **kwargs):
                pass
            def exec(self):
                return QDialog.DialogCode.Accepted
        dialog = TrackDetailsDialog(track, Translator())
        try:
            with patch("app.dialogs.lyrics_alignment_dialog.LyricsAlignmentDialog", FakeDialog), \
                 patch("app.dialogs.track_details_dialog.QMessageBox.information"):
                dialog._align_words_to_audio()
            self.assertEqual(dialog.selected_lyrics, aligned)
            self.assertNotIn("words", track.lyrics[0])
            self.assertTrue(dialog.selected_karaoke_settings["subtitle_karaoke_enabled"])
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()
