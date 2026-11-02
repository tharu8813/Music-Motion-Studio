"""Animated sources used by the opt-in native GPU preview soak."""
from pathlib import Path
from PySide6.QtGui import QColor, QImage
from app.animation.keyframes import Keyframe
from app.canvas.source_item import SourceItem
from app.models.source import Shadow, Source, SourceType


def add_keyframe_runtime_fixture(scene, store, video, duration):
    scene.set_artboard_size(640, 360)
    times = list(range(0, int(duration) + 7, 3))

    def curve(low, high):
        return [Keyframe(t, low if index % 2 == 0 else high, "bezier", (.25, -.25, .65, 1.4))
                for index, t in enumerate(times)]

    video.keyframes.global_curves = {"x": curve(-8, 8), "rotation": curve(-1, 1), "opacity": curve(.8, 1)}
    shape = Source(SourceType.SHAPE, "Bezier geometry and shadow", x=35, y=250,
                   width=100, height=50, z_index=5, fill_color="#FFB347",
                   shadow=Shadow(enabled=True, color="#FFFFFF"))
    shape.keyframes.global_curves = {
        "width": curve(80, 240), "height": curve(30, 80), "border_radius": curve(0, 30),
        "outline_width": curve(0, 8), "shadow.opacity": curve(.15, .8),
        "shadow.blur_radius": curve(2, 18), "shadow.offset_x": curve(-8, 12),
        "shadow.offset_y": curve(-4, 8),
    }
    text = Source(SourceType.TEXT, "Bezier font", x=40, y=20, width=550, height=110,
                  text="베지어 · GPU · Keyframes", font_size=24, z_index=6, fill_color="#00000000")
    text.keyframes.global_curves = {"font_size": curve(20, 46), "x": curve(20, 60)}
    image_path = Path(video.video_paths[0]).with_name("blur-pattern.png")
    pattern = QImage(64, 64, QImage.Format.Format_RGB32)
    for y in range(64):
        for x in range(64):
            pattern.setPixelColor(x, y, QColor("#FFFFFF" if (x // 8 + y // 8) % 2 else "#191919"))
    if not pattern.save(str(image_path)):
        raise AssertionError("Could not save the blur fixture")
    image = Source(SourceType.IMAGE, "Bezier blur", x=510, y=230, width=90, height=90,
                   content_path=str(image_path), z_index=7)
    image.keyframes.global_curves = {"blur": curve(0, 4)}
    for source in (shape, text, image):
        scene.addItem(SourceItem(source))
        store.add(source)
