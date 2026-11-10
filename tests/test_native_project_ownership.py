"""Production native owner/time resolution at compiled overlap and gap boundaries."""
from unittest import TestCase

from app.renderer.native_project_support import resolve_project_frame, TimelineValidationError
from app.timeline.render_plan import (
    AudioRenderClip, AudioRenderPlan, AudioRenderTransition, CompiledRenderPlan,
    build_presentation_and_metadata,
)
from app.timeline.models import TransitionType
from tests.test_native_canvas_project_probe import project_fixture


class NativeProjectOwnershipTests(TestCase):
    def plan(self, second_start, *, transition=False):
        project = project_fixture()
        for track in project.playlist:
            track.duration_seconds = 30
        clips = (AudioRenderClip("a", project.playlist[0].id, 0, 2, 12, 1),
                 AudioRenderClip("b", project.playlist[1].id, second_start, 4, 24, 2))
        presentation, metadata, duration = build_presentation_and_metadata(clips)
        transitions = (AudioRenderTransition("a", "b", 8, 2, TransitionType.CROSSFADE),) if transition else ()
        return project, CompiledRenderPlan(AudioRenderPlan(clips, transitions), presentation, metadata, duration)

    def test_midpoint_before_exact_after_preserves_source_offset_and_rate(self):
        project, plan = self.plan(8, transition=True)
        for time, owner, elapsed, window_start in (
            (9 - 1e-6, 0, 11 - 1e-6, 0),
            (9, 1, 6, 8),
            (9 + 1e-6, 1, 6 + 2e-6, 8),
        ):
            with self.subTest(time=time):
                state = resolve_project_frame(project, time, plan=plan)
                self.assertIs(state["track"], project.playlist[owner])
                self.assertEqual(state["track_number"], owner + 1)
                self.assertAlmostEqual(state["track_elapsed_seconds"], elapsed)
                self.assertEqual(state["timeline_seconds"], time)
                self.assertEqual(state["window_start_seconds"], window_start)

    def test_gap_has_no_fabricated_track_and_incoming_boundary_uses_source_offset(self):
        project, plan = self.plan(12)
        state = resolve_project_frame(project, 10 - 1e-6, plan=plan)
        self.assertIs(state["track"], project.playlist[0])
        self.assertAlmostEqual(state["track_elapsed_seconds"], 12 - 1e-6)
        for time in (10, 11, 12 - 1e-6):
            with self.subTest(gap=time), self.assertRaisesRegex(TimelineValidationError, "gap"):
                resolve_project_frame(project, time, plan=plan)
        for time, elapsed in ((12, 4), (12 + 1e-6, 4 + 2e-6)):
            state = resolve_project_frame(project, time, plan=plan)
            self.assertIs(state["track"], project.playlist[1])
            self.assertAlmostEqual(state["track_elapsed_seconds"], elapsed)
