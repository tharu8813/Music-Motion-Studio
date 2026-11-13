"""Non-destructive animation preview directly on a Canvas source item."""

from __future__ import annotations

from collections.abc import Callable
import numpy as np

from PySide6.QtCore import (
    QObject, QPauseAnimation, QPointF, QSequentialAnimationGroup, QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QTransform

from app.canvas.source_item import SourceItem
from app.models.source import Source, SourceType
from app.animation.curves import AnimationPose, SAMPLE_TEMPO, music_reactive_pose, source_loop_pose
from app.animation.custom_motion import source_motion
from app.renderer.python_visualizer import PythonVisualizerRenderer


class CanvasAnimationPreviewController(QObject):
    """Animate graphics properties and restore the item exactly afterward."""

    finished = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._group: QSequentialAnimationGroup | None = None
        self._item: SourceItem | None = None
        self._original: tuple[QPointF, float, float, float, QTransform, bool, float, int | None] | None = None

    @property
    def active(self) -> bool:
        return self._group is not None

    def preview(self, item: SourceItem, source: Source) -> bool:
        """Play entrance, loop, sample music reaction and exit without editing the source."""
        music_reactive = source.source_type in {SourceType.TEXT, SourceType.LYRICS} and source.uses_bass_reaction
        if self.active or (
            source.animation_in == "none" and source.animation_out == "none"
            and source.loop_motion == "none" and not music_reactive
        ):
            return False
        self._item = item
        self._original = (
            QPointF(item.pos()), item.scale(), item.rotation(), item.opacity(),
            item.transform(), item.isSelected(),
            item._music_reaction_level,
            item._music_preview_current_line,
        )
        item._suppress_position_sync = True
        self._set_selected_without_signal(item, False)

        def milliseconds(seconds: float) -> int:
            return max(100, min(10000, round(seconds * 1000)))

        rest = source_motion(source, 1, True) if source.animation_in == "custom" else AnimationPose()
        sequence = QSequentialAnimationGroup(self)
        if source.animation_in != "none":
            self._apply(item, source, source_motion(source, 0.0, True))
            sequence.addAnimation(self._phase(
                item, source, milliseconds(source.animation_in_duration),
                lambda progress: source_motion(source, progress, True),
            ))
        if source.loop_motion != "none":
            tempo = SAMPLE_TEMPO if source.uses_tempo_sync else None
            period = tempo.period * source.loop_tempo_beats if tempo else source.loop_motion_period
            loop_seconds = max(1.5, min(8.0 if tempo else 6.0, period * 2.0))
            sequence.addAnimation(self._phase(
                item, source, round(loop_seconds * 1000),
                lambda progress: rest.combined(source_loop_pose(
                    source, progress * loop_seconds, progress * loop_seconds, tempo,
                )),
            ))
        if music_reactive:
            # This button demonstrates two sample hits; Full Preview uses real audio.
            if source.source_type is SourceType.LYRICS and source.subtitle_current_line < 0:
                lines = (item._render_text() or source.subtitle_fallback).splitlines()
                item._music_preview_current_line = len([line for line in lines if line.strip()]) // 2
            samples = np.zeros((120, 24), dtype=np.float32)
            samples[12:18] = 1
            samples[54:60] = 0.7
            envelope = PythonVisualizerRenderer.music_envelope(samples, 30, source.music_reaction_profile)

            def music_pose(progress: float) -> AnimationPose:
                level = PythonVisualizerRenderer.music_reaction_level(
                    envelope, 30, progress * 4, source.music_reactive_offset,
                )
                if source.source_type is SourceType.LYRICS:
                    item._music_reaction_level = level
                    item.update()
                    return rest
                return rest.combined(music_reactive_pose(source.music_reactive_effect, level,
                                           source.music_reactive_strength, source.font_size))

            sequence.addAnimation(self._phase(
                item, source, 4000, music_pose,
            ))
        if source.animation_out != "none":
            sequence.addAnimation(QPauseAnimation(320))
            sequence.addAnimation(self._phase(
                item, source, milliseconds(source.animation_out_duration),
                lambda progress: source_motion(source, progress, False).combined(
                    rest if source.animation_out != "custom" else AnimationPose()),
            ))
        sequence.finished.connect(self._restore)
        self._group = sequence
        sequence.start()
        return True

    def cancel(self) -> None:
        if self._group is not None:
            self._group.stop()
            self._restore()

    def _phase(
        self, item: SourceItem, source: Source, duration: int,
        pose_at: Callable[[float], AnimationPose],
    ) -> QVariantAnimation:
        animation = QVariantAnimation()
        animation.setDuration(duration)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.valueChanged.connect(
            lambda value: self._apply(item, source, pose_at(float(value)))
        )
        return animation

    @staticmethod
    def _apply(item: SourceItem, source: Source, pose: AnimationPose) -> None:
        # Source opacity is already applied inside SourceItem.paint(). Graphics
        # opacity is only the animation multiplier; including source.opacity
        # here would square semi-transparent elements.
        item.setPos(source.x + pose.dx, source.y + pose.dy)
        item.setScale(source.scale * pose.scale)
        item.setRotation(source.rotation + pose.rotation)
        item.setOpacity(pose.opacity)
        center_x = source.width / 2.0
        item.setTransform(
            QTransform().translate(center_x, 0.0).scale(pose.scale_x, 1.0).translate(-center_x, 0.0)
            if pose.scale_x != 1.0 else QTransform()
        )

    def _restore(self) -> None:
        group = self._group
        item = self._item
        original = self._original
        self._group = None
        self._item = None
        self._original = None
        if item is not None and original is not None:
            position, scale, rotation, opacity, transform, selected, music_level, current_line = original
            item._music_reaction_level = music_level
            item._music_preview_current_line = current_line
            item.setPos(position)
            item.setScale(scale)
            item.setRotation(rotation)
            item.setOpacity(opacity)
            item.setTransform(transform)
            item._suppress_position_sync = False
            self._set_selected_without_signal(item, selected)
            item.update()
        if group is not None:
            group.deleteLater()
        self.finished.emit()

    @staticmethod
    def _set_selected_without_signal(item: SourceItem, selected: bool) -> None:
        scene = item.scene()
        if scene is None:
            item.setSelected(selected)
            return
        blocked = scene.blockSignals(True)
        try:
            item.setSelected(selected)
        finally:
            scene.blockSignals(blocked)
