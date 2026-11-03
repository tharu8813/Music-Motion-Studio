"""Stage timings around the native quality benchmark (inclusive wall times).

Use identical arguments before/after. Stages overlap and must not be summed.
The QPixmap proxy instruments the motion Canvas only, without changing Qt.
"""
import json
from functools import wraps
from pathlib import Path
import platform
import subprocess
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import verify_motion_editor_quality as quality
from app.animation import custom_motion
from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.dialogs import motion_canvas
from app.dialogs.custom_motion_dialog import CustomMotionDialog
from app.dialogs.motion_preview_media import MotionPreviewMedia
from app.dialogs.motion_timeline import MotionTimeline
from app.preview.canvas_snapshot import CanvasSnapshot

samples = {}


def baseline_methods():
    """Replay HEAD's changed hot paths in-process, without editing the checkout.

    Constructors retain extra unused fields; all measured render/media methods
    use HEAD. This allows adjacent before/after runs after a long test suite.
    """
    for path, owner, names in (
        ("app/canvas/source_item.py", SourceItem, ("_ensure_video_decoder", "_video_media_status_changed",
            "_apply_video_seek", "_video_frame_changed", "_video_filter_finished", "set_video_preview_position")),
        ("app/dialogs/motion_preview_media.py", MotionPreviewMedia, ("sync",)),
        ("app/dialogs/custom_motion_dialog.py", CustomMotionDialog, ("render_now",)),
        ("app/dialogs/motion_timeline.py", MotionTimeline, ("paintEvent",)),
    ):
        source = subprocess.check_output(["git", "show", f"HEAD:{path}"], cwd=quality.ROOT).decode("utf-8")
        namespace = {"__name__": owner.__module__}
        exec(compile(source, "HEAD:"+path, "exec"), namespace)
        original = namespace[owner.__name__]
        for name in names:
            setattr(owner, name, getattr(original, name))
    initialize = MotionPreviewMedia.__init__
    def without_timestamp_validation(self, *args, **kwargs):
        initialize(self, *args, **kwargs)
        for item in self.items:
            item._video_validate_timestamps = False
    MotionPreviewMedia.__init__ = without_timestamp_validation


def measured(name, function):
    @wraps(function)
    def call(*args, **kwargs):
        start = perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            samples.setdefault(name, []).append((perf_counter()-start)*1000)
    return call


def main():
    baseline = "--baseline" in sys.argv
    if baseline:
        sys.argv.remove("--baseline")
        baseline_methods()
    for owner, method, label in (
        (custom_motion, "evaluate_curve", "keyframe_evaluation"),
        (custom_motion.CustomMotion, "at", "motion_pose"),
        (CanvasSnapshot, "capture_track", "canvas_snapshot"),
        (CanvasScene, "render", "scene_render"),
        (SourceItem, "_apply_image_filters", "image_filter"),
        (SourceItem, "_video_frame_changed", "video_frame"),
        (MotionPreviewMedia, "sync", "media_sync"),
        (CustomMotionDialog, "reaction_preview", "audio_lookup"),
        (CustomMotionDialog, "tick", "play_timer"),
        (CustomMotionDialog, "refresh_values", "widget_update"),
        (CustomMotionDialog, "render_now", "render_request"),
        (MotionTimeline, "paintEvent", "keyframe_paint"),
    ):
        wrapped = measured(label, getattr(owner, method))
        setattr(owner, method, staticmethod(wrapped) if owner is CanvasSnapshot else wrapped)
    original = motion_canvas.QPixmap
    class PixmapProbe:
        def __new__(cls, *args, **kwargs):
            return original(*args, **kwargs)
        fromImage = staticmethod(measured("image_to_pixmap", original.fromImage))
    motion_canvas.QPixmap = PixmapProbe
    # Also exercise native timer/loop callbacks; ordinary seek samples are kept
    # separate by the original harness.
    original_show = CustomMotionDialog.show
    def show(dialog):
        original_show(dialog)
        dialog.toggle_play()
        quality.QTest.qWait(100)
        if dialog.timer.isActive():
            dialog.toggle_play()
    CustomMotionDialog.show = show
    quality.main()
    from PySide6 import __version__ as qt_binding
    output = Path(sys.argv[sys.argv.index("--output")+1]) if "--output" in sys.argv else quality.ROOT / "work" / "motion-quality-ko"
    stages = {}
    for name, values in samples.items():
        stages[name] = {"count": len(values), "median_ms": float(quality.np.median(values)),
            "p95_ms": float(quality.np.percentile(values, 95)), "max_ms": max(values)}
    (output / "stages.json").write_text(json.dumps({"platform": platform.platform(),
        "code": "HEAD hot-path replay" if baseline else "working tree",
        "python": sys.version, "pyside": qt_binding, "stages": stages}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
