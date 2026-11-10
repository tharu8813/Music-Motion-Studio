"""Bounded MMSB native frames: v1 raw, v2 references, v3 adaptive LZ4 blocks.

All formats include an SHA-256 digest of the **uncompressed** BGRA pixels.
The receiver validates reconstructed bytes before D3D11 submission.
"""
import hashlib
import json
import struct
from time import monotonic

try:
    from lz4 import block as _lz4
except ImportError:  # Installed releases pin lz4; older source environments use v2.
    _lz4 = None


class FrameReuseState:
    """Sender-local previous frame identities; never holds QImage pixels."""

    def __init__(self):
        self.previous = {}
        self.reused_planes = 0
        self.saved_bytes = 0


HEADER = struct.Struct("<4sIQIQ")
MAX_PAYLOAD = 3840 * 2160 * 4 * 8


def supports_lz4_transport() -> bool:
    return _lz4 is not None


def _metric(stats, key, amount):
    if stats is not None:
        stats[key] = stats.get(key, 0) + amount


def _compressible(raw: memoryview) -> bool:
    """Check four small distributed regions before paying to compress MBs."""
    if len(raw) < 128 * 1024:
        return False
    block = 16384
    last = len(raw) - block
    samples = (0, last // 3, (2 * last) // 3, last)
    size = sum(len(_lz4.compress(raw[pos:pos + block],
                                  mode="fast", store_size=False)) for pos in samples)
    return size <= int(block * len(samples) * 0.82)


def frame_chunks(frame, index, *, reuse_state=None, timings=None, compress_planes=False):
    if compress_planes and (reuse_state is None or _lz4 is None):
        raise ValueError("Native LZ4 transport requires v2 reuse and installed lz4")
    version = 3 if compress_planes else 2 if reuse_state is not None else 1
    sizes = [layer.width * layer.height * 4 for layer in frame.layers]
    total_size = sum(sizes)
    if (not 2 <= frame.width <= 3840 or not 2 <= frame.height <= 2160
            or frame.width % 2 or frame.height % 2
            or not 1 <= len(frame.layers) <= 8 or total_size > MAX_PAYLOAD):
        raise ValueError("Invalid binary frame geometry or memory limit")
    layers = []
    reused = []
    next_digests = {}
    payloads = []
    wire_size = 0
    for i, layer in enumerate(frame.layers):
        size = sizes[i]
        key = "base" if i == 0 else f"layer:{i-1}"
        if (layer.stream_key != key or len(layer.pixels) != size
                or not 0 <= layer.opacity <= 1 or (i == 0 and layer.opacity != 1)):
            raise ValueError("Invalid binary frame layer")
        hash_started = monotonic() if timings is not None else 0.0
        digest = hashlib.sha256(layer.pixels).hexdigest()
        if timings is not None:
            _metric(timings, "sha256_seconds", monotonic() - hash_started)
            _metric(timings, "sha256_bytes", size)
        previous = reuse_state.previous.get(key) if reuse_state is not None else None
        reuse = previous == (layer.width, layer.height, digest)
        reused.append(reuse)
        next_digests[key] = (layer.width, layer.height, digest)
        info = dict(file=f"layer-{i}.bgra", stream_key=key, z_order=i,
                    opacity=layer.opacity, bytes=size, sha256=digest)
        if frame.schema == 2:
            info.update(width=layer.width, height=layer.height)
            if layer.inverse_transform is not None:
                info["inverse_transform"] = layer.inverse_transform
            if layer.clip is not None:
                info["clip"] = layer.clip
        if not reuse:
            encoded = memoryview(layer.pixels)
            encoding = "raw"
            if compress_planes:
                began = monotonic() if timings is not None else 0.0
                if _compressible(encoded):
                    compressed = _lz4.compress(encoded, mode="fast", store_size=False)
                    # Keep uncompressible pixel data on the old raw transport.
                    if len(compressed) < size * 0.90:
                        encoded = compressed
                        encoding = "lz4"
                        _metric(timings, "lz4_planes", 1)
                if timings is not None:
                    _metric(timings, "lz4_cpu_seconds", monotonic() - began)
            if compress_planes:
                info["compression"] = encoding
                info["wire_bytes"] = len(encoded)
            payloads.append(encoded)
            wire_size += len(encoded)
            _metric(timings, "raw_source_bytes", size)
            _metric(timings, "wire_payload_bytes", len(encoded))
        elif compress_planes:
            info["compression"] = "previous"
            info["wire_bytes"] = 0
        layers.append(info)
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
    if reuse_state is not None:
        reuse_state.previous = next_digests
        reuse_state.reused_planes += sum(reused)
        reuse_state.saved_bytes += total_size - sum(
            size for size, reused_flag in zip(sizes, reused) if not reused_flag
        )
    yield HEADER.pack(b"MMSB", version, index, len(metadata), wire_size)
    yield metadata
    yield from payloads


def write_frame(stream, frame, index, *, reuse_state=None, timings=None, compress_planes=False):
    count = 0
    for chunk in frame_chunks(frame, index, reuse_state=reuse_state, timings=timings,
                              compress_planes=compress_planes):
        view = memoryview(chunk)
        while view:
            write_started = monotonic() if timings is not None else 0.0
            written = stream.write(view)
            if timings is not None:
                _metric(timings, "pipe_syswrite_seconds", monotonic() - write_started)
                _metric(timings, "pipe_syswrite_calls", 1)
            if written is None or written <= 0:
                raise OSError("Native frame channel made no write progress")
            count += written
            view = view[written:]
    stream.flush()
    return count
