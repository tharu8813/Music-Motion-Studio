"""Real Qt gestures, isolated drafts, sample content, and modal project history."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QColor, QImage, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QPushButton

from app.animation.custom_motion import CustomMotion
from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.dialogs.custom_motion_dialog import CustomMotionDialog
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.utils.i18n import Translator
from tests.main_window_base import MainWindowTestCase
from tests import test_keyframe_workspace as workspace


class MotionDialogTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from app.ui.design_system import apply_studio_style
        apply_studio_style(cls.app)

    def setUp(self):
        from app.dialogs.motion_editor_guide import MOTION_GUIDE_KEY
        QSettings().setValue(MOTION_GUIDE_KEY, True)
        self.scene = CanvasScene()
        self.scene.set_artboard_size(640, 360)
        self.scene.artboard_color = self.scene.artboard_border_color = QColor("black")
        self.source = Source(SourceType.SHAPE, "Motion", x=160, y=120, width=100, height=60,
                             fill_color="#FFFFFF", opacity=.5)
        self.item = SourceItem(self.source)
        self.scene.addItem(self.item)
        self.tracks = [PlaylistTrack("same.wav", "First", artist="Artist A", duration_seconds=10),
                       PlaylistTrack("same.wav", "Second", artist="Artist B", duration_seconds=20)]

    def dialog(self, edge="in", **kwargs):
        translator = Translator()
        if os.environ.get("MMS_CUSTOM_MOTION_LANGUAGE"):
            translator.set_language(os.environ["MMS_CUSTOM_MOTION_LANGUAGE"])
        dialog = CustomMotionDialog(self.source, self.scene, self.tracks, translator, edge, **kwargs)
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(lambda: dialog.reject() if not dialog._closed else None)
        dialog.show()
        self.app.processEvents()
        dialog.seek(.5)
        dialog.render_now()
        return dialog

    def test_canvas_move_creates_relative_pose_and_local_undo_without_project_writes(self):
        saved = deepcopy(self.source.to_dict())
        position = self.item.pos()
        dialog = self.dialog()
        view, item = dialog.canvas, dialog.canvas.handles
        center = item.mapToScene(item.content_rect().center())
        start = view.mapFromScene(center)
        end = view.mapFromScene(center + QPointF(50, 20))
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(view.viewport(), end)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
        self.assertAlmostEqual(dialog.motion.at(.5).dx, 50, delta=2)
        self.assertAlmostEqual(dialog.motion.at(.5).dy, 20, delta=2)
        self.assertEqual(len(dialog.motion.keys("dx")), 3)
        self.assertEqual(len(dialog.motion.keys("dy")), 3)
        self.assertEqual(len(dialog.motion.keys("opacity")), 2)
        self.assertEqual(dialog.undo_stack.count(), 1)
        dialog.undo_stack.undo()
        self.assertEqual(len(dialog.motion.keys("dx")), 2)
        dialog.undo_stack.redo()
        self.assertEqual(len(dialog.motion.keys("dx")), 3)
        dialog.reject()
        self.assertEqual(self.source.to_dict(), saved)
        self.assertEqual(self.item.pos(), position)

    def test_simple_default_and_mode_switch_preserve_independent_keys_bezier_and_history(self):
        motion = CustomMotion.default().with_key("dx", .3, value=40)
        motion = motion.with_key("rotation", .7, value=30, interpolation="bezier", bezier=(.2, -.4, .8, 1.3), custom=True)
        self.source.custom_intro_keyframes = motion
        self.source.animation_in = "custom"
        dialog = self.dialog()
        saved = dialog.state()
        self.assertFalse(dialog.advanced)
        self.assertTrue(dialog.simple_host.isVisible())
        self.assertFalse(dialog.lane_scroll.isVisible())
        self.assertEqual([k.time for k in dialog.simple_timeline.points], [0, .3, .7, 1])
        for _ in range(3):
            dialog.mode_combo.setCurrentIndex(1)
            self.assertTrue(dialog.lane_scroll.isVisible())
            dialog.mode_combo.setCurrentIndex(0)
        self.assertEqual(dialog.state(), saved)
        self.assertEqual(dialog.undo_stack.count(), 0)

        dialog.seek(.4)
        dialog.fields["scale"].setValue(150)
        edited = dialog.motion
        self.assertEqual(edited.keys("rotation"), motion.keys("rotation"))
        dialog.set_advanced(True)
        dialog.undo_stack.undo()
        self.assertEqual(dialog.motion, motion)
        dialog.undo_stack.redo()
        self.assertEqual(dialog.motion, edited)
        dialog.apply_preset("dx", "linear")
        advanced_edit = dialog.motion
        dialog.set_advanced(False)
        dialog.undo_stack.undo()
        self.assertEqual(dialog.motion, edited)
        dialog.undo_stack.redo()
        self.assertEqual(dialog.motion, advanced_edit)

    def test_direct_render_consumes_pending_timer_request(self):
        dialog = self.dialog()
        QTest.qWait(60)
        with patch.object(dialog.canvas, "render_frame", wraps=dialog.canvas.render_frame) as render:
            dialog.seek(.25)
            self.assertTrue(dialog.render_timer.isActive())
            dialog.render_now()
            self.assertFalse(dialog.render_timer.isActive())
            QTest.qWait(40)
            self.assertEqual(render.call_count, 1)

    def test_simple_timeline_pose_add_delete_move_are_atomic_and_preserve_disabled_keys(self):
        dialog = self.dialog()
        dialog.toggle_field("rotation", False)
        baseline = dialog.motion
        dialog.seek(.3)
        QTest.mouseClick(dialog.simple_add, Qt.MouseButton.LeftButton)
        for name in ("dx", "dy", "scale", "opacity"):
            self.assertEqual([k.time for k in dialog.motion.keys(name)], [0, .3, 1])
        self.assertEqual(dialog.motion.keys("rotation"), baseline.keys("rotation"))
        added = dialog.motion
        dialog.begin_gesture()
        dialog.move_common_point(.3, .4)
        dialog.move_common_point(.4, .5)
        dialog.end_gesture()
        moved = dialog.motion
        self.assertEqual(moved.key_times(), (0, .5, 1))
        dialog.undo_stack.undo()
        self.assertEqual(dialog.motion, added)
        dialog.undo_stack.redo()
        self.assertEqual(dialog.motion, moved)
        dialog.delete_common_point(.5)
        self.assertEqual(dialog.motion.key_times(), (0, 1))
        dialog.undo_stack.undo()
        self.assertEqual(dialog.motion, moved)
        dialog.move_common_point(.5, 1)
        self.assertEqual(dialog.motion, moved)

    def test_simple_timeline_mouse_drag_moves_existing_properties_in_one_undo(self):
        dialog = self.dialog()
        dialog.seek(.3)
        dialog.edit_pose(dx=40, scale=1.3)
        before = dialog.motion
        timeline = dialog.simple_timeline
        start = QPoint(round(timeline.point_x(.3)), 19)
        end = QPoint(round(timeline.point_x(.45)), 19)
        QTest.mousePress(timeline, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(timeline, end)
        QTest.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=end)
        self.assertAlmostEqual(dialog.progress, .45, delta=.005)
        self.assertEqual(dialog.motion.keys("dx")[1].time, dialog.motion.keys("scale")[1].time)
        self.assertEqual(dialog.motion.keys("opacity"), before.keys("opacity"))
        dialog.undo_stack.undo()
        self.assertEqual(dialog.motion, before)

    def test_simple_canvas_horizontal_edit_does_not_key_unchanged_vertical_curve(self):
        dialog = self.dialog()
        before = dialog.motion
        dialog.edit_canvas_pose(dict(dx=30, dy=0))
        self.assertEqual([k.time for k in dialog.motion.keys("dx")], [0, .5, 1])
        for name in ("dy", "scale", "rotation", "opacity"):
            self.assertEqual(dialog.motion.keys(name), before.keys(name))

    def test_hidden_items_retained_for_project_undo_do_not_reappear_in_the_clone(self):
        hidden = SourceItem(Source(SourceType.SHAPE, "Deleted", x=0, y=0, width=640, height=360, fill_color="#FF0000", z_index=100))
        self.scene.addItem(hidden)
        hidden.hide()
        dialog = self.dialog()
        self.assertNotIn(hidden.source.id, [item.source.id for item in dialog.canvas.project_scene.items()])
        self.assertEqual(dialog.render_now(full_size=True).pixelColor(5, 5), QColor("black"))

    def test_apply_without_link_preserves_an_intentional_pose_difference(self):
        self.source.animation_in = self.source.animation_out = "custom"
        self.source.custom_intro_keyframes = CustomMotion.default().with_key("dx", 1, value=80)
        self.source.custom_outro_keyframes = CustomMotion.default(False)
        dialog = self.dialog("out")
        saved = dialog.motion
        self.assertTrue(dialog.connection_host.isVisible())
        dialog.apply_changes()
        self.assertEqual(dialog.changes["custom_outro_keyframes"], saved)
        from dataclasses import replace
        restored = Source.from_dict(replace(self.source, **dialog.changes).to_dict())
        self.assertEqual(restored.custom_outro_keyframes, saved)
        self.assertEqual(restored.custom_intro_keyframes.at(1).dx, 80)

    def test_project_plan_clocks_match_rendered_pose_for_crossfade_and_automix(self):
        from app.animation.custom_motion_render import CustomMotionRenderBinding
        from app.automix.planner import compile_automix
        from app.timeline.crossfade import compile_crossfade
        from app.preview.canvas_snapshot import CanvasSnapshot
        from tests.test_automix_manual_transitions import _tracks, _analyses
        from tests.test_automix_planner import ENABLED
        self.tracks = _tracks()
        plans = [compile_crossfade(self.tracks, 2), compile_automix(self.tracks, _analyses(self.tracks, (120, 124, 120)), ENABLED)]
        self.source.animation_in = self.source.animation_out = "custom"
        self.source.custom_intro_keyframes = CustomMotion.default().with_key("dx", 1, value=30)
        self.source.custom_outro_keyframes = CustomMotion.default(False).with_start_pose(self.source.custom_intro_keyframes.at(1))
        self.source.animation_fit_mix = True
        original = deepcopy(self.source.to_dict())
        for plan in plans:
            dialog = self.dialog(compiled_plan=plan)
            dialog.track.setCurrentIndex(1)
            for edge in (0, 1):
                dialog.edge_combo.setCurrentIndex(edge)
                for progress in (0, .25, .5, .99):
                    dialog.seek(progress)
                    local, seconds, _ = dialog.preview_clock(self.tracks[1], 1)
                    binding = CustomMotionRenderBinding.build(dialog.draft_source(), plan, sources=[dialog.draft_source()])
                    for name in dialog.fields:
                        self.assertAlmostEqual(getattr(binding.pose(seconds), name), getattr(dialog.motion.at(progress), name), places=7)
                    expected = CanvasSnapshot.capture_track(self.scene, self.tracks[1], 2, len(self.tracks), 0,
                        elapsed_seconds=local, timeline_seconds=seconds, compiled_plan=plan)
                    actual = dialog.render_now(full_size=True)
                    import numpy as np
                    def pixels(image):
                        image = image.convertToFormat(QImage.Format.Format_RGBA8888)
                        return np.frombuffer(image.constBits(), dtype=np.uint8).copy().astype(int)
                    error = np.abs(pixels(actual)-pixels(expected))
                    # Timeline arithmetic can land on opposite sides of an
                    # 8-bit alpha rounding boundary; geometry must still match.
                    self.assertLessEqual(int(error.max()), 1, (plan.audio.mode if hasattr(plan.audio, "mode") else "plan", edge, progress))
                    self.assertLess(float(error.mean()), .05)
            dialog.reject()
        self.assertEqual(self.source.to_dict(), original)

    def test_reactive_text_handles_follow_the_composited_pose(self):
        from app.animation.curves import music_reactive_pose
        self.scene.clear()
        self.source = Source(SourceType.TEXT, "React", x=160, y=120, width=100, height=60, text="Beat",
            music_reactive_enabled=True, music_reactive_effect="bass_scale", music_reactive_strength=1)
        self.item = SourceItem(self.source)
        self.scene.addItem(self.item)
        dialog = self.dialog()
        pose = dialog.motion.at(dialog.progress)
        reaction = music_reactive_pose(self.source.music_reactive_effect, .5, self.source.music_reactive_strength, self.source.font_size)
        self.assertAlmostEqual(dialog.canvas.handles.scale(), self.source.scale*pose.scale*reaction.scale)
        self.assertGreater(dialog.canvas.handles.scale(), 1)
        self.assertAlmostEqual(dialog.canvas.handles.x(), self.source.x+pose.dx+reaction.dx)

    def test_link_is_explicit_undoable_preserves_other_keys_and_cancel_preserves_project(self):
        intro = CustomMotion.default().with_key("dx", 1, value=100).with_key("scale", 1, value=1.2)
        outro = CustomMotion.default(False).with_key("dx", .6, value=40, interpolation="bezier", bezier=(.1, -.2, .8, 1.3), custom=True)
        self.source.animation_in = self.source.animation_out = "custom"
        self.source.custom_intro_keyframes, self.source.custom_outro_keyframes = intro, outro
        original = deepcopy(self.source.to_dict())
        dialog = self.dialog("out")
        self.assertTrue(dialog.connection_host.isVisible())
        self.assertEqual(dialog.motion, outro)
        dialog.seek(-.01)
        before_frame = dialog.render_now(full_size=True)
        dialog.seek(0)
        self.assertNotEqual(before_frame, dialog.render_now(full_size=True))
        QTest.mouseClick(dialog.connect_pose, Qt.MouseButton.LeftButton)
        self.assertEqual(dialog.motion.at(0), intro.at(1))
        self.assertEqual(dialog.motion.keys("dx")[1:], outro.keys("dx")[1:])
        self.assertFalse(dialog.connection_host.isVisible())
        self.assertEqual(before_frame, dialog.render_now(full_size=True))
        dialog.undo_stack.undo()
        self.assertEqual(dialog.motion, outro)
        self.assertTrue(dialog.connection_host.isVisible())
        dialog.undo_stack.redo()
        dialog.reject()
        self.assertEqual(self.source.to_dict(), original)

    def test_editing_opposite_edge_preview_does_not_activate_it_or_rewrite_legacy_banks(self):
        from app.animation.keyframes import Keyframe
        self.source.keyframes.enabled = False
        self.source.keyframes.global_curves = {"x": [Keyframe(0, 100), Keyframe(5, 200)]}
        original = deepcopy(self.source.to_dict())
        dialog = self.dialog()
        self.assertTrue(dialog.legacy_info.isVisible())
        dialog.edge_combo.setCurrentIndex(1)
        dialog.render_now()
        dialog.apply_changes()
        self.assertNotIn("animation_out", dialog.changes)
        self.assertNotIn("keyframes", dialog.changes)
        self.assertEqual(self.source.to_dict(), original)

    def test_cached_real_reaction_and_sample_mode_are_distinct_without_analysis(self):
        import numpy as np
        from app.preview.audio_reaction_cache import REACTION_CACHE
        from app.renderer.python_visualizer import PythonVisualizerRenderer
        reactive = Source(SourceType.TEXT, "Reactive", text="Music", x=10, y=10,
            music_reactive_enabled=True, music_reactive_strength=1)
        self.scene.addItem(SourceItem(reactive))
        with TemporaryDirectory() as raw:
            path = Path(raw)/"track.wav"
            path.write_bytes(b"cached audio fingerprint")
            self.tracks[0].file_path = str(path)
            data = np.linspace(0, 1, 301, dtype=np.float32)
            REACTION_CACHE.store(self.tracks[0], 30, {"bass": data, "music_envelopes": {reactive.music_reaction_profile: data}})
            with patch.object(PythonVisualizerRenderer, "_decode_mono_audio", side_effect=AssertionError("must reuse cache")):
                dialog = self.dialog()
                _, levels, notice = dialog.reaction_preview(self.tracks[0], 5)
                self.assertAlmostEqual(levels[reactive.id], .5)
                self.assertIn("실제" if dialog.korean else "real", notice)
                _, _, sample = dialog.reaction_preview(self.tracks[1], 5)
                self.assertIn("예시" if dialog.korean else "samples", sample)
                dialog.track.setCurrentIndex(1)
                dialog.render_now()
                self.assertIn("예시" if dialog.korean else "samples", dialog.limitations.text())
                path.write_bytes(b"changed track")
                self.assertIsNone(REACTION_CACHE.get(self.tracks[0]))

    def test_reaction_cache_keeps_only_two_completed_tracks(self):
        from app.preview.audio_reaction_cache import AudioReactionCache
        cache = AudioReactionCache()
        with TemporaryDirectory() as raw:
            tracks = []
            for i in range(3):
                path = Path(raw)/f"{i}.wav"
                path.write_bytes(b"fingerprint")
                track = PlaylistTrack(str(path), str(i))
                tracks.append(track)
                cache.store(track, 30, {"bass": [i], "music_envelopes": {}})
            self.assertIsNone(cache.get(tracks[0]))
            self.assertEqual(cache.get(tracks[2]), (30, [2], {}))
            self.assertEqual(len(cache.entries), 2)

    def test_closing_during_duration_probe_does_not_block_or_delete_a_running_thread(self):
        from threading import Event
        from time import perf_counter
        from app.dialogs.motion_preview_media import MotionPreviewMedia
        from app.services.playlist_service import PlaylistService
        entered, finish = Event(), Event()
        def probe(_path):
            entered.set()
            finish.wait(2)
            return 2
        media = MotionPreviewMedia([], {}, None)
        self.addCleanup(media.deleteLater)
        with patch.object(PlaylistService, "_probe_duration", side_effect=probe):
            media.queue.append("probe.mp4")
            media.start_probe()
            worker = media.worker
            try:
                self.assertTrue(entered.wait(1))
                begin = perf_counter()
                media.close()
                self.assertLess(perf_counter()-begin, .1)
                self.assertTrue(worker.isInterruptionRequested())
                self.assertIs(worker.parent(), self.app)
                self.assertIsNone(media.worker)
            finally:
                finish.set()
                self.assertTrue(worker.wait(1000))
                self.app.processEvents()

    def test_real_video_scrub_play_loop_speed_exit_and_close_use_owned_decoder(self):
        import subprocess
        from contextlib import ExitStack
        from tests.test_keyframe_rendering import FFMPEG
        from app.utils.subprocess_utils import hidden_process_kwargs
        if not FFMPEG:
            self.skipTest("FFmpeg is unavailable")
        with TemporaryDirectory() as raw, ExitStack() as cleanup:
            cleanup.callback(self.app.processEvents)
            path = Path(raw)/"motion-video.mp4"
            result = subprocess.run([str(FFMPEG), "-v", "error", "-f", "lavfi", "-i",
                "testsrc2=size=96x64:rate=20:duration=2", "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-y", str(path)], capture_output=True, timeout=20, **hidden_process_kwargs())
            self.assertEqual(result.returncode, 0, result.stderr)
            video = Source(SourceType.VIDEO, "Video", x=0, y=0, width=96, height=64,
                video_paths=[str(path)], video_repeat_mode="loop_one", video_speed=2)
            original_item = SourceItem(video)
            self.scene.addItem(original_item)
            self.addCleanup(original_item.release_video_decoder)
            cleanup.callback(original_item.release_video_decoder)
            saved = deepcopy(video.to_dict())
            dialog = self.dialog()
            cleanup.callback(lambda: dialog.reject() if not dialog._closed else None)
            dialog.duration.setValue(.6)
            clone = dialog.canvas.media.items[0]
            def wait_for(predicate):
                for _ in range(200):
                    if predicate():
                        return
                    QTest.qWait(20)
                player = clone._video_player
                self.fail(
                    "Video preview did not deliver the requested frame; "
                    f"media_state={player.mediaStatus() if player else None}, "
                    f"playback_state={player.playbackState() if player else None}, "
                    f"media_error={player.errorString() if player else None}, "
                    f"player_position={player.position() if player else None}, "
                    f"pending_seek={clone._video_pending_seek_ms}, "
                    f"requested={clone._video_last_requested_target_ms}, "
                    f"frame_handle={clone._video_frame_handle}, "
                    f"accepted={clone._video_accepted_frames}, "
                    f"dropped={clone._video_dropped_frames}, "
                    f"timestamp_missing={clone._video_timestamp_missing}, "
                    f"filter_busy={clone._video_filter_busy}, "
                    f"state={clone.video_frame_state}, "
                    f"durations={dialog.canvas.media.durations}, "
                    f"media_states={dialog.canvas.media.states}, "
                    f"probe_queue={dialog.canvas.media.queue}, "
                    f"probe_worker={getattr(dialog.canvas.media.worker, 'path', None)}, "
                    f"video_sync={dialog.canvas.video_sync}, "
                    f"source_type={clone.source.source_type}, "
                    f"source_paths={clone.source.video_paths}, "
                    f"track_paths={dialog.tracks[0].video_paths}, "
                    f"timing_mode={clone.source.video_timing_mode}, "
                    f"source_visible={clone.source.visible}, "
                    f"item_visible={clone.isVisible()}, "
                    f"tracked_items={len(dialog.canvas.media.items)}"
                )
            wait_for(lambda: str(path) in dialog.canvas.media.durations and not clone.video_preview_frame().isNull())
            dialog.seek(0)
            dialog.render_now()
            wait_for(lambda: clone._video_player.position() < 180 and clone.video_frame_state == "synced"
                     and not clone.video_preview_frame().isNull())
            first = clone.video_preview_frame().copy()
            revision = clone.video_preview_revision
            dialog.seek(.75)
            dialog.render_now()
            wait_for(lambda: clone.video_preview_revision > revision and abs(clone._video_player.position()-900) < 180
                     and clone.video_frame_state == "synced" and not clone.video_preview_frame().isNull())
            self.assertNotEqual(first, clone.video_preview_frame())
            # Default 0.6s effect at 0.75 => .45s * speed 2 => .9s.
            self.assertAlmostEqual(dialog.canvas.media.last_requests[video.id][1], .9, places=5)
            dialog.duration.setValue(2)
            dialog.seek(.75)
            dialog.render_now()
            self.assertAlmostEqual(dialog.canvas.media.last_requests[video.id][1], 1, places=5)  # 3s wraps in a 2s clip
            dialog.edge_combo.setCurrentIndex(1)
            dialog.seek(0)
            dialog.render_now()
            expected = ((self.tracks[0].duration_seconds-dialog.durations["out"])*2) % 2
            self.assertAlmostEqual(dialog.canvas.media.last_requests[video.id][1], expected, places=5)
            dialog.seek(.2)
            dialog.toggle_play()
            QTest.qWait(180)
            self.assertTrue(clone._video_should_play)
            dialog.toggle_play()
            self.assertFalse(clone._video_should_play)
            dialog.video_sync.setChecked(False)
            request = dialog.canvas.media.last_requests[video.id]
            dialog.seek(.8)
            dialog.render_now()
            self.assertEqual(dialog.canvas.media.last_requests[video.id], request)
            dialog.reject()
            self.assertTrue(dialog.canvas.media.closed)
            self.assertIsNone(clone._video_player)
            self.assertIsNone(clone._video_sink)
            self.assertEqual(video.to_dict(), saved)

    def test_canvas_resize_and_rotation_handles_sync_numeric_pose(self):
        dialog = self.dialog()
        view, item = dialog.canvas, dialog.canvas.handles
        def drag(scene_start, scene_end):
            QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=view.mapFromScene(scene_start))
            QTest.mouseMove(view.viewport(), view.mapFromScene(scene_end))
            QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=view.mapFromScene(scene_end))
            dialog.render_now()
        center = item.mapToScene(item.content_rect().center())
        corner = item.mapToScene(item.content_rect().bottomRight())
        drag(corner, center + (corner - center) * 1.5)
        self.assertAlmostEqual(dialog.fields["scale"].value(), 150, delta=4)
        center = item.mapToScene(item.content_rect().center())
        handle = item.mapToScene(item.rotation_handle_rect().center())
        delta = handle - center
        drag(handle, center + QPointF(-delta.y(), delta.x()))
        self.assertAlmostEqual(dialog.fields["rotation"].value(), 90, delta=3)

    def test_example_track_and_duration_changes_keep_shared_normalized_motion(self):
        dialog = self.dialog()
        dialog.fields["dx"].setValue(100)
        saved = dialog.motion
        dialog.track.setCurrentIndex(1)
        dialog.duration.setValue(1.2)
        dialog.render_now()
        self.assertEqual(dialog.motion, saved)
        self.assertEqual(dialog.motion.keys("dx")[1].time * dialog.durations["in"], .6)
        self.assertEqual(self.source.custom_intro_keyframes, None)
        dialog.edge_combo.setCurrentIndex(1)
        self.assertEqual(dialog.motion, CustomMotion.default(False))
        dialog.fields["rotation"].setValue(30)
        dialog.edge_combo.setCurrentIndex(0)
        self.assertEqual(dialog.motion, saved)

    def test_play_loop_edit_pause_and_close_release_owned_resources(self):
        dialog = self.dialog()
        dialog.duration.setValue(.1)
        dialog.toggle_play()
        QTest.qWait(240)
        self.assertTrue(dialog.timer.isActive())
        self.assertLess(dialog.progress, dialog.play_maximum)
        self.assertGreaterEqual(dialog.progress, dialog.play_minimum)
        dialog.seek(.5)
        dialog.begin_gesture()
        time = dialog.progress
        QTest.qWait(50)
        self.assertEqual(dialog.progress, time)
        dialog.edit_pose(dx=40)
        dialog.end_gesture()
        self.assertTrue(dialog.timer.isActive())
        dialog.reject()
        self.assertFalse(dialog.timer.isActive())
        self.assertFalse(dialog.render_timer.isActive())
        self.assertEqual(self.source.custom_intro_keyframes, None)

    def test_project_content_album_cover_and_now_playing_change_without_changing_draft(self):
        with TemporaryDirectory() as raw:
            paths = []
            for index, color in enumerate(("#FF0000", "#0000FF")):
                path = Path(raw) / f"cover{index}.png"
                image = QImage(32, 32, QImage.Format.Format_RGB32)
                image.fill(QColor(color))
                self.assertTrue(image.save(str(path)))
                self.tracks[index].cover_path = str(path)
                paths.append(path)
            cover = Source(SourceType.ALBUM_COVER, "Cover", x=10, y=10, width=80, height=80,
                           fill_color="#00000000")
            playing = Source(SourceType.NOW_PLAYING, "Now Playing", x=270, y=100, width=320, height=120,
                             fill_color="#00000000")
            self.scene.addItem(SourceItem(cover))
            self.scene.addItem(SourceItem(playing))
            dialog = self.dialog()
            draft = dialog.motion
            a = dialog.render_now(full_size=True)
            dialog.track.setCurrentIndex(1)
            b = dialog.render_now(full_size=True)
            self.assertGreater(a.pixelColor(40, 40).red(), 240)
            self.assertGreater(b.pixelColor(40, 40).blue(), 240)
            self.assertNotEqual(a, b)
            self.assertEqual(dialog.motion, draft)
            if os.environ.get("MMS_CUSTOM_MOTION_SCREENSHOT"):
                dialog.render_now()
                self.app.processEvents()
                self.assertTrue(dialog.grab().save(os.environ["MMS_CUSTOM_MOTION_SCREENSHOT"]))

    def test_legacy_requires_explicit_ack_and_original_compare_is_read_only(self):
        from app.animation.keyframes import Keyframe
        self.source.keyframes.global_curves = {"x": [Keyframe(0, 400)]}
        saved = deepcopy(self.source.to_dict())
        dialog = self.dialog()
        self.assertFalse(dialog.apply_button.isEnabled())
        dialog.seek(0)
        animated = dialog.render_now(full_size=True)
        dialog.original.setChecked(True)
        original = dialog.render_now(full_size=True)
        self.assertNotEqual(animated, original)
        self.assertEqual(self.source.to_dict(), saved)
        dialog.legacy_ack.setChecked(True)
        dialog.apply_changes()
        self.assertFalse(dialog.changes["keyframes"].enabled)
        self.assertEqual(dialog.changes["keyframes"].global_curves, self.source.keyframes.global_curves)
        self.assertEqual(self.source.to_dict(), saved)

    def test_locked_source_cannot_be_edited_or_applied(self):
        self.source.locked = True
        dialog = self.dialog()
        saved = dialog.motion
        dialog.edit_pose(dx=100)
        dialog.change_duration(3)
        dialog.apply_changes()
        self.assertEqual(dialog.motion, saved)
        self.assertFalse(dialog.apply_button.isEnabled())
        self.assertIsNone(dialog.changes)

    def test_timeline_selection_add_move_delete_and_interpolation_have_local_undo(self):
        dialog = self.dialog()
        dialog.set_advanced(True)
        dialog.add_point(.3)
        dialog.add_point(.7)
        before = dialog.motion
        timeline = dialog.timeline
        timeline.setMinimumWidth(600)
        self.app.processEvents()
        from PySide6.QtCore import QPoint
        point = lambda t: QPoint(round(timeline.point_x(t)), 19)
        QTest.mousePress(timeline, Qt.MouseButton.LeftButton, pos=point(.3))
        QTest.mouseMove(timeline, point(.4))
        QTest.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=point(.4))
        self.assertTrue(any(abs(p.time - .4) < .006 for p in dialog.motion.keys("dx")))
        self.assertEqual(len(dialog.motion.keys("dy")), 2)
        dialog.undo_stack.undo()
        self.assertEqual(dialog.motion, before)
        dialog.seek(.7)
        dialog.apply_preset("dx", "hold")
        self.assertEqual(next(p for p in dialog.motion.keys("dx") if p.time == .7).interpolation, "hold")
        dialog.delete_point(.7)
        self.assertEqual(len(dialog.motion.keys("dx")), 3)
        dialog.undo_stack.undo()
        self.assertEqual(len(dialog.motion.keys("dx")), 4)

    def test_bezier_accept_cancel_and_local_undo_update_the_preview(self):
        from app.timeline.bezier_editor import BezierEditor
        dialog = self.dialog()
        dialog.seek(.25)
        before = dialog.motion
        def accepted(editor):
            self.assertIn("Custom" if not dialog.korean else "사용자 지정", dialog.lanes["opacity"].preset.text())
            editor.set_handles((.2, 1.4, .8, -.2))
            return QDialog.DialogCode.Accepted
        with patch.object(BezierEditor, "exec", new=accepted):
            dialog.edit_graph("opacity")
        self.assertEqual(dialog.motion.keys("opacity")[0].bezier, (.2, 1.4, .8, -.2))
        self.assertEqual(dialog.motion.keys("opacity")[0].interpolation, "bezier")
        self.assertEqual(len(dialog.motion.keys("opacity")), 2)
        self.assertEqual(dialog.lanes["opacity"].preset_key, "custom")
        dialog.seek(.25)
        changed = dialog.render_now(full_size=True)
        dialog.undo_stack.undo()
        self.assertEqual(dialog.motion, before)
        self.assertNotEqual(changed, dialog.render_now(full_size=True))
        dialog.seek(0)
        def rejected(editor):
            editor.set_handles((0, 0, 1, 1))
            return QDialog.DialogCode.Rejected
        with patch.object(BezierEditor, "exec", new=rejected):
            dialog.edit_graph("opacity")
        self.assertEqual(dialog.motion, before)

    def test_minimum_window_keeps_canvas_and_apply_button_reachable(self):
        dialog = self.dialog()
        dialog.resize(840, 620)
        self.app.processEvents()
        self.assertGreaterEqual(dialog.canvas.viewport().width(), 350)
        self.assertGreaterEqual(dialog.canvas.viewport().height(), 200)
        self.assertTrue(dialog.rect().contains(dialog.apply_button.mapTo(dialog, dialog.apply_button.rect().bottomRight())))
        self.assertLessEqual(dialog.frameGeometry().height(), dialog.screen().availableGeometry().height())

    def test_property_row_buttons_add_delete_and_navigate_independently(self):
        dialog = self.dialog()
        dialog.set_advanced(True)
        dialog.seek(.3)
        QTest.mouseClick(dialog.lanes["dx"].add, Qt.MouseButton.LeftButton)
        dialog.fields["dx"].setValue(90)
        dialog.seek(.7)
        QTest.mouseClick(dialog.lanes["dy"].add, Qt.MouseButton.LeftButton)
        dialog.fields["dy"].setValue(40)
        self.assertEqual([p.time for p in dialog.motion.keys("dx")], [0, .3, 1])
        self.assertEqual([p.time for p in dialog.motion.keys("dy")], [0, .7, 1])
        self.assertEqual([p.time for p in dialog.motion.keys("opacity")], [0, 1])
        QTest.mouseClick(dialog.lanes["dx"].previous, Qt.MouseButton.LeftButton)
        self.assertEqual((dialog.active_field, dialog.progress), ("dx", .3))
        self.assertEqual(dialog.fields["dx"].value(), 90)
        QTest.mouseClick(dialog.lanes["dx"].delete, Qt.MouseButton.LeftButton)
        self.assertEqual(len(dialog.motion.keys("dx")), 2)
        self.assertEqual(len(dialog.motion.keys("dy")), 3)
        dialog.undo_stack.undo()
        self.assertEqual(len(dialog.motion.keys("dx")), 3)

    def test_preset_popup_has_graph_icons_and_edits_only_the_containing_interval(self):
        from app.dialogs.motion_curve_presets import CurvePresetMenu
        from PySide6.QtCore import QPoint
        dialog = self.dialog()
        dialog.set_advanced(True)
        dialog.add_point(.3, "dx")
        dialog.add_point(.7, "dx")
        timeline = dialog.lanes["dx"].timeline
        QTest.mouseClick(timeline, Qt.MouseButton.LeftButton, pos=QPoint(round(timeline.point_x(.5)), 19))
        self.assertAlmostEqual(dialog.progress, .5, delta=.006)
        self.assertEqual(timeline.segment, (.3, .7))
        dialog.toggle_play()
        QTest.mouseClick(dialog.lanes["dx"].preset, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertFalse(dialog.timer.isActive())
        menu = next(m for m in dialog.findChildren(CurvePresetMenu) if m.isVisible())
        self.assertEqual(len(menu.buttons), 7)
        self.assertTrue(all(not b.icon().isNull() for b in menu.buttons.values()))
        self.assertTrue(all(b.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextUnderIcon for b in menu.buttons.values()))
        if os.environ.get("MMS_CURVE_PRESET_SCREENSHOT"):
            self.assertTrue(menu.grab().save(os.environ["MMS_CURVE_PRESET_SCREENSHOT"]))
        other = dialog.motion.keys("dy")
        QTest.mouseClick(menu.buttons["linear"], Qt.MouseButton.LeftButton)
        self.assertEqual(dialog.motion.keys("dx")[1].interpolation, "linear")
        self.assertEqual(dialog.motion.keys("dx")[0].interpolation, "ease_in_out")
        self.assertEqual(dialog.motion.keys("dy"), other)
        self.assertEqual(len(dialog.motion.keys("dx")), 4)
        self.assertEqual(dialog.lanes["dx"].preset_key, "linear")
        self.assertTrue(dialog.timer.isActive())

    def test_context_is_after_entrance_and_before_exit_and_is_read_only(self):
        for edge in ("in", "out"):
            dialog = self.dialog(edge)
            saved = dialog.motion
            duration = dialog.durations[edge]
            allowed = 1.3 if edge == "in" else -.3
            for progress in (allowed,):
                dialog.seek(progress)
                image = dialog.render_now(full_size=True)
                dialog.render_now()
                boundary_alpha = saved.at(0 if progress < 0 else 1).opacity
                self.assertAlmostEqual(image.pixelColor(190, 140).red(), boundary_alpha*.5*255, delta=2)
                self.assertEqual(dialog.progress, progress)
                self.assertFalse(dialog.can_edit)
                self.assertFalse(dialog.canvas.handles.editable)
                self.assertTrue(all(not f.isEnabled() for f in dialog.fields.values()))
                self.assertTrue(all(not lane.add.isEnabled() and not lane.delete.isEnabled()
                    and not lane.graph.isEnabled() and not lane.preset.isEnabled() for lane in dialog.lanes.values()))
                dialog.edit_pose(dx=999)
                dialog.add_point(.6, "dx")
                dialog.move_point(.5, .6, "dx")
                dialog.apply_preset("dx", "linear")
                dialog.change_duration(3)
                self.assertEqual(dialog.motion, saved)
                self.assertEqual(dialog.durations[edge], duration)
                self.assertEqual(dialog.undo_stack.count(), 0)
            dialog.navigate(-1 if edge == "in" else 1, "dx")
            self.assertEqual(dialog.progress, 1 if edge == "in" else 0)
            self.assertTrue(dialog.can_edit)
            self.assertTrue(dialog.fields["dx"].isEnabled())
            dialog.seek(-1 if edge == "in" else 2)
            self.assertEqual(dialog.progress, 0 if edge == "in" else 1)
            self.assertTrue(dialog.can_edit)
            self.assertEqual(dialog.play_minimum, 0 if edge == "in" else -.5)
            self.assertEqual(dialog.play_maximum, 1.5 if edge == "in" else 1)
            self.assertIn(f"{duration/2:.3f}", dialog.context.text())
            dialog.reject()

    def test_track_enable_toggle_preserves_keys_and_selected_row_is_emphasized(self):
        dialog = self.dialog()
        dialog.set_advanced(True)
        dialog.edit_pose(dx=80, dy=30)
        keys = dialog.motion.keys("dx")
        y_keys = dialog.motion.keys("dy")
        lane = dialog.lanes["dx"]
        QTest.mouseClick(lane.toggle, Qt.MouseButton.LeftButton)
        self.assertFalse(dialog.motion.field_enabled("dx"))
        self.assertEqual(dialog.motion.keys("dx"), keys)
        self.assertEqual(dialog.motion.at(.5).dx, 0)
        self.assertEqual(dialog.motion.keys("dy"), y_keys)
        self.assertFalse(dialog.fields["dx"].isEnabled())
        self.assertFalse(lane.add.isEnabled())
        self.assertFalse(lane.graph.isEnabled())
        dialog.edit_pose(dx=999)
        self.assertEqual(dialog.motion.keys("dx"), keys)
        QTest.mouseClick(dialog.lanes["dy"].title, Qt.MouseButton.LeftButton)
        self.assertEqual(dialog.active_field, "dy")
        self.assertTrue(dialog.lanes["dy"].left.property("active"))
        self.assertTrue(dialog.lanes["dy"].timeline.active)
        self.assertFalse(lane.timeline.active)
        QTest.mouseClick(lane.toggle, Qt.MouseButton.LeftButton)
        self.assertTrue(dialog.motion.field_enabled("dx"))
        self.assertEqual(dialog.motion.at(.5).dx, 80)
        dialog.undo_stack.undo()
        self.assertFalse(dialog.motion.field_enabled("dx"))
        self.assertEqual(dialog.motion.keys("dx"), keys)

    def test_playback_runs_through_context_and_stops_or_loops_at_context_end(self):
        dialog = self.dialog()
        dialog.duration.setValue(.1)
        dialog.loop.setChecked(False)
        dialog.seek(dialog.play_minimum)
        dialog.toggle_play()
        QTest.qWait(280)
        self.assertFalse(dialog.timer.isActive())
        self.assertEqual(dialog.progress, dialog.play_maximum)
        self.assertFalse(dialog.can_edit)
        dialog.loop.setChecked(True)
        dialog.toggle_play()
        QTest.qWait(450)
        self.assertTrue(dialog.timer.isActive())
        self.assertGreaterEqual(dialog.progress, dialog.play_minimum)
        self.assertLess(dialog.progress, dialog.play_maximum)

    def test_play_after_fractional_context_end_restarts_at_the_allowed_start(self):
        for edge in ("in", "out"):
            dialog = self.dialog(edge)
            dialog.loop.setChecked(False)
            dialog.seek(dialog.play_maximum)
            self.assertEqual(dialog.progress, dialog.play_maximum)
            dialog.toggle_play()
            self.assertEqual(dialog.progress, dialog.play_minimum)
            self.assertTrue(dialog.timer.isActive())
            dialog.reject()

    def test_short_source_exposure_does_not_hide_the_effect_being_authored(self):
        self.source.timeline_start = 3
        self.source.timeline_duration = .1
        self.source.animation_out_duration = 2
        dialog = self.dialog("out")
        dialog.seek(0)
        image = dialog.render_now(full_size=True)
        self.assertAlmostEqual(image.pixelColor(190, 140).red(), 127, delta=2)
        self.assertEqual(self.source.timeline_start, 3)
        self.assertEqual(self.source.timeline_duration, .1)

    def test_wheel_zoom_keeps_cursor_anchor_and_does_not_edit_motion(self):
        dialog = self.dialog()
        view = dialog.canvas
        anchor = view.viewport().rect().center()+QPoint(60, 20)
        before = view.mapToScene(anchor)
        saved = dialog.motion
        scale = view.transform().m11()
        wheel = QWheelEvent(QPointF(anchor), QPointF(view.viewport().mapToGlobal(anchor)), QPoint(), QPoint(0, 120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        QCoreApplication.sendEvent(view.viewport(), wheel)
        self.assertGreater(view.transform().m11(), scale)
        self.assertLess((view.mapToScene(anchor)-before).manhattanLength(), 6)
        self.assertEqual(dialog.motion, saved)
        self.assertEqual(dialog.undo_stack.count(), 0)
        view.zoom(100000)
        self.assertAlmostEqual(view.transform().m11(), 8)
        view.zoom(.0000001)
        self.assertAlmostEqual(view.transform().m11(), .05)
        view.fit_canvas()
        self.assertTrue(view.fit_mode)

    def test_blank_and_middle_drag_pan_without_changing_source_or_guides(self):
        dialog = self.dialog()
        view = dialog.canvas
        saved = dialog.motion
        board = view.project_scene.artboard_rect
        for button in (Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton):
            view.fit_canvas()
            before = view.mapToScene(view.viewport().rect().center())
            start = QPoint(20, 20)
            QTest.mousePress(view.viewport(), button, pos=start)
            QTest.mouseMove(view.viewport(), start+QPoint(50, 25))
            QTest.mouseRelease(view.viewport(), button, pos=start+QPoint(50, 25))
            after = view.mapToScene(view.viewport().rect().center())
            self.assertGreater((after-before).manhattanLength(), 40)
            self.assertFalse(view.fit_mode)
            self.assertFalse(view._panning)
            dialog.render_now()
            self.assertLess((view.mapToScene(view.viewport().rect().center())-after).manhattanLength(), 3)
            self.assertEqual(view.project_scene.artboard_rect, board)
        self.assertEqual(dialog.motion, saved)
        self.assertEqual(dialog.undo_stack.count(), 0)

    def test_space_tap_plays_but_space_drag_over_source_only_pans(self):
        dialog = self.dialog()
        view = dialog.canvas
        view.setFocus()
        QTest.keyClick(view, Qt.Key.Key_Space)
        self.assertTrue(dialog.timer.isActive())
        QTest.keyClick(view, Qt.Key.Key_Space)
        self.assertFalse(dialog.timer.isActive())
        saved = dialog.motion
        start = view.mapFromScene(view.handles.mapToScene(view.handles.content_rect().center()))
        QTest.keyPress(view, Qt.Key.Key_Space)
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(view.viewport(), start+QPoint(35, 15))
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=start+QPoint(35, 15))
        QTest.keyRelease(view, Qt.Key.Key_Space)
        self.assertFalse(dialog.timer.isActive())
        self.assertEqual(dialog.motion, saved)
        self.assertEqual(dialog.undo_stack.count(), 0)

    def test_escape_cancels_hand_mode_before_cancelling_the_editor(self):
        dialog = self.dialog()
        dialog.canvas.setFocus()
        QTest.keyPress(dialog.canvas, Qt.Key.Key_Space)
        QTest.keyClick(dialog.canvas, Qt.Key.Key_Escape)
        self.assertFalse(dialog._closed)
        self.assertFalse(dialog.canvas._space_pan)
        QTest.keyRelease(dialog.canvas, Qt.Key.Key_Space)
        self.assertFalse(dialog.timer.isActive())
        QTest.keyClick(dialog.canvas, Qt.Key.Key_Escape)
        self.assertTrue(dialog._closed)

    def test_shortcuts_select_add_delete_navigate_and_keep_numeric_input_native(self):
        dialog = self.dialog()
        view = dialog.canvas
        view.setFocus()
        QTest.keyClick(view, Qt.Key.Key_2)
        self.assertEqual(dialog.active_field, "dy")
        QTest.keyClick(view, Qt.Key.Key_K)
        self.assertEqual(len(dialog.motion.keys("dy")), 3)
        QTest.keyClick(view, Qt.Key.Key_Delete)
        self.assertEqual(len(dialog.motion.keys("dy")), 2)
        QTest.keyClick(view, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(len(dialog.motion.keys("dy")), 3)
        QTest.keyClick(view, Qt.Key.Key_Left, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(dialog.progress, 0)
        QTest.keyClick(view, Qt.Key.Key_Right, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(dialog.progress, .5)
        QTest.keyClick(view, Qt.Key.Key_End)
        self.assertEqual(dialog.progress, 1.5)
        QTest.keyClick(view, Qt.Key.Key_K)
        self.assertEqual(len(dialog.motion.keys("dy")), 3)
        QTest.keyClick(view, Qt.Key.Key_Home, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(dialog.progress, 0)
        line = dialog.fields["dx"].lineEdit()
        line.setFocus()
        saved = dialog.motion
        stack_index = dialog.undo_stack.index()
        QTest.keyClick(line, Qt.Key.Key_K)
        QTest.keyClick(line, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(dialog.motion, saved)
        self.assertEqual(dialog.undo_stack.index(), stack_index)
        QTest.keyClick(line, Qt.Key.Key_Space)
        self.assertFalse(dialog.timer.isActive())

    def test_slider_sync_and_large_numeric_values_are_preserved_and_drag_is_one_undo(self):
        dialog = self.dialog()
        slider = dialog.sliders["dx"]
        start = slider.rect().center()
        QTest.mousePress(slider, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(slider, start+QPoint(18, 0))
        QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=start+QPoint(18, 0))
        self.assertNotEqual(dialog.motion.at(.5).dx, 0)
        self.assertEqual(dialog.fields["dx"].value(), slider.value())
        self.assertEqual(dialog.undo_stack.count(), 1)
        dialog.undo_stack.undo()
        self.assertEqual(dialog.motion.at(.5).dx, 0)
        dialog.fields["dx"].setValue(4000)
        self.assertEqual(slider.value(), 4000)
        self.assertEqual(dialog.motion.at(.5).dx, 4000)
        self.assertLess(slider.mapTo(dialog, slider.rect().center()).x(),
            dialog.fields["dx"].mapTo(dialog, dialog.fields["dx"].rect().center()).x())
        dialog.seek(1.2)
        self.assertTrue(all(not s.isEnabled() for s in dialog.sliders.values()))

    def test_first_use_tour_opens_once_and_can_be_reopened(self):
        from app.dialogs.motion_editor_guide import MOTION_GUIDE_KEY
        from app.dialogs.welcome_dialog import GuideDialog
        QSettings().setValue(MOTION_GUIDE_KEY, False)
        with patch.object(GuideDialog, "exec", return_value=QDialog.DialogCode.Accepted) as show:
            dialog = self.dialog()
            self.assertEqual(show.call_count, 1)
            self.dialog("out")
            self.assertEqual(show.call_count, 1)
            QTest.mouseClick(dialog.guide_button, Qt.MouseButton.LeftButton)
            self.assertEqual(show.call_count, 2)

    def test_context_is_always_half_duration_and_help_keys_open_editor_topics(self):
        for edge in ("in", "out"):
            dialog = self.dialog(edge)
            for duration in (.1, .45, 2, 10):
                dialog.seek(.5)
                dialog.change_duration(duration)
                context = (dialog.play_maximum-1 if edge == "in" else -dialog.play_minimum)*duration
                self.assertAlmostEqual(context, duration/2)
            dialog.canvas.setFocus()
            with patch("app.dialogs.help_dialog.open_help") as opened:
                QTest.keyClick(dialog.canvas, Qt.Key.Key_F1)
                opened.assert_called_with(dialog, dialog.translator, "canvas", "custom_motion")
                QTest.keyClick(dialog.canvas, Qt.Key.Key_F1, Qt.KeyboardModifier.ShiftModifier)
                opened.assert_called_with(dialog, dialog.translator, "canvas", "motion_shortcuts")
            dialog.reject()


class MotionWorkspaceTests(MainWindowTestCase):
    def test_new_project_hides_generic_controls_and_shows_custom_entry_buttons(self):
        source, controller = workspace.KeyframeWorkspaceTests.prepare(self)
        self.assertFalse(hasattr(self.window.timeline_panel, "view_combo"))
        self.assertFalse(self.window.inspector.findChildren(QPushButton, "keyframeDiamondButton"))
        inspector = self.window.inspector
        inspector.animation_in_combo.setCurrentIndex(inspector.animation_in_combo.findData("custom"))
        self.assertFalse(inspector.animation_in_edit_button.isHidden())
        self.assertTrue(inspector.animation_out_edit_button.isHidden())

    def test_apply_is_one_project_undo_and_cancel_changes_no_serial(self):
        source, controller = workspace.KeyframeWorkspaceTests.prepare(self)
        saved = deepcopy(source.to_dict())
        def apply(dialog):
            dialog.seek(.5)
            dialog.edit_pose(dx=100)
            dialog.toggle_field("opacity", False)
            dialog.apply_changes()
            return QDialog.DialogCode.Accepted
        with patch.object(CustomMotionDialog, "exec", new=apply):
            self.window.custom_motion_controller.open_editor(source.id, "in")
        self.assertEqual(controller.source.custom_intro_keyframes.at(.5).dx, 100)
        self.assertFalse(controller.source.custom_intro_keyframes.field_enabled("opacity"))
        self.window._undo()
        self.assertEqual(controller.source.to_dict(), saved)
        self.window._redo()
        after = deepcopy(controller.source.to_dict())
        serial = self.window.project_session.change_serial
        def cancel(dialog):
            dialog.edit_pose(dx=-200)
            dialog.reject()
            return QDialog.DialogCode.Rejected
        with patch.object(CustomMotionDialog, "exec", new=cancel):
            self.window.custom_motion_controller.open_editor(source.id, "out")
        self.assertEqual(controller.source.to_dict(), after)
        self.assertEqual(self.window.project_session.change_serial, serial)
