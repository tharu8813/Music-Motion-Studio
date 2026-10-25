from __future__ import annotations

import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QGraphicsView

from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.models.source import Source, SourceType


class CanvasSnappingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        modifiers = patch.object(QApplication, "keyboardModifiers", return_value=Qt.NoModifier)
        modifiers.start()
        self.addCleanup(modifiers.stop)
        self.scene = CanvasScene()
        self.view = QGraphicsView(self.scene)
        self.item = self.add_item(400, 200)
        self.scene.begin_item_interaction(self.item)

    def tearDown(self) -> None:
        self.scene.finish_item_interaction()
        self.view.close()
        self.scene.clear()

    def add_item(self, x: float, y: float, **changes: float) -> SourceItem:
        item = SourceItem(Source(
            SourceType.SHAPE, "Shape", x=x, y=y, width=80, height=60, **changes,
        ))
        self.scene.addItem(item)
        return item

    def targets(self, x: list[float], y: list[float] | None = None) -> None:
        self.scene._snap_candidates_x = sorted(x)
        self.scene._snap_candidates_y = sorted(y or [])

    def test_nearest_target_wins_instead_of_first_target(self) -> None:
        self.targets([500, 504])
        result = self.scene.snap_position(self.item, QPointF(503, 203))
        self.assertEqual(result.x(), 504)
        self.assertEqual(self.scene.guide_x, 504)

    def test_grid_rounding_does_not_extend_alignment_capture_range(self) -> None:
        self.targets([500])
        result = self.scene.snap_position(self.item, QPointF(508, 203))
        self.assertEqual(result.x(), 510)
        self.assertIsNone(self.scene.guide_x)

    def test_grid_allows_unsnapped_positions_away_from_grid_lines(self) -> None:
        self.targets([])
        result = self.scene.snap_position(self.item, QPointF(405, 205))
        self.assertEqual(result, QPointF(405, 205))

    def test_screen_tolerance_is_consistent_at_different_zooms(self) -> None:
        self.targets([500])
        for zoom in (0.25, 1.0, 3.0):
            with self.subTest(zoom=zoom):
                self.view.resetTransform()
                self.view.scale(zoom, zoom)
                self.scene.clear_alignment_guides()
                self.assertEqual(
                    self.scene.snap_position(self.item, QPointF(500 + 3 / zoom, 203)).x(),
                    500,
                )
                self.scene.clear_alignment_guides()
                self.scene.snap_position(self.item, QPointF(500 + 5 / zoom, 203))
                self.assertIsNone(self.scene.guide_x)

    def test_dense_grid_does_not_quantize_zoomed_out_drag(self) -> None:
        self.targets([])
        self.view.scale(0.25, 0.25)
        self.assertEqual(
            self.scene.snap_position(self.item, QPointF(401, 201)), QPointF(401, 201),
        )

    def test_target_is_retained_until_pointer_leaves_release_radius(self) -> None:
        self.targets([500, 505])
        for proposed in (501, 503, 506):
            self.assertEqual(
                self.scene.snap_position(self.item, QPointF(proposed, 203)).x(), 500,
            )
        self.assertEqual(
            self.scene.snap_position(self.item, QPointF(508, 203)).x(), 505,
        )

    def test_alt_uses_event_modifiers_and_clears_previous_lock(self) -> None:
        self.targets([500])
        self.scene.snap_position(self.item, QPointF(501, 203))
        with patch.object(QApplication, "keyboardModifiers", return_value=Qt.NoModifier):
            free = self.scene.snap_position(self.item, QPointF(506, 203), Qt.AltModifier)
        self.assertEqual(free, QPointF(506, 203))
        self.assertIsNone(self.scene.guide_x)
        self.scene.snap_position(self.item, free, Qt.NoModifier)
        self.assertIsNone(self.scene.guide_x)

    def test_disabled_snapping_clears_locks(self) -> None:
        self.targets([500])
        self.scene.snap_position(self.item, QPointF(501, 203))
        self.scene.snap_enabled = False
        self.assertEqual(
            self.scene.snap_position(self.item, QPointF(501, 203)), QPointF(501, 203),
        )
        self.assertFalse(self.scene._snap_locks)

    def test_scaled_rotated_content_edges_really_touch_guide(self) -> None:
        for scale, rotation in ((2.0, 0.0), (1.5, 45.0), (0.5, 90.0)):
            with self.subTest(scale=scale, rotation=rotation):
                self.item.setScale(scale)
                self.item.setRotation(rotation)
                self.scene.clear_alignment_guides()
                bounds = self.item.mapRectToScene(self.item.content_rect())
                self.targets([bounds.right() + 20])
                position = self.scene.snap_position(self.item, self.item.pos() + QPointF(18, 0))
                moved = bounds.translated(position - self.item.pos())
                self.assertAlmostEqual(moved.right(), self.scene.guide_x)

    def test_resize_chooses_nearest_target_without_grid_pre_rounding(self) -> None:
        self.targets([500, 504])
        width, height = self.scene.snap_resize(self.item, 103, 63, "e", Qt.NoModifier)
        self.assertEqual((width, height), (104, 63))
        self.assertEqual(self.scene.guide_x, 504)

    def test_resizing_each_edge_keeps_opposite_anchor_and_matches_guide(self) -> None:
        self.item.setScale(1.5)
        for handle in ("e", "w", "n", "s"):
            with self.subTest(handle=handle):
                self.scene.clear_alignment_guides()
                before = self.item.mapRectToScene(self.item.content_rect())
                horizontal = handle in ("e", "w")
                sign = -1 if handle in ("w", "n") else 1
                edge = {"e": before.right(), "w": before.left(),
                        "n": before.top(), "s": before.bottom()}[handle]
                target = edge + sign * 30
                self.targets([target] if horizontal else [], [] if horizontal else [target])
                self.item._resize_handle = handle
                self.item._resize_anchor_scene = self.item.mapToScene(
                    self.item._anchor_point_for_size(handle, self.item.source.width, self.item.source.height)
                )
                width = self.item.source.width + (18 if horizontal else 0)
                height = self.item.source.height + (0 if horizontal else 18)
                width, height = self.scene.snap_resize(self.item, width, height, handle, Qt.NoModifier)
                self.item._resize_to(width, height, handle)
                after = self.item.mapRectToScene(self.item.content_rect())
                actual = {"e": after.right(), "w": after.left(),
                          "n": after.top(), "s": after.bottom()}[handle]
                self.assertAlmostEqual(actual, target)
                self.assertEqual(
                    self.item.mapToScene(self.item._anchor_point_for_size(handle, width, height)),
                    self.item._resize_anchor_scene,
                )

    def test_rotated_resize_stays_free_and_alt_resize_is_exact(self) -> None:
        self.item.setRotation(30)
        self.assertEqual(self.scene.snap_resize(self.item, 83, 67), (83, 67))
        self.item.setRotation(0)
        self.targets([484], [264])
        self.assertEqual(
            self.scene.snap_resize(self.item, 83, 63, "se", Qt.AltModifier), (83, 63),
        )

    def test_alt_resize_passes_event_modifiers_and_preserves_aspect(self) -> None:
        self.targets([484], [264])
        self.item._resizing = True
        self.item._resize_origin_scene = QPointF()
        self.item._resize_size = (80, 60)
        self.item._resize_handle = "se"
        self.item._resize_anchor_scene = self.item.mapToScene(QPointF())
        self.item.mouseMoveEvent(SimpleNamespace(
            scenePos=lambda: QPointF(3, 1), modifiers=lambda: Qt.AltModifier,
            accept=lambda: None,
        ))
        self.assertAlmostEqual(self.item.source.width, 83)
        self.assertAlmostEqual(self.item.source.height, 62.25)
        self.assertIsNone(self.scene.guide_x)

    def test_control_drag_emits_duplicate_at_drop_and_restores_original(self) -> None:
        self.scene.finish_item_interaction()
        other = self.add_item(600, 300)
        other.setSelected(True)
        self.item.setSelected(True)
        self.view.resize(900, 600)
        self.view.show()
        self.view.centerOn(550, 300)
        self.application.processEvents()
        dropped: list[tuple[object, ...]] = []
        self.item.duplicate_requested.connect(lambda *args: dropped.append(args))
        start = self.view.mapFromScene(self.item.mapToScene(QPointF(30, 30)))
        QTest.mousePress(self.view.viewport(), Qt.LeftButton, Qt.ControlModifier, start)
        position = QPointF(start + QPoint(20, 30))
        QApplication.sendEvent(self.view.viewport(), QMouseEvent(
            QEvent.MouseMove, position, position, Qt.NoButton, Qt.LeftButton, Qt.ControlModifier,
        ))
        QTest.mouseRelease(self.view.viewport(), Qt.LeftButton, Qt.ControlModifier, position.toPoint())
        self.assertEqual(self.item.pos(), QPointF(400, 200))
        self.assertEqual(other.pos(), QPointF(600, 300))
        self.assertEqual(dropped, [(self.item.source.id, 420, 230)])

    def test_aspect_minimum_does_not_leave_a_guide_at_the_wrong_edge(self) -> None:
        self.item.source.source_type = SourceType.ALBUM_COVER
        self.item.source.height = 80
        self.item.apply_source()
        self.targets([], [224])
        self.item._resizing = True
        self.item._resize_origin_scene = QPointF()
        self.item._resize_size = (80, 80)
        self.item._resize_handle = "se"
        self.item._resize_anchor_scene = self.item.mapToScene(QPointF())
        self.item.mouseMoveEvent(SimpleNamespace(
            scenePos=lambda: QPointF(-10, -55), modifiers=lambda: Qt.NoModifier,
            accept=lambda: None,
        ))
        self.assertEqual((self.item.source.width, self.item.source.height), (32, 32))
        self.assertIsNone(self.scene.guide_y)

    def test_group_snaps_collective_bounds_and_never_changes_spacing(self) -> None:
        other = self.add_item(603, 303)
        for item in (other, self.item):
            item.setSelected(True)
        self.scene.finish_item_interaction()
        self.scene.begin_item_interaction(self.item, include_selection=True)
        self.targets([700])
        spacing = other.pos() - self.item.pos()
        notifications: list[object] = []
        for item in (self.item, other):
            item.changed_by_user.connect(lambda *args: notifications.append(args))
        for delta in (QPointF(15, 13), QPointF(20, 17), QPointF(18, 19)):
            self.scene.move_interactive_items(delta, Qt.NoModifier)
            self.assertEqual(other.pos() - self.item.pos(), spacing)
            bounds = self.scene.multi_selection_bounds()
            self.assertAlmostEqual(bounds.right(), 700)
        self.assertEqual(notifications, [])
        self.scene.move_interactive_items(QPointF(22.5, 23.5), Qt.AltModifier)
        self.assertEqual(self.item.pos(), QPointF(422.5, 223.5))
        self.assertEqual(other.pos() - self.item.pos(), spacing)
        self.scene.finish_item_interaction()
        self.assertEqual(len(notifications), 2)

    def test_programmatic_position_changes_are_not_snapped(self) -> None:
        self.item.setPos(403.5, 207.5)
        self.assertEqual(self.item.pos(), QPointF(403.5, 207.5))
        self.assertEqual((self.item.source.x, self.item.source.y), (403.5, 207.5))

    def test_candidates_are_unique_sorted_and_exclude_hidden_or_moving_items(self) -> None:
        visible = self.add_item(500, 300)
        self.add_item(500, 300)
        hidden = self.add_item(777, 777)
        hidden.hide()
        x, y = self.scene._build_alignment_candidates({self.item})
        self.assertEqual(x, sorted(set(x)))
        self.assertEqual(y, sorted(set(y)))
        self.assertIn(visible.pos().x(), x)
        self.assertNotIn(777, x)
        self.assertNotIn(400, x)

    def test_drag_reuses_candidates_and_finish_discards_snap_state(self) -> None:
        with patch.object(self.scene, "_build_alignment_candidates", wraps=self.scene._build_alignment_candidates) as build:
            for step in range(30):
                self.scene.move_interactive_items(QPointF(step, step), Qt.NoModifier)
            build.assert_not_called()
        self.scene.finish_item_interaction()
        self.assertFalse(self.scene._snap_locks)
        self.assertFalse(self.scene._interactive_positions)
        self.assertIsNone(self.scene._snap_candidates_x)
        self.assertIsNone(self.scene.guide_x)

    def test_vertical_guide_connects_only_moving_and_reference_bounds(self) -> None:
        reference = self.add_item(500, 350)
        self.scene.begin_item_interaction(self.item)
        self.scene.move_interactive_items(QPointF(98, 0), Qt.NoModifier)
        line, = self.scene._alignment_guide_lines()
        self.assertEqual(line.p1(), QPointF(500, 200))
        self.assertEqual(line.p2(), QPointF(500, reference.source.y + reference.source.height))

    def test_horizontal_guide_connects_only_moving_and_reference_bounds(self) -> None:
        self.add_item(600, 300)
        self.scene.begin_item_interaction(self.item)
        self.scene.move_interactive_items(QPointF(0, 98), Qt.NoModifier)
        line, = self.scene._alignment_guide_lines()
        self.assertEqual(line.p1(), QPointF(400, 300))
        self.assertEqual(line.p2(), QPointF(680, 300))

    def test_artboard_guides_keep_full_span_even_when_a_source_shares_target(self) -> None:
        self.add_item(640, 350)
        for position, start, end in (
            (QPointF(638, 203), QPointF(640, 0), QPointF(640, 720)),
            (QPointF(2, 203), QPointF(0, 0), QPointF(0, 720)),
            (QPointF(403, 358), QPointF(0, 360), QPointF(1280, 360)),
        ):
            with self.subTest(position=position):
                self.scene.begin_item_interaction(self.item)
                self.item.setPos(self.scene.snap_position(self.item, position, Qt.NoModifier))
                line, = self.scene._alignment_guide_lines()
                self.assertEqual((line.p1(), line.p2()), (start, end))

    def test_coincident_targets_connect_to_nearest_source(self) -> None:
        self.add_item(500, 350)
        self.add_item(500, 650)
        self.scene.begin_item_interaction(self.item)
        self.scene.move_interactive_items(QPointF(98, 0), Qt.NoModifier)
        line, = self.scene._alignment_guide_lines()
        self.assertEqual(line.y2(), 410)

    def test_guide_uses_transformed_reference_and_collective_moving_bounds(self) -> None:
        reference = self.add_item(900, 400, scale=1.5, rotation=30)
        other = self.add_item(403, 303)
        self.item.setSelected(True)
        other.setSelected(True)
        self.scene.begin_item_interaction(self.item, include_selection=True)
        target_bounds = reference.mapRectToScene(reference.content_rect())
        original = self.scene.multi_selection_bounds()
        delta = target_bounds.left() - original.right() - 2
        self.scene.move_interactive_items(QPointF(delta, 0), Qt.NoModifier)
        bounds = self.scene.multi_selection_bounds().united(target_bounds)
        line = next(line for line in self.scene._alignment_guide_lines() if line.x1() == line.x2())
        self.assertAlmostEqual(line.x1(), target_bounds.left())
        self.assertAlmostEqual(line.y1(), bounds.top())
        self.assertAlmostEqual(line.y2(), bounds.bottom())

    def test_changing_segment_length_repaints_old_and_new_extents(self) -> None:
        self.add_item(500, 350)
        self.scene.begin_item_interaction(self.item)
        self.scene.move_interactive_items(QPointF(98, 0), Qt.NoModifier)
        previous, = self.scene._painted_guide_lines
        with patch.object(self.scene, "update") as update:
            self.scene.move_interactive_items(QPointF(98, 300), Qt.NoModifier)
        current, = self.scene._painted_guide_lines
        dirty = [call.args[0] for call in update.call_args_list if call.args]
        for point in (previous.p1(), previous.p2(), current.p1(), current.p2()):
            self.assertTrue(any(rect.contains(point) for rect in dirty))
        self.scene.finish_item_interaction()
        self.assertEqual(self.scene._painted_guide_lines, ())
        self.assertFalse(self.scene._snap_bounds_x)

    def test_real_mouse_drag_keeps_selection_together_with_alt_switch(self) -> None:
        self.scene.finish_item_interaction()
        other = self.add_item(600, 300)
        self.item.setSelected(True)
        other.setSelected(True)
        self.view.resize(900, 600)
        self.view.show()
        self.view.centerOn(550, 300)
        self.application.processEvents()
        start = self.view.mapFromScene(self.item.mapToScene(QPointF(30, 30)))
        spacing = other.pos() - self.item.pos()
        QTest.mousePress(self.view.viewport(), Qt.LeftButton, Qt.AltModifier, start)
        for offset, modifiers in (
            (QPoint(13, 17), Qt.NoModifier), (QPoint(27, 31), Qt.NoModifier),
            (QPoint(19, 23), Qt.AltModifier),
        ):
            position = QPointF(start + offset)
            QApplication.sendEvent(self.view.viewport(), QMouseEvent(
                QEvent.MouseMove, position, position, Qt.NoButton, Qt.LeftButton,
                modifiers,
            ))
            self.assertEqual(other.pos() - self.item.pos(), spacing)
        QTest.mouseRelease(self.view.viewport(), Qt.LeftButton, Qt.AltModifier, start + offset)
        self.assertEqual(self.item.pos(), QPointF(419, 223))
        self.assertFalse(self.scene._interactive_items)


if __name__ == "__main__":
    unittest.main()
