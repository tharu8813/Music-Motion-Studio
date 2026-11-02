"""Read-only keyed poses while scrubbing the editing canvas."""
from PySide6.QtCore import QObject


class CanvasKeyframeBinding(QObject):
    def __init__(self, canvas, controller):
        super().__init__(canvas)
        self.canvas, self.controller = canvas, controller
        controller.changed.connect(self.refresh)
        controller.position_changed.connect(lambda _: self.refresh())
        controller.store.source_added.connect(lambda *_: self.refresh())
        controller.store.sources_replaced.connect(self.refresh)
        self.refresh()

    def refresh(self):
        for item in self.canvas._items.values():
            source = item.source
            if not (source.keyframes.animated or getattr(item, "_editor_keyframe_pose", False)):
                continue
            values = self.controller.evaluated(source)
            item._suppress_position_sync = True
            try:
                item.setPos(values.get("x", source.x), values.get("y", source.y))
                item.setScale(values.get("scale", source.scale))
                item.setRotation(values.get("rotation", source.rotation))
                if values:
                    item._editor_keyframe_opacity = values.get("opacity", source.opacity)
                elif hasattr(item, "_editor_keyframe_opacity"):
                    del item._editor_keyframe_opacity
                item._editor_keyframe_pose = bool(values)
                item.update()
            finally:
                item._suppress_position_sync = False
