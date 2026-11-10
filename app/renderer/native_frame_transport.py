"""Bounded MMSB v1 transport; no frame files, one acknowledged frame in flight."""
import hashlib
import json
import struct

HEADER = struct.Struct("<4sIQIQ")
MAX_PAYLOAD = 3840 * 2160 * 4 * 8


def frame_chunks(frame, index):
    sizes = [layer.width * layer.height * 4 for layer in frame.layers]
    total_size = sum(sizes)
    if (not 2 <= frame.width <= 3840 or not 2 <= frame.height <= 2160
            or frame.width % 2 or frame.height % 2
            or not 1 <= len(frame.layers) <= 8 or total_size > MAX_PAYLOAD):
        raise ValueError("Invalid binary frame geometry or memory limit")
    layers = []
    for i, layer in enumerate(frame.layers):
        size = sizes[i]
        key = "base" if i == 0 else f"layer:{i-1}"
        if (layer.stream_key != key or len(layer.pixels) != size
                or not 0 <= layer.opacity <= 1 or (i == 0 and layer.opacity != 1)):
            raise ValueError("Invalid binary frame layer")
        layers.append(dict(file=f"layer-{i}.bgra", stream_key=key, z_order=i,
                           opacity=layer.opacity, bytes=size,
                           sha256=hashlib.sha256(layer.pixels).hexdigest()))
        if frame.schema == 2:
            layers[-1].update(width=layer.width, height=layer.height)
            if layer.inverse_transform is not None:
                layers[-1]["inverse_transform"] = layer.inverse_transform
            if layer.clip is not None:
                layers[-1]["clip"] = layer.clip
    metadata = json.dumps(dict(schema=frame.schema, format=f"mms_canvas_bgra_layers_v{frame.schema}",
        pixel_format="BGRA8_STRAIGHT", coordinate_origin="top_left",
        width=frame.width, height=frame.height, qt_canvas_rasterized_on_cpu=True,
        production_export_changed=False, native_gpu_alpha_composition_required=len(layers)>1,
        layers=layers), separators=(",", ":"), allow_nan=False).encode()
    if len(metadata) > 65536:
        raise ValueError("Binary frame metadata exceeds limit")
    yield HEADER.pack(b"MMSB", 1, index, len(metadata), total_size)
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
