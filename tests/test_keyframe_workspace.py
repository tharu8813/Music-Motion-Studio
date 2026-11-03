"""Saved animation compatibility after retiring the generic keyframe editor."""
from copy import deepcopy
from dataclasses import replace

from tests.main_window_base import MainWindowTestCase
from app.animation.keyframes import Keyframe
from app.models.playlist import PlaylistTrack
from app.models.source import Source, SourceType
from app.utils.i18n import Language


class KeyframeWorkspaceTests(MainWindowTestCase):
    def prepare(self):
        source = Source(SourceType.SHAPE, "Animated", x=100, y=100)
        self.window.set_project_open(True)
        self.window.store.replace([source])
        self.window.store.select(source.id)
        self.window.playlist_service.replace([PlaylistTrack("", "A", duration_seconds=30)])
        self.window.project_session.ready = True
        self.window._history_timer.stop()
        self.window.history.reset(self.window._project_document().to_dict())
        return source, self.window.timeline_panel.keyframe_controller

    def test_toggle_and_value_edits_have_distinct_undo_steps(self):
        source, controller = self.prepare()
        controller.toggle("x")
        controller.edit_value("x", 250)
        self.window._undo()
        self.assertEqual(controller.source.keyframes.global_curves["x"][0].value, 100)
        self.window._undo()
        self.assertFalse(controller.source.keyframes.animated)
        self.window._redo()
        self.window._redo()
        self.assertEqual(controller.source.keyframes.global_curves["x"][0].value, 250)

    def test_visual_view_changes_preserve_animation_time_basis_and_data(self):
        source, controller = self.prepare()
        controller.configure(mode="per_track")
        controller.choose_track(self.window.playlist_service.tracks[0].id)
        controller.toggle("scale")
        saved = source.to_dict()
        visual = self.window.timeline_panel.visual
        visual.set_selected("source", source.id)
        visual.focus_selected()
        visual.fit_all()
        self.assertEqual(source.to_dict(), saved)

    def test_saved_banks_never_reintroduce_the_retired_editor(self):
        source, controller = self.prepare()
        track = self.window.playlist_service.tracks[0]
        source.keyframes.global_curves = {"x": [Keyframe(0, 100), Keyframe(10, 300)]}
        source.keyframes.single_track_curves = {track.id: {"rotation": [Keyframe(0, 30)]}}
        source.keyframes.per_track_curves = {track.id: {"scale": [Keyframe(0, 2)]}}
        saved = deepcopy(source.keyframes)
        panel = self.window.timeline_panel
        for mode in ("global", "single_track", "per_track"):
            animation = deepcopy(source.keyframes)
            animation.mode = mode
            animation.target_track_id = track.id
            self.window.store.update(source.id, keyframes=animation)
            for language in (Language.ENGLISH, Language.KOREAN):
                self.window.translator.set_language(language)
                self.application.processEvents()
                self.assertFalse(hasattr(panel, "view_combo"))
                self.assertFalse(hasattr(panel, "track_table"))
                panel.visual.set_selected("source", source.id)
                panel.visual.focus_selected()
                panel.visual.fit_all()
            self.assertEqual(source.keyframes.global_curves, saved.global_curves)
        self.assertEqual(source.keyframes.single_track_curves[track.id]["rotation"], [Keyframe(0, 30)])
        self.assertEqual(source.keyframes.per_track_curves[track.id]["scale"], [Keyframe(0, 2)])

    def test_inspector_edits_base_properties_and_preserves_saved_animation(self):
        source, controller = self.prepare()
        source.keyframes.global_curves = {
            "x": [Keyframe(0, 100, "linear"), Keyframe(10, 300)],
            "shadow.offset_x": [Keyframe(0, 4)],
        }
        self.window.store.update(source.id, keyframes=source.keyframes)
        saved = deepcopy(source.keyframes)
        controller.seek_global(5)
        inspector = self.window.inspector
        self.assertEqual(inspector.x_spin.value(), source.x)
        inspector.x_spin.setValue(250)
        inspector.shadow_x_spin.setValue(12)
        self.assertEqual(source.x, 250)
        self.assertEqual(source.shadow.offset_x, 12)
        self.assertEqual(source.keyframes, saved)
        self.assertEqual(controller.evaluated()["x"], 200)

    def test_rejected_paste_preserves_existing_points(self):
        source, controller = self.prepare()
        controller.toggle("x")
        saved = source.to_dict()
        controller.copy()
        controller.paste()
        self.assertEqual(source.to_dict(), saved)

    def test_crossfade_editor_uses_visual_owner_not_early_incoming_audio(self):
        source, controller = self.prepare()
        tracks = [PlaylistTrack("", "A", duration_seconds=10), PlaylistTrack("", "B", duration_seconds=10)]
        self.window.playlist_service.replace(tracks)
        self.window.project_settings = replace(self.window.project_settings, transition_mode="crossfade", crossfade_seconds=2)
        source.keyframes.mode = "per_track"
        source.keyframes.per_track_curves = {tracks[0].id: {"x": [Keyframe(0, 100)]},
                                             tracks[1].id: {"x": [Keyframe(0, 300)]}}
        controller.seek_global(8.5)
        self.assertEqual(controller.evaluated()["x"], 100)
        controller.seek_global(9.5)
        self.assertEqual(controller.evaluated()["x"], 300)
