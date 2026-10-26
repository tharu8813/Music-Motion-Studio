"""Regression scenarios for the two October audits: preserve work and output intent."""

from concurrent.futures import Future
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import subprocess
import threading
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QDialog, QMessageBox

from app.animation.curves import animation_pose
from app.dialogs.lyrics_alignment_dialog import LyricsAlignmentDialog
from app.models.playlist import PlaylistTrack
from app.models.project import ProjectContent, ProjectDocument
from app.models.source import Source, SourceType
from app.renderer.ffmpeg.filter_graph import filter_color, layered_filter_graph, video_fade_expression
from app.renderer.ffmpeg_renderer import RenderError, VideoClipOverlay
from app.services import lyrics_intro_service as intro_service
from app.services.autosave_service import AutosaveService
from app.services.autosave_worker import AutosaveWorker
from app.services.export_validation_service import validate_export_output
from app.services.project_media_service import ProjectMediaService
from app.services.project_service import ProjectError, ProjectLoadCancelled, ProjectService
from app.services.subtitle_export_service import SubtitleExportService
from app.timeline.compiler import compile_playlist
from tests.main_window_base import MainWindowTestCase


class AuditPersistenceTests(unittest.TestCase):
    def test_sessions_keep_independent_recoveries_for_both_saved_and_untitled_projects(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            first, second = AutosaveService(root), AutosaveService(root)
            for project in (None, root / "shared.json"):
                a = first.save(ProjectDocument(sources=[Source(SourceType.TEXT, "First")]), project)
                b = second.save(ProjectDocument(sources=[Source(SourceType.TEXT, "Second")]), project)
                self.assertNotEqual(a.path, b.path)
                first.clear(project)
                self.assertFalse(a.path.exists())
                self.assertEqual(second.load_snapshot(b.path).document.sources[0].name, "Second")
            legacy = second.save(ProjectDocument(), None).path
            legacy.rename(legacy.parent / "legacy.recovery.json")
            self.assertTrue(any(s.path.name == "legacy.recovery.json" for s in first.recoveries()))

    def test_invalid_autosave_keeps_the_previous_recoverable_document(self):
        with TemporaryDirectory() as raw:
            service = AutosaveService(Path(raw))
            previous = service.save(ProjectDocument(), None)
            invalid = ProjectDocument(sources=[Source(SourceType.TEXT, "")]).to_dict()
            with self.assertRaises(ProjectError):
                service.write_document_data(invalid, None)
            self.assertEqual(service.load_snapshot(previous.path).document.sources, [])

    def test_late_autosave_cannot_reappear_after_manual_save_clears_it(self):
        with TemporaryDirectory() as raw:
            service = AutosaveService(Path(raw))
            worker = AutosaveWorker(service, ProjectDocument().to_dict(), None)
            captured_after = datetime.now(UTC)
            service.clear(None)
            worker.run()
            self.assertLessEqual(worker._saved_at, captured_after)
            self.assertEqual(service.recoveries(), [])

    def test_cancelled_reload_preserves_media_already_used_by_an_open_document(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            audio = root / "original.wav"
            payload = b"original-media" * 180_000
            audio.write_bytes(payload)
            package = root / "work.pvsproj"
            ProjectService.save(package, ProjectDocument(playlist=[PlaylistTrack(str(audio), "Song")]))
            cached = Path(ProjectService.load(package).playlist[0].file_path)
            for mid_extract in (False, True):
                cancel = threading.Event()
                if not mid_extract:
                    cancel.set()
                def progress(extracted, _total):
                    if mid_extract and extracted:
                        cancel.set()
                with self.assertRaises(ProjectLoadCancelled):
                    ProjectService.load(package, cancel_event=cancel, progress=progress)
                self.assertEqual(cached.read_bytes(), payload)
                self.assertEqual(list(cached.parent.glob("*.tmp")), [])

    def test_embed_missing_audio_fails_without_replacing_existing_project(self):
        with TemporaryDirectory() as raw:
            target = Path(raw) / "work.pvsproj"
            target.write_bytes(b"previous project")
            document = ProjectDocument(playlist=[PlaylistTrack(str(Path(raw) / "missing.wav"), "Song")])
            with self.assertRaisesRegex(ProjectError, "missing media"):
                ProjectService.save(target, document)
            self.assertEqual(target.read_bytes(), b"previous project")
            document.settings.content_mode = "reference"
            ProjectService.save(target, document)
            self.assertEqual(ProjectService.load(target).playlist[0].file_path, document.playlist[0].file_path)

    def test_embed_keeps_lyrics_when_the_original_sidecar_has_disappeared(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            audio = root / "song.wav"
            audio.write_bytes(b"audio")
            track = PlaylistTrack(str(audio), "Song", lyrics_path=str(root / "missing.lrc"),
                                  lyrics=[{"start": 1, "end": 3, "text": "Keep words"}])
            target = ProjectService.save(root / "work.pvsproj", ProjectDocument(
                playlist=[track], content_library=[ProjectContent(track.lyrics_path, "lyrics")],
            ))
            loaded = ProjectService.load(target).playlist[0]
            self.assertEqual(loaded.lyrics, track.lyrics)
            self.assertEqual(loaded.lyrics_path, "")
            self.assertEqual(ProjectService.load(target).content_library, [])

    def test_relink_preserves_an_excluded_tracks_enabled_state(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            replacement = root / "found.wav"
            replacement.write_bytes(b"audio")
            track = PlaylistTrack(str(root / "missing.wav"), "Excluded", enabled=False)
            document = ProjectDocument(playlist=[track])
            missing = ProjectMediaService.validate(document, root / "work.json")
            missing[0].replacement_path = str(replacement)
            ProjectMediaService.apply_replacements(document, missing)
            self.assertFalse(track.enabled)
            self.assertEqual(track.file_path, str(replacement.resolve()))


class AuditOutputTests(unittest.TestCase):
    def test_subtitles_combine_source_and_track_offsets_before_mapping_the_timeline(self):
        track = PlaylistTrack("song.wav", "Song", duration_seconds=30, lyrics_timing_offset_seconds=0.5,
                              lyrics=[{"start": 10, "end": 15, "text": "Words"}])
        cue = SubtitleExportService.cues([track], compile_playlist([track]), timing_offset=2)[0]
        self.assertEqual((cue.start, cue.end), (7.5, 12.5))

    def test_probe_timeout_returns_an_unavailable_check_and_preserves_the_output(self):
        with TemporaryDirectory() as raw:
            output = Path(raw) / "video.mp4"
            output.write_bytes(b"finished video")
            with patch("app.services.export_validation_service._find_ffprobe", return_value=Path("ffprobe")), \
                 patch("app.services.export_validation_service.subprocess.run", side_effect=subprocess.TimeoutExpired("ffprobe", 15)):
                result = validate_export_output(output, expected_width=1920, expected_height=1080,
                                                expected_fps=30, expected_duration_seconds=5)
            self.assertFalse(result.available)
            self.assertIn("timed out", result.error)
            self.assertEqual(output.read_bytes(), b"finished video")

    def test_video_padding_converts_argb_and_is_added_after_color_adjustment(self):
        self.assertEqual(filter_color("#80112233"), "0x11223380")
        self.assertEqual(filter_color("#00112233"), "0x11223300")
        self.assertEqual(filter_color("#112233"), "0x112233")
        clip = VideoClipOverlay(Path("clip.mp4"), 0, 4, 0, 0, 0, 200, 100, 0,
                                fit_mode="contain", fill_color="#80112233", brightness=20)
        graph = layered_filter_graph([], [], 30, 200, 100, [clip])
        self.assertIn("format=rgba,pad=200:100:(ow-iw)/2:(oh-ih)/2:color=0x11223380", graph)
        self.assertLess(graph.index("eq=brightness"), graph.index("pad=200:100"))

    def test_video_fades_follow_preview_curves_and_do_not_restart_for_each_media_clip(self):
        clip = VideoClipOverlay(Path("clip.mp4"), 0.2, 3.8, 0, 0, 0, 200, 100, 0,
                                animation_in="fade", animation_out="fade",
                                animation_windows=((0, 4, 1, 1),))
        expression = video_fade_expression(clip).replace("if(", "choose(")
        functions = {"choose": lambda p, a, b: a if p else b, "lt": lambda a, b: a < b,
                     "between": lambda x, a, b: a <= x <= b, "clip": lambda x, a, b: max(a, min(b, x)), "pow": pow}
        for absolute in (0.2, 0.5, 1, 2, 3, 3.5, 3.9):
            expected = (animation_pose("fade", absolute, True, 200, 100).opacity if absolute < 1 else
                        animation_pose("fade", absolute - 3, False, 200, 100).opacity if absolute >= 3 else 1)
            actual = eval(expression, {"__builtins__": {}}, functions | {"T": absolute - clip.timeline_start})
            self.assertAlmostEqual(actual, expected)
        self.assertIn("geq=", layered_filter_graph([], [], 30, 200, 100, [clip]))


class AuditWorkspaceTests(MainWindowTestCase):
    def test_recovery_preserves_another_windows_edits_after_a_newer_save(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            project = root / "shared.json"
            other = AutosaveService(root)
            recovered = ProjectDocument(sources=[Source(SourceType.TEXT, "Unsaved in A", text="Keep A")])
            other.write_document_data(recovered.to_dict(), project, datetime(2000, 1, 1, tzinfo=UTC))
            ProjectService.save(project, ProjectDocument(sources=[Source(SourceType.TEXT, "Saved in B")]))
            self.window.autosave = AutosaveService(root)
            self.assertTrue(self.window._offer_recovery(confirm=False))
            self.assertEqual(self.window.store.sources()[0].text, "Keep A")
            self.assertEqual(ProjectService.load(project).sources[0].name, "Saved in B")
            self.assertTrue(self.window.project_session.dirty)

    def test_saved_workspace_accepts_a_dropped_project(self):
        self.window.project_session.dirty = False
        path = Path("drop.pvsproj")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes) as question, \
             patch.object(self.window, "_load_project_path", return_value=True) as load:
            self.window._confirm_and_load_dropped_project(path)
        self.assertIs(question.call_args.args[0], self.window)
        load.assert_called_once_with(path)

    def test_invalid_subtitle_import_keeps_the_previous_lyrics_and_project_savable(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            audio = root / "song.wav"
            audio.write_bytes(b"audio")
            track = PlaylistTrack(str(audio), "Song", lyrics=[{"start": 0, "end": 5, "text": "Keep me"}])
            self.window.playlist_service.replace([track])
            subtitle = root / "invalid.srt"
            subtitle.write_text("1\n00:00:10,000 --> 00:00:05,000\nBad\n", encoding="utf-8")
            with patch.object(QMessageBox, "warning"):
                self.assertFalse(self.window._attach_lyrics_to_track(subtitle, track.id))
            self.assertEqual(track.lyrics[0]["text"], "Keep me")
            saved = ProjectService.save(root / "work.pvsproj", self.window._project_document())
            self.assertEqual(ProjectService.load(saved).playlist[0].lyrics, track.lyrics)

    def test_delayed_background_autosave_does_not_replace_a_newer_manual_save(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            self.window.autosave = AutosaveService(root)
            self.window.current_project_path = root / "latest.json"
            source = Source(SourceType.TEXT, "Title", text="Old snapshot")
            self.window.store.replace([source])
            started, release = threading.Event(), threading.Event()
            original = self.window.autosave.write_document_data
            def delayed(*args, **kwargs):
                started.set()
                if not release.wait(10):
                    raise RuntimeError("Test writer was not released")
                return original(*args, **kwargs)
            try:
                with patch.object(self.window.autosave, "write_document_data", side_effect=delayed):
                    self.window._autosave_project()
                    self.assertTrue(started.wait(3))
                    worker = self.window.autosave_controller.worker
                    self.window.store.update(source.id, text="New saved work")
                    self.assertTrue(self.window._save_project(wait_for_completion=True))
                    release.set()
                    self.assertTrue(worker.wait(5000))
                self.application.processEvents()
                self.assertEqual(ProjectService.load(self.window.current_project_path).sources[0].text, "New saved work")
                self.assertIsNone(self.window.autosave.latest_recovery())
                self.assertFalse(self.window._offer_recovery(confirm=False))
            finally:
                release.set()
                if self.window.autosave_controller.worker is not None:
                    self.window.autosave_controller.worker.wait()

    def test_update_keeps_editor_open_until_setup_is_ready_and_recaptures_new_edits(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            installer = root / "setup.exe"
            installer.write_bytes(b"verified")
            self.window.autosave = AutosaveService(root)
            source = Source(SourceType.TEXT, "Title", text="Before UAC")
            self.window.store.replace([source])
            self.window.current_project_path = None
            with patch("app.ui.main_window.launch_setup", return_value=True) as launch, \
                 patch("app.ui.main_window.QTimer.singleShot"), patch.object(self.window, "close") as close:
                self.window._launch_update_setup(installer)
                arguments = launch.call_args.args[1]
                ready = Path(next(value.split("=", 1)[1] for value in arguments if value.startswith("/UPDATEREADY=")))
                recovery = Path(next(value.split("=", 1)[1] for value in arguments if value.startswith("/UPDATERECOVERY=")))
                close.assert_not_called()
                self.assertFalse(self.window._update_install_authorized)
                self.window.store.update(source.id, text="Edited during UAC")
                self.window.current_project_path = root / "saved-as.json"
                ready.write_text("ready", encoding="utf-8")
                self.window._wait_for_update_setup(ready, recovery, 1)
                close.assert_called_once()
                snapshot = self.window.autosave.load_snapshot(recovery)
                self.assertEqual(snapshot.document.sources[0].text, "Edited during UAC")
                self.assertEqual(snapshot.project_path, self.window.current_project_path)
            self.window._update_install_authorized = False

    def test_cancelled_update_authorization_never_closes_the_editor(self):
        with TemporaryDirectory() as raw:
            self.window._pending_update_installer = Path(raw) / "setup.exe"
            with patch.object(self.window, "close") as close:
                self.window._wait_for_update_setup(Path(raw) / "no-ready-file", Path(raw) / "recovery", 0)
                close.assert_not_called()
            self.assertIsNone(self.window._pending_update_installer)
            self.assertFalse(self.window._update_install_authorized)

    def test_undo_and_redo_after_saving_mark_the_workspace_unsaved(self):
        source = Source(SourceType.TEXT, "Title", text="Before")
        self.window.store.replace([source])
        self.window.history.reset(self.window._project_document().to_dict())
        self.window.store.update(source.id, text="Saved")
        self.window.history_controller.flush_pending()
        self.window.project_session.dirty = False
        before_serial = self.window.project_session.change_serial
        self.window.history_controller.undo()
        self.assertEqual(self.window.store.get(source.id).text, "Before")
        self.assertTrue(self.window.project_session.dirty)
        self.assertGreater(self.window.project_session.change_serial, before_serial)
        self.assertTrue(self.window._autosave_debounce_timer.isActive())
        self.window.project_session.dirty = False
        self.window.history_controller.redo()
        self.assertEqual(self.window.store.get(source.id).text, "Saved")
        self.assertTrue(self.window.project_session.dirty)

    def test_blank_source_name_reverts_in_inspector_and_remains_recoverable(self):
        source = Source(SourceType.TEXT, "Title")
        self.window.store.add(source)
        inspector = self.window.inspector
        inspector.name_edit.setText("  ")
        inspector.name_edit.editingFinished.emit()
        self.assertEqual(source.name, "Title")
        self.assertEqual(inspector.name_edit.text(), "Title")
        with TemporaryDirectory() as raw:
            service = AutosaveService(Path(raw))
            service.save(self.window._project_document(), None)
            self.assertEqual(len(service.recoveries()), 1)

    def test_subtitle_export_uses_the_chosen_lyrics_elements_offset(self):
        first = Source(SourceType.LYRICS, "First", subtitle_timing_offset=1)
        second = Source(SourceType.LYRICS, "Second", subtitle_timing_offset=2)
        self.window.store.replace([first, second])
        self.window.store.select(second.id)
        track = PlaylistTrack("song.wav", "Song", duration_seconds=30,
                              lyrics=[{"start": 10, "end": 15, "text": "Words"}])
        with TemporaryDirectory() as raw:
            output = Path(raw) / "words.srt"
            def choose(_parent, _title, _label, labels, selected, _editable):
                self.assertEqual(selected, 1)
                return labels[1], True
            with patch("app.controllers.export_controller.QInputDialog.getItem", side_effect=choose), \
                 patch.object(self.window.export_orchestrator, "show_complete_dialog"):
                self.window.export_orchestrator.export_subtitles([track], str(output))
            self.assertIn("00:00:08,000 --> 00:00:13,000", output.read_text(encoding="utf-8"))

    def test_relinked_project_is_dirty_and_autosaves_the_new_paths(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            replacement = root / "found.wav"
            replacement.write_bytes(b"audio")
            original = str(root / "missing.wav")
            project = ProjectService.save(root / "project.json", ProjectDocument(playlist=[PlaylistTrack(original, "Song")]))
            def relink(dialog):
                for entry in dialog.media:
                    entry.replacement_path = str(replacement)
                return QDialog.DialogCode.Accepted
            with patch("app.controllers.project_controller.MissingMediaDialog.exec", new=relink), \
                 patch.object(self.window, "_offer_legacy_upgrade"):
                self.assertTrue(self.window._load_project_path(project))
            self.assertTrue(self.window.project_session.dirty)
            self.assertTrue(self.window._autosave_debounce_timer.isActive())
            self.assertEqual(self.window.playlist_service.tracks[0].file_path, str(replacement.resolve()))
            self.assertEqual(ProjectService.load(project).playlist[0].file_path, original)

    def test_video_fade_is_exported_and_unsupported_effects_are_explicit(self):
        with TemporaryDirectory() as raw:
            video = Path(raw) / "clip.mp4"
            video.write_bytes(b"video fixture")
            source = Source(SourceType.VIDEO, "Video", video_paths=[str(video)], animation_in="fade")
            self.window.store.replace([source])
            self.window.store.select(source.id)
            spin_index = self.window.inspector.animation_in_combo.findData("spin")
            self.assertFalse(self.window.inspector.animation_in_combo.model().item(spin_index).isEnabled())
            track = PlaylistTrack("song.wav", "Song", duration_seconds=4)
            with patch("app.controllers.export_controller.PlaylistService._probe_duration", return_value=4):
                faded = self.window._export_video_clips([track], 4)
                source.animation_in = "none"
                plain = self.window._export_video_clips([track], 4)
                self.assertNotEqual(faded, plain)
                self.assertEqual(faded[0].animation_windows, ((0, 4, 0.45, 0.45),))
                source.animation_in = "spin"
                with self.assertRaises(RenderError):
                    self.window._export_video_clips([track], 4)
            self.window.store.add(Source(SourceType.TEXT, "Text"))
            self.assertTrue(self.window.inspector.animation_in_combo.model().item(spin_index).isEnabled())

    def test_analysis_error_is_visible_while_cue_time_fallback_remains_available(self):
        track = PlaylistTrack("song.wav", "Song", duration_seconds=5,
                              lyrics=[{"start": 0, "end": 5, "text": "Hello world"}])
        dialog = LyricsAlignmentDialog(track, None, True)
        try:
            dialog.line_positions = (0,)
            dialog.worker = SimpleNamespace(wait=lambda: None, cancel_event=threading.Event(), isRunning=lambda: False)
            dialog._failed("RECOGNITION_DOWNLOAD_FAILED")
            dialog._finished()
            self.assertIn("RECOGNITION_DOWNLOAD_FAILED", dialog.label.text())
            self.assertIn("실패", dialog.label.text())
            self.assertFalse(dialog.apply_button.isHidden())
            self.assertTrue(dialog.apply_button.isEnabled())
        finally:
            dialog.worker = None
            dialog.close()

    def test_failed_interlude_analysis_retries_after_tool_change_or_cooldown(self):
        with TemporaryDirectory() as raw:
            path = Path(raw) / "song.wav"
            path.write_bytes(b"audio")
            track = PlaylistTrack(str(path), "Song", duration_seconds=40,
                lyrics=[{"start": 1, "end": 5, "text": "First"}, {"start": 25, "end": 30, "text": "Next"}])
            source = Source(SourceType.LYRICS, "Lyrics", subtitle_intro_midtrack=True)
            failed = Future(); failed.set_result(None)
            with patch.object(intro_service, "_jobs", {}), patch.object(intro_service, "_submitted_at", {}), \
                 patch.object(intro_service, "_executor") as executor, patch.object(intro_service, "QSettings") as settings, \
                 patch.object(intro_service.time, "monotonic", return_value=10) as now:
                executor.submit.return_value = failed
                settings.return_value.value.return_value = "missing-ffmpeg.exe"
                intro_service.request_instrumental_analysis(track, source)
                self.assertEqual(intro_service.instrumental_spans(track, source), ())
                self.assertEqual(executor.submit.call_count, 1)
                settings.return_value.value.return_value = "installed-ffmpeg.exe"
                intro_service.request_instrumental_analysis(track, source)
                self.assertEqual(executor.submit.call_count, 2)
                now.return_value = 41
                intro_service.request_instrumental_analysis(track, source)
                self.assertEqual(executor.submit.call_count, 3)


if __name__ == "__main__":
    unittest.main()
