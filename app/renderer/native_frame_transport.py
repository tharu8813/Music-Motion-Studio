"""Bounded MMSB native frames: v1 raw, v2 references, v3 adaptive LZ4 blocks.

All formats include an SHA-256 digest of the **uncompressed** BGRA pixels.
The receiver validates reconstructed bytes before D3D11 submission.
"""
import hashlib
import json
import struct
from itertools import chain
from time import monotonic
from statistics import median

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
SHARED_PACKET_CAPACITY = 32 * 1024 * 1024
SHARED_PACKET_LENGTH = struct.Struct("<I")


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


class AdaptiveCompressionBudget:
    """Choose the faster v3 wire encoding by measuring *actual* sender latency.

    The v3 protocol supports raw and LZ4 planes in the same stream. Probe
    alternating frames, then require a >=10% elapsed-time win to retain LZ4.
    Missing/unstable measurements fall back to raw BGRA without a GUI toggle.
    """

    PROBE_FRAMES = 64
    MIN_BYTES = 1 << 20

    def __init__(self):
        self.samples = {False: [], True: []}
        self.selected = None

    def use_compression(self, frame_index: int) -> bool:
        if self.selected is not None:
            return self.selected
        if frame_index < self.PROBE_FRAMES:
            return frame_index % 2 == 0
        raw = self.samples[False]
        packed = self.samples[True]
        self.selected = bool(
            len(raw) >= 8 and len(packed) >= 8
            and median(packed) < median(raw) * 0.90
        )
        return self.selected

    def record(self, compressed: bool, elapsed: float, raw_bytes: int) -> None:
        if self.selected is not None or raw_bytes < self.MIN_BYTES:
            return
        if elapsed >= 0:
            # Normalize for varying canvas band sizes and overlay counts.
            self.samples[compressed].append(elapsed / (raw_bytes / 1_000_000))

    @property
    def decision(self) -> str:
        return ("lz4" if self.selected else "raw") if self.selected is not None else "calibrating"


def _hash_pixels(pixels):
    started = monotonic()
    return hashlib.sha256(pixels).hexdigest(), monotonic() - started


def _encode_pixels(pixels):
    started = monotonic()
    original = memoryview(pixels)
    if _compressible(original):
        compressed = _lz4.compress(original, mode="fast", store_size=False)
        if len(compressed) < len(original) * 0.90:
            return compressed, "lz4", monotonic() - started
    return original, "raw", monotonic() - started


def frame_chunks(frame, index, *, reuse_state=None, timings=None,
                 compress_planes=False, v3_wire=False, executor=None):
    """Iterate one bounded packet with immutable pixels.

    An optional small shared executor runs CPU hashing/compression of separate
    BGRA layers concurrently. We preserve each layer's order and exact SHA
    checks, and never run Qt painting on these worker threads.
    """
    if compress_planes and (reuse_state is None or _lz4 is None):
        raise ValueError("Native LZ4 transport requires v2 reuse and installed lz4")
    if v3_wire and reuse_state is None:
        raise ValueError("Native v3 transport requires a previous-frame state")
    version = 3 if (v3_wire or compress_planes) else 2 if reuse_state is not None else 1
    sizes = [layer.width * layer.height * 4 for layer in frame.layers]
    total_size = sum(sizes)
    if (not 2 <= frame.width <= 3840 or not 2 <= frame.height <= 2160
            or frame.width % 2 or frame.height % 2
            or not 1 <= len(frame.layers) <= 8 or total_size > MAX_PAYLOAD):
        raise ValueError("Invalid binary frame geometry or memory limit")

    for i, layer in enumerate(frame.layers):
        size = sizes[i]
        key = "base" if i == 0 else f"layer:{i-1}"
        if (layer.stream_key != key or len(layer.pixels) != size
                or not 0 <= layer.opacity <= 1 or (i == 0 and layer.opacity != 1)):
            raise ValueError("Invalid binary frame layer")

    parallel = executor is not None and len(frame.layers) >= 2 and total_size >= 1 << 20
    started_hash = monotonic() if timings is not None else 0.0
    if parallel:
        digests = list(executor.map(_hash_pixels, (layer.pixels for layer in frame.layers)))
    else:
        digests = [_hash_pixels(layer.pixels) for layer in frame.layers]
    if timings is not None:
        _metric(timings, "sha256_wall_seconds", monotonic() - started_hash)

    prior = reuse_state.previous if reuse_state is not None else {}
    reused = [
        prior.get("base" if i == 0 else f"layer:{i-1}")
        == (layer.width, layer.height, digests[i][0])
        for i, layer in enumerate(frame.layers)
    ]
    next_digests = {
        "base" if i == 0 else f"layer:{i-1}":
        (layer.width, layer.height, digests[i][0])
        for i, layer in enumerate(frame.layers)
    }

    for i, (digest, elapsed) in enumerate(digests):
        _metric(timings, "sha256_seconds", elapsed)
        _metric(timings, "sha256_bytes", sizes[i])

    encoded = {}
    if compress_planes:
        candidates = [i for i, reuse in enumerate(reused) if not reuse]
        started_lz4 = monotonic() if timings is not None else 0.0
        if parallel and len(candidates) >= 2:
            results = list(executor.map(_encode_pixels,
                                        (frame.layers[i].pixels for i in candidates)))
        else:
            results = [_encode_pixels(frame.layers[i].pixels) for i in candidates]
        if timings is not None:
            _metric(timings, "lz4_wall_seconds", monotonic() - started_lz4)
        for i, (data, algorithm, elapsed) in zip(candidates, results):
            encoded[i] = (data, algorithm)
            _metric(timings, "lz4_cpu_seconds", elapsed)
            if timings is not None:
                # Diagnostic only: at most eight existing MMSB stream keys.
                # Thread-task durations may overlap, so never add these
                # values to obtain wall-clock export time.
                key = "base" if i == 0 else f"layer:{i-1}"
                by_layer = timings.setdefault("lz4_layer_cost", {})
                bucket = by_layer.setdefault(key, {
                    "attempted_planes": 0, "source_bytes": 0,
                    "wire_bytes": 0, "saved_bytes": 0,
                    "task_elapsed_seconds": 0.0, "compressed_planes": 0,
                })
                bucket["attempted_planes"] += 1
                bucket["source_bytes"] += sizes[i]
                bucket["wire_bytes"] += len(data)
                bucket["saved_bytes"] += sizes[i] - len(data)
                bucket["task_elapsed_seconds"] += elapsed
                bucket["compressed_planes"] += int(algorithm == "lz4")
            if algorithm == "lz4":
                _metric(timings, "lz4_planes", 1)

    layers = []
    payloads = []
    wire_size = 0
    for i, layer in enumerate(frame.layers):
        size = sizes[i]
        key = "base" if i == 0 else f"layer:{i-1}"
        layer_wire_bytes = 0
        layer_lz4 = False
        info = dict(file=f"layer-{i}.bgra", stream_key=key, z_order=i,
                    opacity=layer.opacity, bytes=size, sha256=digests[i][0])
        if frame.schema == 2:
            info.update(width=layer.width, height=layer.height)
            if layer.inverse_transform is not None:
                info["inverse_transform"] = layer.inverse_transform
            if layer.clip is not None:
                info["clip"] = layer.clip
        if not reused[i]:
            data, algorithm = encoded.get(i, (memoryview(layer.pixels), "raw"))
            layer_wire_bytes = len(data)
            layer_lz4 = algorithm == "lz4"
            if version == 3:
                info["compression"] = algorithm
                info["wire_bytes"] = layer_wire_bytes
            payloads.append(data)
            wire_size += layer_wire_bytes
            _metric(timings, "raw_source_bytes", size)
            _metric(timings, "wire_payload_bytes", layer_wire_bytes)
        elif version == 3:
            info["compression"] = "previous"
            info["wire_bytes"] = 0
        if timings is not None:
            # Diagnostics only: bounded by the protocol's 8 stream keys.
            # No per-frame payload/metadata change, no retained BGRA bytes.
            by_stream = timings.setdefault("layer_transport", {})
            bucket = by_stream.setdefault(key, {
                "planes": 0, "source_bytes": 0, "wire_bytes": 0,
                "reused_planes": 0, "lz4_planes": 0,
            })
            bucket["planes"] += 1
            bucket["source_bytes"] += size
            bucket["wire_bytes"] += layer_wire_bytes
            bucket["reused_planes"] += int(reused[i])
            bucket["lz4_planes"] += int(layer_lz4)
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
        reuse_state.saved_bytes += sum(size for size, reused_flag in zip(sizes, reused)
                                       if reused_flag)
    yield HEADER.pack(b"MMSB", version, index, len(metadata), wire_size)
    yield metadata
    yield from payloads

def write_frame(stream, frame, index, *, reuse_state=None, timings=None,
                compress_planes=False, v3_wire=False, executor=None, shared_packet=None):
    chunks = frame_chunks(frame, index, reuse_state=reuse_state, timings=timings,
                          compress_planes=compress_planes, v3_wire=v3_wire,
                          executor=executor)
    # A frame always starts with the fixed 28-byte header followed by its
    # bounded JSON manifest. One write for both eliminates a tiny pipe write
    # per frame without touching layer payloads or the MMSB wire format.
    header = next(chunks)
    preamble = header + next(chunks)
    chunks = chain((preamble,), chunks)
    if shared_packet is not None:
        packet_bytes = len(preamble) + HEADER.unpack(header)[-1]
        if packet_bytes <= len(shared_packet):
            shared_packet.seek(0)
            started = monotonic()
            for chunk in chunks:
                shared_packet.write(chunk)
            _metric(timings, "shared_packet_write_seconds", monotonic() - started)
            _metric(timings, "shared_packet_bytes", packet_bytes)
            _metric(timings, "shared_packet_frames", 1)
            # The sender waits for this frame's ACK before touching the mapping again.
            chunks = (SHARED_PACKET_LENGTH.pack(packet_bytes),)
        else:
            _metric(timings, "shared_packet_fallback_frames", 1)
            chunks = chain((SHARED_PACKET_LENGTH.pack(0),), chunks)
    else:
        packet_bytes = None
    count = 0
    for chunk in chunks:
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
    return count if packet_bytes is None else packet_bytes
