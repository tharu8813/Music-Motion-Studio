"""Bounded MMSB transport: v1 raw planes; opt-in v2 prior-frame plane references."""
import hashlib
import json
import struct


class FrameReuseState:
    """Sender-local, bounded previous-frame digests (never retains Qt pixels)."""

    def __init__(self):
        self.previous = {}
        self.reused_planes = 0
        self.saved_bytes = 0


HEADER = struct.Struct("<4sIQIQ")
MAX_PAYLOAD = 3840 * 2160 * 4 * 8


def frame_chunks(frame, index, *, reuse_state=None):
    sizes = [layer.width * layer.height * 4 for layer in frame.layers]
    total_size = sum(sizes)
    if (not 2 <= frame.width <= 3840 or not 2 <= frame.height <= 2160
            or frame.width % 2 or frame.height % 2
            or not 1 <= len(frame.layers) <= 8 or total_size > MAX_PAYLOAD):
        raise ValueError("Invalid binary frame geometry or memory limit")
    layers = []
    reused = []
    next_digests = {}
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
    metadata = json.dumps(descriptor, separators=(",", ":"), allow_nan=False).encode()
    if len(metadata) > 65536:
        raise ValueError("Binary frame metadata exceeds limit")
    sent_size = sum(size for size, reuse in zip(sizes, reused) if not reuse)
    if reuse_state is not None:
        reuse_state.previous = next_digests
        reuse_state.reused_planes += sum(reused)
        reuse_state.saved_bytes += total_size - sent_size
    yield HEADER.pack(b"MMSB", 2 if reuse_state is not None else 1,
                      index, len(metadata), sent_size)
    yield metadata
    for layer, reuse in zip(frame.layers, reused):
        if not reuse:
            yield memoryview(layer.pixels)


def write_frame(stream, frame, index, *, reuse_state=None):
    count = 0
    for chunk in frame_chunks(frame, index, reuse_state=reuse_state):
        view = memoryview(chunk)
        while view:
            written = stream.write(view)
            if written is None or written <= 0:
                raise OSError("Native frame channel made no write progress")
            count += written
            view = view[written:]
    stream.flush()
    return count
