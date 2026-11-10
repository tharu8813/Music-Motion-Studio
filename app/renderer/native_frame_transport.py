"""Bounded MMSB transport: v1 raw planes; opt-in v2 prior-frame plane references."""
import hashlib
import json
import struct
from time import monotonic


class FrameReuseState:
    """Sender-local, bounded previous-frame digests (never retains Qt pixels)."""

    def __init__(self):
        self.previous = {}
        self.reused_planes = 0
        self.saved_bytes = 0
        # Binary-v3 only. Store references to the previous frame's immutable
        # planes, never another copy of each (up to eight 4K planes).
        self.previous_pixels = {}
        self.row_delta_planes = 0
        self.row_delta_bytes_saved = 0
        self.row_delta_compare_seconds = 0.0


HEADER = struct.Struct("<4sIQIQ")
MAX_PAYLOAD = 3840 * 2160 * 4 * 8


def _row_delta(previous: bytes, current: bytes, width: int, height: int):
    """Return the changed whole-row interval, or None if not worth patching.

    C-level memoryview equality avoids Python per-pixel work. If first and
    last rows both differ, fallback is constant-time and sends the full plane.
    Partial rows are never used, keeping BGRA geometry and pixel parity exact.
    """
    stride = width * 4
    if len(current) < 1024 * 1024 or len(previous) != len(current):
        return None
    old, new = memoryview(previous), memoryview(current)
    first = 0
    while first < height and old[first * stride:(first + 1) * stride] == new[first * stride:(first + 1) * stride]:
        first += 1
    if first == height:
        return None
    end = height
    while end > first and old[(end - 1) * stride:end * stride] == new[(end - 1) * stride:end * stride]:
        end -= 1
    # Patch only when at least 25% of the raw plane can be removed.
    return (first, end) if (end - first) * 4 <= height * 3 else None


def frame_chunks(frame, index, *, reuse_state=None, row_deltas=False):
    sizes = [layer.width * layer.height * 4 for layer in frame.layers]
    total_size = sum(sizes)
    if (not 2 <= frame.width <= 3840 or not 2 <= frame.height <= 2160
            or frame.width % 2 or frame.height % 2
            or not 1 <= len(frame.layers) <= 8 or total_size > MAX_PAYLOAD):
        raise ValueError("Invalid binary frame geometry or memory limit")
    layers = []
    reused = []
    next_digests = {}
    next_pixels = {}
    patches = []
    payloads = []
    for i, layer in enumerate(frame.layers):
        size = sizes[i]
        key = "base" if i == 0 else f"layer:{i-1}"
        if (layer.stream_key != key or len(layer.pixels) != size
                or not 0 <= layer.opacity <= 1 or (i == 0 and layer.opacity != 1)):
            raise ValueError("Invalid binary frame layer")
        digest = hashlib.sha256(layer.pixels).hexdigest()
        # A reference only reuses identical pixel dimensions + digest. This
        # does NOT freeze opacity/clip/affine geometry or track-dependent text.
        previous = reuse_state.previous.get(key) if reuse_state is not None else None
        reuse = previous == (layer.width, layer.height, digest)
        reused.append(reuse)
        next_digests[key] = (layer.width, layer.height, digest)
        next_pixels[key] = layer.pixels
        patch = None
        if row_deltas and reuse_state is not None and not reuse:
            old = reuse_state.previous_pixels.get(key)
            if old is not None and previous is not None and previous[:2] == (layer.width, layer.height):
                started = monotonic()
                patch = _row_delta(old, layer.pixels, layer.width, layer.height)
                reuse_state.row_delta_compare_seconds += monotonic() - started
            if patch is not None:
                reuse_state.row_delta_planes += 1
                reuse_state.row_delta_bytes_saved += size - (patch[1] - patch[0]) * layer.width * 4
        patches.append(patch)
        payloads.append(0 if reuse else
                        ((patch[1] - patch[0]) * layer.width * 4 if patch is not None else size))
        layers.append(dict(file=f"layer-{i}.bgra", stream_key=key, z_order=i,
                           opacity=layer.opacity, bytes=size, sha256=digest))
        if frame.schema == 2:
            layers[-1].update(width=layer.width, height=layer.height)
            if layer.inverse_transform is not None:
                layers[-1]["inverse_transform"] = layer.inverse_transform
            if layer.clip is not None:
                layers[-1]["clip"] = layer.clip
    descriptor = dict(schema=frame.schema, format=f"mms_canvas_bgra_layers_v{frame.schema}",
        pixel_format="BGRA8_STRAIGHT", coordinate_origin="top_left",
        width=frame.width, height=frame.height, qt_canvas_rasterized_on_cpu=True,
        production_export_changed=False, native_gpu_alpha_composition_required=len(layers)>1,
        layers=layers)
    if reuse_state is not None:
        descriptor["reuse_previous_frame"] = reused
        if row_deltas:
            descriptor["row_deltas"] = patches
    metadata = json.dumps(descriptor, separators=(",", ":"), allow_nan=False).encode()
    if len(metadata) > 65536:
        raise ValueError("Binary frame metadata exceeds limit")
    sent_size = sum(payloads)
    if reuse_state is not None:
        reuse_state.previous = next_digests
        reuse_state.previous_pixels = next_pixels if row_deltas else {}
        reuse_state.reused_planes += sum(reused)
        reuse_state.saved_bytes += sum(size for size, reuse in zip(sizes, reused) if reuse)
    yield HEADER.pack(b"MMSB", 3 if reuse_state is not None and row_deltas else
                      2 if reuse_state is not None else 1,
                      index, len(metadata), sent_size)
    yield metadata
    for layer, reuse, patch in zip(frame.layers, reused, patches):
        if reuse:
            continue
        if patch is None:
            yield memoryview(layer.pixels)
        else:
            stride = layer.width * 4
            yield memoryview(layer.pixels)[patch[0] * stride:patch[1] * stride]


def write_frame(stream, frame, index, *, reuse_state=None, row_deltas=False):
    count = 0
    for chunk in frame_chunks(frame, index, reuse_state=reuse_state,
                              row_deltas=row_deltas):
        view = memoryview(chunk)
        while view:
            written = stream.write(view)
            if written is None or written <= 0:
                raise OSError("Native frame channel made no write progress")
            count += written
            view = view[written:]
    stream.flush()
    return count
