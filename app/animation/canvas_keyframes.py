"""Read-only keyed poses while scrubbing the editing canvas."""
from PySide6.QtCore import QObject
from contextlib import contextmanager
from app.animation.keyframes import resolved_source


@contextmanager
def keyframe_source(item, values):
    original = item.source
    resolved = resolved_source(original, values)
    filter_image = "blur" in values or (getattr(item, "_editor_keyframe_pose", False)
                                        and "blur" in getattr(item, "_editor_keyframe_values", {}))
    if resolved is original and not filter_image:
        yield original
        return
    pixmap, origin, suppressed = item._pixmap, item.transformOriginPoint(), item._suppress_position_sync
    item._suppress_position_sync = True
    item.prepareGeometryChange()
    item.source = resolved
    item._sync_transform_origin()
    try:
        if filter_image and not item._raw_pixmap.isNull():
            item._pixmap = item._apply_image_filters(item._raw_pixmap)
        yield resolved
    finally:
        item.prepareGeometryChange()
        item.source = original
        item.setTransformOriginPoint(origin)
        item._pixmap = pixmap
        item._suppress_position_sync = suppressed
        item.update()


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
            source = self.controller.store.get(item.source.id)
            if source is None:
                continue
            if not (source.keyframes.animated or getattr(item, "_editor_keyframe_pose", False)):
                continue
            values = self.controller.evaluated(source)
            item._suppress_position_sync = True
            try:
                item.prepareGeometryChange()
                item._editor_keyframe_base = source
                item.source = resolved_source(source, values)
                item._sync_transform_origin()
                item.setPos(values.get("x", source.x), values.get("y", source.y))
                item.setScale(values.get("scale", source.scale))
                item.setRotation(values.get("rotation", source.rotation))
                if values:
                    item._editor_keyframe_opacity = values.get("opacity", source.opacity)
                elif hasattr(item, "_editor_keyframe_opacity"):
                    del item._editor_keyframe_opacity
                item._editor_keyframe_pose = bool(values)
                previous_values = getattr(item, "_editor_keyframe_values", {})
                if "blur" in values or "blur" in previous_values:
                    filter_key = (item._raw_pixmap.cacheKey(), item.source.brightness, item.source.contrast, item.source.blur)
                    if filter_key != getattr(item, "_editor_keyframe_filter_key", None):
                        item._pixmap = item._apply_image_filters(item._raw_pixmap)
                        item._editor_keyframe_filter_key = filter_key
                item._editor_keyframe_values = values
                item.update()
            finally:
                item._suppress_position_sync = False
