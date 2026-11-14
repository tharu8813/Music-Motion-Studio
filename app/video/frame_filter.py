"""Bounded, worker-safe filtering for live video element previews."""

from __future__ import annotations

from app.utils.performance import timed

from dataclasses import dataclass
import logging
from math import ceil

import numpy as np
from PySide6.QtCore import QObject, QRunnable, Qt, Signal, Slot
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QVideoFrame

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VideoFrameFilterSettings:
    """Preview-only filter values and the useful display resolution."""

    width: int
    height: int
    brightness: float = 0.0
    contrast: float = 0.0
    saturation: float = 1.0
    grayscale: bool = False
    blur: float = 0.0


class VideoFrameFilterSignals(QObject):
    """Return a worker result to its GUI-owned SourceItem."""

    finished = Signal(object, QImage, QImage)


class VideoFrameFilterTask(QRunnable):
    """Convert, scale and filter one immutable frame outside the GUI thread."""

    def __init__(
        self,
        frame: QImage | QVideoFrame,
        settings: VideoFrameFilterSettings,
        generation: object,
        signals: VideoFrameFilterSignals,
    ) -> None:
        super().__init__()
        self.frame = QImage(frame) if isinstance(frame, QImage) else QVideoFrame(frame)
        self.settings = settings
        self.generation = generation
        self.signals = signals
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        image = QImage()
        try:
            image = self.frame if isinstance(self.frame, QImage) else video_frame_to_image(self.frame)
            filtered = filter_video_frame(image, self.settings)
        except Exception as error:  # noqa: BLE001 - a bad frame must not kill Preview
            _log_filter_failure(error, image, self.settings)
            filtered = QImage()
        try:
            self.signals.finished.emit(self.generation, filtered, image)
        except RuntimeError:
            # The owning Canvas may have closed after this bounded task began.
            pass


_LOGGED_FAILURES: set[tuple[str, str]] = set()
_MAX_LOGGED_FAILURES = 32


@timed("video.to_image_seconds")
def video_frame_to_image(frame: QVideoFrame) -> QImage:
    return frame.toImage()


def _log_filter_failure(
    error: Exception, image: QImage, settings: VideoFrameFilterSettings,
) -> None:
    """Log each distinct failure once; the same one repeats on every frame."""
    signature = (type(error).__name__, str(error))
    if signature in _LOGGED_FAILURES or len(_LOGGED_FAILURES) >= _MAX_LOGGED_FAILURES:
        return
    _LOGGED_FAILURES.add(signature)
    LOGGER.warning(
        "Preview video filter failed; showing an empty frame instead "
        "(input %dx%d format=%s, settings=%s)",
        image.width(), image.height(), image.format().name, settings,
        exc_info=error,
    )


@timed("video.cpu_filter_seconds")
def filter_video_frame(
    image: QImage, settings: VideoFrameFilterSettings,
) -> QImage:
    """Return a preview RGBA frame within the useful display resolution."""
    if image.isNull():
        return QImage()
    target_width = max(8, min(1920, int(settings.width)))
    target_height = max(8, min(1080, int(settings.height)))
    neutral = (
        settings.brightness == 0.0 and settings.contrast == 0.0
        and settings.saturation == 1.0 and not settings.grayscale
        and settings.blur == 0.0
    )
    # The compositor already scales a video into its destination rectangle.
    # Keep native pixels when a clean frame is smaller than that rectangle.
    scaled = image
    if not neutral or image.width() > target_width or image.height() > target_height:
        scaled = _scale_video_image(image, target_width, target_height)
    return apply_color_filters(
        scaled,
        brightness=settings.brightness,
        contrast=settings.contrast,
        saturation=settings.saturation,
        grayscale=settings.grayscale,
        blur=settings.blur,
    )


@timed("video.cpu_scale_seconds")
def _scale_video_image(image: QImage, width: int, height: int) -> QImage:
    return image.scaled(
        width, height,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


@timed("video.cpu_color_blur_seconds")
def apply_color_filters(
    image: QImage,
    *,
    brightness: float = 0.0,
    contrast: float = 0.0,
    saturation: float = 1.0,
    grayscale: bool = False,
    blur: float = 0.0,
) -> QImage:
    """Return an RGBA8888 copy of *image* with vectorized brightness, contrast,
    saturation and box-blur applied. Shared by the live-video preview and the
    static image/background element filters so both look identical."""
    result = image.convertToFormat(QImage.Format.Format_RGBA8888)
    if (
        brightness == 0.0
        and contrast == 0.0
        and saturation == 1.0
        and not grayscale
        and blur <= 0.0
    ):
        return result

    if (brightness != 0.0 or contrast != 0.0) and saturation == 1.0 and not grayscale:
        # Per-channel only: the same float math on 256 levels, then one lookup per
        # channel -- identical bytes, about three times faster than per pixel.
        levels = np.arange(256, dtype=np.float32)
        levels = (levels - 128.0) * (1.0 + float(contrast) / 100.0) + 128.0 + float(brightness) * 2.55
        table = np.clip(levels, 0.0, 255.0).astype(np.uint8)
        pixels = _rgb_pixels(result)
        for channel in range(3):
            pixels[:, :, channel] = table[pixels[:, :, channel]]
    elif brightness != 0.0 or contrast != 0.0 or saturation != 1.0 or grayscale:
        pixels = _rgb_pixels(result)
        rgb = pixels[:, :, :3].astype(np.float32)
        rgb = (rgb - 128.0) * (1.0 + float(contrast) / 100.0) + 128.0 + float(brightness) * 2.55
        effective_saturation = 0.0 if grayscale else max(0.0, min(3.0, saturation))
        if effective_saturation != 1.0:
            luminance = (
                rgb[:, :, 0:1] * 0.2126
                + rgb[:, :, 1:2] * 0.7152
                + rgb[:, :, 2:3] * 0.0722
            )
            rgb = luminance + (rgb - luminance) * effective_saturation
        pixels[:, :, :3] = np.clip(rgb, 0.0, 255.0).astype(np.uint8)

    radius = max(0, min(40, int(ceil(blur))))
    if radius:
        result = _blurred_image(result, radius)
    return result


# A box blur only reproduces low frequencies, so it is computed on a bounded
# working resolution and scaled back.  Blurring a 4K frame directly costs well
# over a second; this keeps it near 20 ms with no visible difference.
_BLUR_WORKING_EDGE = 540


def _rgb_pixels(image: QImage) -> np.ndarray:
    """Return a writable ``(h, w, 4)`` uint8 view of an RGBA8888 image."""
    width, height = image.width(), image.height()
    stride = image.bytesPerLine()
    raw = np.frombuffer(image.bits(), dtype=np.uint8, count=image.sizeInBytes())
    return raw.reshape(height, stride)[:, : width * 4].reshape(height, width, 4)


def _blurred_image(image: QImage, radius: int) -> QImage:
    """Box-blur ``image`` on a downscaled copy, then restore its resolution."""
    width, height = image.width(), image.height()
    longest = max(width, height)
    if longest > _BLUR_WORKING_EDGE:
        factor = longest / _BLUR_WORKING_EDGE
        work = image.scaled(
            max(1, round(width / factor)), max(1, round(height / factor)),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        work_radius = max(1, round(radius / factor))
    else:
        work, factor, work_radius = image, 1.0, radius
    pixels = _rgb_pixels(work)
    rgb = pixels[:, :, :3].astype(np.float32)
    rgb = _box_blur(_box_blur(rgb, work_radius, axis=1), work_radius, axis=0)
    pixels[:, :, :3] = np.clip(rgb, 0.0, 255.0).astype(np.uint8)
    if factor > 1.0:
        return work.scaled(
            width, height, Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return work


def _box_blur(values: np.ndarray, radius: int, axis: int) -> np.ndarray:
    """Apply one O(n) edge-padded box-blur pass along an image axis.

    The summed axis is moved to the front so ``np.cumsum`` runs on contiguous
    memory; along a large-stride axis it is otherwise several times slower.
    """
    if radius <= 0:
        return values
    moved = np.ascontiguousarray(np.moveaxis(values, axis, 0))
    padded = np.pad(moved, [(radius, radius), (0, 0), (0, 0)], mode="edge")
    cumulative = np.empty((padded.shape[0] + 1, *padded.shape[1:]), dtype=np.float32)
    cumulative[0] = 0
    np.cumsum(padded, axis=0, dtype=np.float32, out=cumulative[1:])
    length = moved.shape[0]
    kernel = radius * 2 + 1
    blurred = cumulative[kernel:kernel + length] - cumulative[:length]
    blurred /= kernel
    return np.moveaxis(blurred, 0, axis)
