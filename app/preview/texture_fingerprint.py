"""Bounded image signatures for reusable preview GPU textures.

Qt changes QImage.cacheKey() when an image is repainted, even when the pixels
are identical. A digest avoids redundant pixel conversion and OpenGL uploads
for small regenerated layers. Large canvases rely on cacheKey() so reading
megabytes of pixels cannot itself become the rendering bottleneck.
"""

from __future__ import annotations

from hashlib import blake2b


MAX_FINGERPRINT_BYTES = 4 * 1024 * 1024


def image_content_signature(image, *, max_bytes: int = MAX_FINGERPRINT_BYTES):
    """Return a format- and geometry-sensitive pixel digest, or None if too large."""
    size = image.sizeInBytes()
    if not 0 < size <= max_bytes:
        return None
    try:
        return (
            int(getattr(image.format(), "value", image.format())),
            image.width(), image.height(), image.bytesPerLine(),
            blake2b(image.constBits().tobytes(), digest_size=16).digest(),
        )
    except (BufferError, TypeError, ValueError):
        # Some platform-specific QImage wrappers cannot expose the buffer.
        # Falling back to an upload is always correct.
        return None
