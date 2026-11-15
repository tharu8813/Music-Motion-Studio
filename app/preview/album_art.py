"""Embedded album-art extraction for track-aware Canvas previews."""

from __future__ import annotations

from base64 import b64decode
from collections import Counter
from functools import lru_cache
from math import sin
import os
from pathlib import Path
from stat import S_ISREG

from mutagen import File as MutagenFile
from mutagen import MutagenError
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt
from PySide6.QtGui import (
    QBrush, QColor, QImage, QImageReader, QPainter, QPixmap, QRadialGradient,
)

from app.video.frame_filter import apply_color_filters


def extract_embedded_cover(audio_path: str | Path) -> QPixmap:
    """Return the first readable embedded cover image for an audio file.

    The function deliberately returns a null pixmap when a file has no artwork;
    the Canvas source then keeps its normal placeholder.  Images are cached as
    ``QImage`` objects so changing tracks does not repeatedly parse tag data.
    """
    path = Path(audio_path)
    if not path.is_file():
        return QPixmap()
    image = _cached_cover(str(path.resolve()))
    return QPixmap.fromImage(image) if not image.isNull() else QPixmap()


def extract_track_cover(
    audio_path: str | Path, cover_path: str | Path = "",
) -> QPixmap:
    """Return a project override image, falling back to embedded audio art."""
    override = Path(cover_path) if cover_path else None
    if override is not None and override.is_file():
        try:
            stat = override.stat()
            image = _cached_image_cover(
                str(override.resolve()), stat.st_mtime_ns, stat.st_size,
            )
            if not image.isNull():
                return QPixmap.fromImage(image)
        except OSError:
            pass
    return extract_embedded_cover(audio_path)


# Lists show covers at up to 46 px; twice that stays sharp on a 2x display.
THUMBNAIL_EDGE = 96


def track_cover_thumbnail(
    audio_path: str | Path, cover_path: str | Path = "", edge: int = THUMBNAIL_EDGE,
) -> QPixmap:
    """A small cover for lists, decoded straight at ``edge`` px (never at full size).

    Covering ``edge`` x ``edge``; a null pixmap when there is no artwork.
    """
    image = prewarm_cover_thumbnail(audio_path, cover_path, edge)
    return QPixmap.fromImage(image) if not image.isNull() else QPixmap()


def prewarm_cover_thumbnail(
    audio_path: str | Path, cover_path: str | Path = "", edge: int = THUMBNAIL_EDGE,
) -> QImage:
    """The cached thumbnail as a QImage; safe off the GUI thread (no QPixmap)."""
    source = _cover_source(audio_path, cover_path)
    return QImage() if source is None else _cached_thumbnail(*source, max(1, int(edge)))


def _cover_source(
    audio_path: str | Path, cover_path: str | Path = "",
) -> tuple[str, bool, int, int] | None:
    """``(path, is image file, mtime_ns, size)`` of the artwork a track shows.

    One stat per file (lists call this for every row): the path is only made
    absolute, not resolved -- the stat already tells a changed file apart.
    """
    for path, direct_image in ((cover_path, True), (audio_path, False)):
        if not path:
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        if S_ISREG(stat.st_mode):
            return os.path.abspath(path), direct_image, stat.st_mtime_ns, stat.st_size
    return None


def _read_scaled(reader: QImageReader, edge: int) -> QImage:
    size = reader.size()
    if size.isValid():
        # JPEG decodes at 1/2, 1/4, 1/8 scale: far faster than decoding and shrinking.
        reader.setScaledSize(size.scaled(
            QSize(edge, edge), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        ))
    image = reader.read()
    return image if not image.isNull() else QImage()


@lru_cache(maxsize=512)
def _cached_thumbnail(
    source_path: str, direct_image: bool, _modified_ns: int, _file_size: int, edge: int,
) -> QImage:
    if direct_image:
        return _read_scaled(QImageReader(source_path), edge)
    try:
        audio = MutagenFile(source_path)
    except (MutagenError, OSError):
        return QImage()
    for data in _cover_candidates(audio) if audio is not None else ():
        buffer = QBuffer()
        buffer.setData(QByteArray(data))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        image = _read_scaled(QImageReader(buffer), edge)
        if not image.isNull():
            return image
    return QImage()


def extract_track_personal_color(
    audio_path: str | Path, cover_path: str | Path = "",
) -> QColor:
    """Return the current track's dominant, display-friendly personal color."""
    audio = Path(audio_path)
    override = Path(cover_path) if cover_path else None
    source_path = ""
    direct_image = False
    stat = None
    try:
        if override is not None and override.is_file():
            stat = override.stat()
            source_path = str(override.resolve())
            direct_image = True
        elif audio.is_file():
            stat = audio.stat()
            source_path = str(audio.resolve())
    except OSError:
        pass
    if not source_path or stat is None:
        return QColor()
    rgba = _cached_personal_color(
        source_path, direct_image, stat.st_mtime_ns, stat.st_size,
    )
    return QColor.fromRgba(rgba) if rgba is not None else QColor()


def adjust_personal_color(
    personal_color: QColor,
    fallback: str,
    *,
    brightness: float = 0.0,
    saturation: float = 0.0,
    hue_shift: float = 0.0,
    strength: float = 1.0,
) -> str:
    """Adjust and blend a personal color while preserving channel alpha."""
    original = QColor(fallback)
    if not original.isValid():
        original = QColor("#FFFFFF")
    if not personal_color.isValid():
        return original.name(
            QColor.NameFormat.HexRgb
            if original.alpha() == 255 else QColor.NameFormat.HexArgb
        ).upper()
    adjusted = QColor(personal_color)
    hue = adjusted.hsvHue()
    if hue < 0:
        hue = 0
    adjusted.setHsv(
        round((hue + max(-180.0, min(180.0, hue_shift))) % 360),
        max(0, min(255, round(
            adjusted.hsvSaturation()
            + max(-100.0, min(100.0, saturation)) * 2.55
        ))),
        max(0, min(255, round(
            adjusted.value()
            + max(-100.0, min(100.0, brightness)) * 2.55
        ))),
    )
    amount = max(0.0, min(1.0, strength))
    result = QColor(
        round(original.red() + (adjusted.red() - original.red()) * amount),
        round(original.green() + (adjusted.green() - original.green()) * amount),
        round(original.blue() + (adjusted.blue() - original.blue()) * amount),
        original.alpha(),
    )
    return result.name(
        QColor.NameFormat.HexRgb
        if result.alpha() == 255 else QColor.NameFormat.HexArgb
    ).upper()


# How many distinct ambient-background frames are produced per second of the
# final video. The colour field drifts slowly, so this low rate looks smooth
# while keeping preview and export cheap. The export state key uses the same
# rate so it re-captures the background exactly this often.
AMBIENT_FLOW_HZ = 12

# The soft colour field is painted at this long-edge resolution and then scaled
# up. Every element is a blurred blob, so the upscale is visually lossless.
_AMBIENT_FIELD_LONG_EDGE = 360
# Radians per second of video for the drift. Small = a slow, river-like current.
_AMBIENT_FLOW_SPEED = 0.9

_PaletteEntry = tuple[tuple[int, int, int], float]
_Palette = tuple[_PaletteEntry, ...]


def _render_ambient_field(palette: _Palette, width: int, height: int,
                          blur_radius: float, phase: float) -> QImage:
    """Paint the drifting, palette-weighted colour field for one instant.

    Each dominant colour is a soft radial blob whose centre follows a slow,
    non-repeating Lissajous path, so the field reads as a flowing current
    rather than orbiting dots. Blob strength tracks the colour's image share.
    """
    if not palette or width <= 0 or height <= 0:
        return QImage()
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    base = QColor(*palette[0][0])
    hue = base.hue() if base.hue() >= 0 else 215
    base.setHsv(hue, min(255, base.saturation() + 22),
                max(22, int(base.value() * 0.42)))
    image.fill(base)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    t = phase * _AMBIENT_FLOW_SPEED
    softness = max(0.0, min(1.0, blur_radius / 80.0))
    reach = max(width, height) * (0.42 + 0.38 * softness)
    for index, (rgb, weight) in enumerate(palette[1:], start=1):
        fx = 0.10 + 0.037 * index
        fy = 0.083 + 0.041 * index
        px = index * 1.7
        py = index * 2.9 + 0.6
        cx = width * (0.5 + 0.30 * sin(t * fx + px) + 0.12 * sin(t * fx * 2.3 + px * 1.6))
        cy = height * (0.5 + 0.30 * sin(t * fy + py) + 0.12 * sin(t * fy * 1.9 + py * 1.3))
        blob_radius = max(1.0, reach * (0.82 + 0.22 * sin(t * 0.31 + index)))
        glow = QRadialGradient(cx, cy, blob_radius)
        vivid = QColor(*rgb)
        alpha = max(70, min(225, round(130 + 180 * weight)))
        vivid.setAlpha(alpha)
        glow.setColorAt(0.0, vivid)
        glow.setColorAt(
            0.35 + 0.4 * softness,
            QColor(vivid.red(), vivid.green(), vivid.blue(), round(alpha * 0.4)),
        )
        glow.setColorAt(1.0, QColor(vivid.red(), vivid.green(), vivid.blue(), 0))
        painter.fillRect(0, 0, width, height, QBrush(glow))
    shade = QColor("#05070C")
    shade.setAlpha(40)
    painter.fillRect(0, 0, width, height, shade)
    painter.end()
    return image


def create_cached_ambient_background(audio_path: str | Path, width: int, height: int,
                                     blur_radius: float = 24.0,
                                     cover_path: str | Path = "",
                                     phase: float = 0.0, *,
                                     motion: float = 1.0,
                                     brightness: float = 0.0,
                                     contrast: float = 0.0) -> QPixmap:
    """Return the ambient backdrop for one instant, reusing cached work.

    Palette extraction is cached per artwork; the drifting field is cached per
    quantised ``phase`` step so paused playback and repeated frames are free.
    ``brightness``/``contrast`` (the source's image filters) are applied to the
    small field before the upscale: the same look for a fraction of the pixels.
    """
    audio = Path(audio_path)
    override = Path(cover_path) if cover_path else None
    if width <= 0 or height <= 0:
        return QPixmap()
    source_path = ""
    direct_image = False
    stat = None
    try:
        if override is not None and override.is_file():
            stat = override.stat()
            source_path = str(override.resolve())
            direct_image = True
        elif audio.is_file():
            stat = audio.stat()
            source_path = str(audio.resolve())
    except OSError:
        pass
    if not source_path or stat is None:
        return QPixmap()
    palette = _cached_ambient_palette(
        source_path, direct_image, stat.st_mtime_ns, stat.st_size,
    )
    if not palette:
        return QPixmap()
    width = max(1, int(width))
    height = max(1, int(height))
    field_scale = min(1.0, _AMBIENT_FIELD_LONG_EDGE / max(width, height))
    small_w = max(2, round(width * field_scale))
    small_h = max(2, round(height * field_scale))
    phase_step = round(
        max(0.0, float(phase)) * max(0.0, min(3.0, float(motion)))
        * AMBIENT_FLOW_HZ
    )
    scaled = _cached_ambient_scaled(
        palette, small_w, small_h, round(max(0.0, blur_radius) * 10),
        phase_step, width, height, float(brightness), float(contrast),
    )
    return QPixmap.fromImage(scaled) if not scaled.isNull() else QPixmap()


@lru_cache(maxsize=4)
def _cached_ambient_scaled(
    palette: _Palette, small_width: int, small_height: int,
    blur_radius_tenths: int, phase_step: int, width: int, height: int,
    brightness: float = 0.0, contrast: float = 0.0,
) -> QImage:
    """Cache the full-size ambient frame.

    Export renders many samples per quantised phase step (progress bar and
    lyric timing points force near-per-frame sampling); without this the smooth
    upscale from the small colour field to the output resolution was ~40% of
    the whole canvas-capture cost.
    """
    field = _cached_ambient_field(
        palette, small_width, small_height, blur_radius_tenths, phase_step,
    )
    if field.isNull():
        return QImage()
    if brightness or contrast:
        field = apply_color_filters(field, brightness=brightness, contrast=contrast)
    return field.scaled(
        width, height, Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _dominant_colors(cover: QPixmap) -> list[QColor]:
    """Extract a compact, saturated palette without retaining image detail."""
    sample = cover.toImage().scaled(
        48, 48, Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    ).convertToFormat(QImage.Format.Format_ARGB32)
    counts: Counter[tuple[int, int, int]] = Counter()
    for y in range(sample.height()):
        for x in range(sample.width()):
            color = sample.pixelColor(x, y)
            if color.alpha() < 40:
                continue
            # Quantization preserves the dominant palette while eliminating
            # individual photographic pixels and tiny high-contrast details.
            key = (color.red() // 32 * 32, color.green() // 32 * 32,
                   color.blue() // 32 * 32)
            counts[key] += 1
    if not counts:
        return [QColor("#202633"), QColor("#42536B"), QColor("#765B8E")]
    ordered = sorted(
        counts.items(),
        key=lambda item: item[1] * (1.25 if QColor(*item[0]).saturation() > 65 else 1.0),
        reverse=True,
    )
    colors = [QColor(*rgb) for rgb, _count in ordered[:4]]
    while len(colors) < 4:
        colors.append(QColor(colors[-1]))
    return colors


def _dominant_palette(cover: QPixmap) -> _Palette:
    """Return up to five ``(rgb, weight)`` pairs, weight = share of the artwork."""
    if cover.isNull():
        return ()
    sample = cover.toImage().scaled(
        48, 48, Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    ).convertToFormat(QImage.Format.Format_ARGB32)
    counts: Counter[tuple[int, int, int]] = Counter()
    for y in range(sample.height()):
        for x in range(sample.width()):
            color = sample.pixelColor(x, y)
            if color.alpha() < 40:
                continue
            counts[(color.red() // 32 * 32, color.green() // 32 * 32,
                    color.blue() // 32 * 32)] += 1
    if not counts:
        return (((32, 38, 51), 0.5), ((66, 83, 107), 0.3), ((118, 91, 142), 0.2))
    ordered = sorted(
        counts.items(),
        key=lambda item: item[1] * (1.25 if QColor(*item[0]).saturation() > 65 else 1.0),
        reverse=True,
    )[:5]
    total = sum(count for _rgb, count in ordered) or 1
    return tuple((rgb, count / total) for rgb, count in ordered)


@lru_cache(maxsize=128)
def _cached_cover(audio_path: str) -> QImage:
    """Read cover bytes from common Mutagen tag representations."""
    try:
        audio = MutagenFile(audio_path)
    except (MutagenError, OSError):
        return QImage()
    if audio is None:
        return QImage()
    for data in _cover_candidates(audio):
        image = QImage.fromData(data)
        if not image.isNull():
            return image
    return QImage()


@lru_cache(maxsize=128)
def _cached_image_cover(
    image_path: str, _modified_ns: int, _file_size: int,
) -> QImage:
    reader = QImageReader(image_path)
    size = reader.size()
    if size.isValid() and (size.width() > 4096 or size.height() > 4096):
        reader.setScaledSize(size.scaled(
            QSize(4096, 4096), Qt.AspectRatioMode.KeepAspectRatio,
        ))
    image = reader.read()
    return image if not image.isNull() else QImage()


@lru_cache(maxsize=64)
def _cached_ambient_palette(source_path: str, direct_image: bool,
                            modified_ns: int, file_size: int) -> _Palette:
    """Cache the dominant palette (with weights) once per physical artwork."""
    cover_image = (
        _cached_image_cover(source_path, modified_ns, file_size) if direct_image
        else _cached_cover(source_path)
    )
    if cover_image.isNull():
        return ()
    return _dominant_palette(QPixmap.fromImage(cover_image))


@lru_cache(maxsize=192)
def _cached_ambient_field(palette: _Palette, small_width: int, small_height: int,
                          blur_radius_tenths: int, phase_step: int) -> QImage:
    """Cache the small drifting colour field per quantised phase step."""
    return _render_ambient_field(
        palette, small_width, small_height, blur_radius_tenths / 10.0,
        phase_step / AMBIENT_FLOW_HZ,
    )


@lru_cache(maxsize=256)
def _cached_personal_color(
    source_path: str, direct_image: bool,
    modified_ns: int, file_size: int,
) -> int | None:
    """Cache dominant-color extraction once per physical artwork revision."""
    image = (
        _cached_image_cover(source_path, modified_ns, file_size)
        if direct_image else _cached_cover(source_path)
    )
    if image.isNull():
        return None
    palette = _dominant_colors(QPixmap.fromImage(image))
    # Album covers often devote most pixels to black, white, or a muted
    # photographic background. Prefer a visible accent from the dominant
    # palette while still choosing the brightest neutral for monochrome art.
    selected = max(
        palette,
        key=lambda color: (
            color.saturation() * 0.68 + color.value() * 0.32
            - (90.0 if color.value() < 36 else 0.0)
        ),
    )
    return selected.rgba()


def _cover_candidates(audio: object) -> list[bytes]:
    """Collect artwork byte payloads from ID3, FLAC, MP4, and generic tags."""
    candidates: list[bytes] = []
    pictures = getattr(audio, "pictures", None)
    if pictures:
        candidates.extend(
            bytes(picture.data) for picture in pictures if getattr(picture, "data", None)
        )
    tags = getattr(audio, "tags", None)
    if not tags:
        return candidates
    getall = getattr(tags, "getall", None)
    if callable(getall):
        candidates.extend(
            bytes(frame.data) for frame in getall("APIC") if getattr(frame, "data", None)
        )
    values = getattr(tags, "values", None)
    if callable(values):
        for value in values():
            if getattr(value, "data", None):
                candidates.append(bytes(value.data))
            elif isinstance(value, (bytes, bytearray)):
                candidates.append(bytes(value))
            elif isinstance(value, (list, tuple)):
                candidates.extend(bytes(entry) for entry in value if isinstance(entry, (bytes, bytearray)))
    get = getattr(tags, "get", None)
    if callable(get):
        encoded_picture = get("metadata_block_picture")
        if encoded_picture:
            try:
                payload = encoded_picture[0] if isinstance(encoded_picture, list) else encoded_picture
                decoded = b64decode(str(payload))
                # FLAC picture blocks contain a header before image bytes.  They
                # are uncommon here, so QImage is allowed to reject the block.
                candidates.append(decoded)
            except (ValueError, TypeError):
                pass
    return candidates
