"""Bounded MMSB v1 transport; no frame files, one acknowledged frame in flight."""
import hashlib
import json
import struct

HEADER = struct.Struct("<4sIQIQ")
MAX_PAYLOAD = 3840 * 2160 * 4 * 8


def frame_chunks(frame, index):
    size = frame.width * frame.height * 4
    if (not 2 <= frame.width <= 3840 or not 2 <= frame.height <= 2160
            or frame.width % 2 or frame.height % 2
            or not 1 <= len(frame.layers) <= 8 or size * len(frame.layers) > MAX_PAYLOAD):
        raise ValueError("Invalid binary frame geometry or memory limit")
    layers = []
    for i, layer in enumerate(frame.layers):
        key = "base" if i == 0 else f"layer:{i-1}"
        if (layer.stream_key != key or len(layer.pixels) != size
                or not 0 <= layer.opacity <= 1 or (i == 0 and layer.opacity != 1)):
            raise ValueError("Invalid binary frame layer")
        layers.append(dict(file=f"layer-{i}.bgra", stream_key=key, z_order=i,
                           opacity=layer.opacity, bytes=size,
                           sha256=hashlib.sha256(layer.pixels).hexdigest()))
    metadata = json.dumps(dict(schema=1, format="mms_canvas_bgra_layers_v1",
        pixel_format="BGRA8_STRAIGHT", coordinate_origin="top_left",
        width=frame.width, height=frame.height, qt_canvas_rasterized_on_cpu=True,
        production_export_changed=False, native_gpu_alpha_composition_required=len(layers)>1,
        layers=layers), separators=(",", ":"), allow_nan=False).encode()
    if len(metadata) > 65536:
        raise ValueError("Binary frame metadata exceeds limit")
    yield HEADER.pack(b"MMSB", 1, index, len(metadata), size * len(layers))
    yield metadata
    for layer in frame.layers:
        yield memoryview(layer.pixels)


def write_frame(stream, frame, index):
    count = 0
    for chunk in frame_chunks(frame, index):
        view = memoryview(chunk)
        while view:
            written = stream.write(view)
            if written is None or written <= 0:
                raise OSError("Native frame channel made no write progress")
            count += written
            view = view[written:]
    stream.flush()
    return count
