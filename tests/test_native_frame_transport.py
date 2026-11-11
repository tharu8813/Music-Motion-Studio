"""Protocol units require no Qt, GPU or frame spool."""
import io
import json
from types import SimpleNamespace as NS
import unittest

from app.renderer.native_frame_transport import (
    HEADER, AdaptiveCompressionBudget, FrameReuseState, frame_chunks, write_frame,
)


def frame():
    return NS(width=2, height=2, schema=1, layers=(NS(stream_key="base", width=2, height=2, opacity=1.0,
                                         pixels=bytes([1, 2, 3, 255])*4),))


class TransportTests(unittest.TestCase):
    def test_partial_writes_and_wire_layout(self):
        class Partial(io.BytesIO):
            def write(self, data):
                return super().write(data[:3])
        stream = Partial()
        count = write_frame(stream, frame(), 42)
        raw = stream.getvalue()
        self.assertEqual(count, len(raw))
        magic, version, index, meta, payload = HEADER.unpack(raw[:28])
        self.assertEqual((magic, version, index, payload), (b"MMSB", 1, 42, 16))
        self.assertEqual(json.loads(raw[28:28+meta])["layers"][0]["bytes"], 16)
        self.assertEqual(raw[28+meta:], frame().layers[0].pixels)

    def test_timing_measurement_preserves_exact_wire_bytes(self):
        baseline = io.BytesIO()
        measured = io.BytesIO()
        measurements = {}
        self.assertEqual(write_frame(baseline, frame(), 0),
                         write_frame(measured, frame(), 0, timings=measurements))
        self.assertEqual(baseline.getvalue(), measured.getvalue())
        self.assertEqual(measurements["sha256_bytes"], 16)
        self.assertGreaterEqual(measurements["sha256_seconds"], 0)
        self.assertGreater(measurements["pipe_syswrite_calls"], 0)
        self.assertGreaterEqual(measurements["pipe_syswrite_seconds"], 0)

    def test_per_layer_wire_accounting_keeps_packets_identical(self):
        def make_frame(top):
            base = NS(
                stream_key="base", width=2, height=2, opacity=1.0,
                pixels=bytes((5, 8, 11, 255)) * 4,
            )
            overlay = NS(
                stream_key="layer:0", width=2, height=2, opacity=0.5,
                pixels=bytes((top, 2, 3, 128)) * 4,
            )
            return NS(width=2, height=2, schema=1, layers=(base, overlay))

        samples = (make_frame(20), make_frame(21), make_frame(21))
        expected_reuse = FrameReuseState()
        counted_reuse = FrameReuseState()
        stats = {}
        expected = [
            b"".join(frame_chunks(sample, index, reuse_state=expected_reuse))
            for index, sample in enumerate(samples)
        ]
        actual = [
            b"".join(frame_chunks(sample, index, reuse_state=counted_reuse, timings=stats))
            for index, sample in enumerate(samples)
        ]
        self.assertEqual(actual, expected)
        self.assertEqual(stats["layer_transport"], {
            "base": {
                "planes": 3, "source_bytes": 48, "wire_bytes": 16,
                "reused_planes": 2, "lz4_planes": 0,
            },
            "layer:0": {
                "planes": 3, "source_bytes": 48, "wire_bytes": 32,
                "reused_planes": 1, "lz4_planes": 0,
            },
        })
        self.assertEqual(
            sum(layer["wire_bytes"] for layer in stats["layer_transport"].values()),
            stats["wire_payload_bytes"],
        )
        self.assertEqual(
            sum(layer["reused_planes"] for layer in stats["layer_transport"].values()),
            counted_reuse.reused_planes,
        )

    def test_v2_previous_frame_references_skip_wire_payload_without_freezing_state(self):
        cache = FrameReuseState()
        first = frame()
        first_bytes = b"".join(frame_chunks(first, 0, reuse_state=cache))
        magic, version, pts, meta, payload = HEADER.unpack(first_bytes[:28])
        self.assertEqual((magic, version, pts, payload), (b"MMSB", 2, 0, 16))
        data = json.loads(first_bytes[28:28+meta])
        self.assertEqual(data["reuse_previous_frame"], [False])

        repeated = frame()
        second = b"".join(frame_chunks(repeated, 1, reuse_state=cache))
        magic, version, pts, meta, payload = HEADER.unpack(second[:28])
        self.assertEqual((magic, version, pts, payload), (b"MMSB", 2, 1, 0))
        data = json.loads(second[28:28+meta])
        self.assertEqual(data["reuse_previous_frame"], [True])
        self.assertEqual(second[28+meta:], b"")
        self.assertEqual(cache.reused_planes, 1)
        self.assertEqual(cache.saved_bytes, 16)

        changed = frame()
        changed.layers[0].pixels = bytes([1, 2, 4, 255]) * 4
        third = b"".join(frame_chunks(changed, 2, reuse_state=cache))
        self.assertEqual(HEADER.unpack(third[:28])[-1], 16)
        metadata_len = HEADER.unpack(third[:28])[3]
        self.assertEqual(json.loads(third[28:28+metadata_len])["reuse_previous_frame"], [False])
        self.assertEqual(third[28+metadata_len:], changed.layers[0].pixels)
        self.assertEqual(cache.reused_planes, 1)

    def test_v2_reuses_only_matching_stream_dimensions_and_digest(self):
        cache = FrameReuseState()
        def layer(key, w, h, pixels):
            return NS(
                stream_key=key, width=w, height=h, pixels=pixels,
                opacity=1.0, inverse_transform=None, clip=None,
            )
        base = layer("base", 4, 4, bytes((2, 4, 6, 255)) * 16)
        top = layer("layer:0", 2, 2, bytes((100, 80, 40, 128)) * 4)
        first = NS(width=4, height=4, schema=2, layers=(base, top))
        _ = b"".join(frame_chunks(first, 0, reuse_state=cache))
        modified = NS(
            width=4, height=4, schema=2,
            layers=(
                layer("base", 4, 4, base.pixels),
                layer("layer:0", 2, 2, top.pixels),
            ),
        )
        modified.layers[1].opacity = 0.25
        modified.layers[1].inverse_transform = (1, 0, 0, 1, -1, -1)
        modified.layers[1].clip = (1, 1, 3, 3)
        wire = b"".join(frame_chunks(modified, 1, reuse_state=cache))
        _, version, _, count, size = HEADER.unpack(wire[:28])
        self.assertEqual((version, size), (2, 0))
        metadata = json.loads(wire[28:28+count])
        self.assertEqual(metadata["reuse_previous_frame"], [True, True])
        self.assertEqual(metadata["layers"][1]["opacity"], 0.25)
        self.assertEqual(metadata["layers"][1]["clip"], [1, 1, 3, 3])
        # Equal-size bytes arranged into different geometry are not safe refs.
        reshaped = NS(
            width=4, height=4, schema=2,
            layers=(base, layer("layer:0", 1, 4, top.pixels)),
        )
        wire = b"".join(frame_chunks(reshaped, 2, reuse_state=cache))
        _, _, _, count, size = HEADER.unpack(wire[:28])
        self.assertEqual(size, 16)
        self.assertEqual(json.loads(wire[28:28+count])["reuse_previous_frame"], [True, False])

    def test_v3_adaptive_lz4_compresses_losslessly_and_skips_reused_planes(self):
        from app.renderer.native_frame_transport import supports_lz4_transport
        if not supports_lz4_transport():
            self.skipTest("lz4 wheel not available")
        from lz4 import block
        import hashlib

        def make_frame():
            width, height = 320, 180
            pixels = bytes((0, 0, 0, 255)) * (width * height)
            return NS(width=width, height=height, schema=1, layers=(
                NS(stream_key="base", width=width, height=height, opacity=1.0, pixels=pixels),
            ))
        cache = FrameReuseState()
        stats = {}
        first = b"".join(frame_chunks(make_frame(), 0, reuse_state=cache,
                                       timings=stats, compress_planes=True))
        magic, version, index, mlen, wire_len = HEADER.unpack(first[:28])
        self.assertEqual((magic, version, index), (b"MMSB", 3, 0))
        metadata = json.loads(first[28:28 + mlen])
        layer = metadata["layers"][0]
        self.assertEqual(layer["compression"], "lz4")
        self.assertEqual(layer["wire_bytes"], wire_len)
        self.assertLess(wire_len, len(make_frame().layers[0].pixels) // 5)
        decoded = block.decompress(first[28 + mlen:], uncompressed_size=layer["bytes"])
        self.assertEqual(decoded, make_frame().layers[0].pixels)
        self.assertEqual(hashlib.sha256(decoded).hexdigest(), layer["sha256"])
        self.assertEqual(stats["lz4_planes"], 1)
        self.assertGreater(stats["raw_source_bytes"], stats["wire_payload_bytes"])

        second = b"".join(frame_chunks(make_frame(), 1, reuse_state=cache,
                                        compress_planes=True))
        _, version, _, mlen, wire_len = HEADER.unpack(second[:28])
        self.assertEqual((version, wire_len), (3, 0))
        entry = json.loads(second[28:28 + mlen])["layers"][0]
        self.assertEqual((entry["compression"], entry["wire_bytes"]), ("previous", 0))
        self.assertEqual(cache.reused_planes, 1)

    def test_v3_raw_fallback_keeps_noise_identical(self):
        from app.renderer.native_frame_transport import supports_lz4_transport
        if not supports_lz4_transport():
            self.skipTest("lz4 wheel not available")
        import random
        width, height = 320, 180
        pixels = bytearray(random.Random(42).randbytes(width * height * 4))
        pixels[3::4] = bytes([255]) * (width * height)
        value = NS(width=width, height=height, schema=1, layers=(
            NS(stream_key="base", width=width, height=height, opacity=1.0,
               pixels=bytes(pixels)),
        ))
        wire = b"".join(frame_chunks(value, 0, reuse_state=FrameReuseState(),
                                      compress_planes=True))
        _, version, _, mlen, payload = HEADER.unpack(wire[:28])
        layer = json.loads(wire[28:28+mlen])["layers"][0]
        self.assertEqual(version, 3)
        self.assertEqual(layer["compression"], "raw")
        self.assertEqual(layer["wire_bytes"], payload)
        self.assertEqual(wire[28+mlen:], bytes(pixels))

    def test_parallel_hash_and_lz4_keep_identical_wire_bytes_and_order(self):
        from concurrent.futures import ThreadPoolExecutor
        from app.renderer.native_frame_transport import supports_lz4_transport
        if not supports_lz4_transport():
            self.skipTest("lz4 wheel not available")

        width, height = 640, 360
        def make_layer(key, w, h, pixel):
            return NS(
                stream_key=key, width=w, height=h, opacity=1.0,
                pixels=pixel * (w*h), inverse_transform=None, clip=None,
            )
        source = NS(width=width, height=height, schema=2, layers=(
            make_layer("base", width, height, b"\x14\x54\x7f\xff"),
            make_layer("layer:0", 320, 180, b"\x00\x00\x00\x80"),
            make_layer("layer:1", 320, 180, b"\x00\x20\x00\x40"),
        ))
        for compressed in (False, True):
            serial = b"".join(frame_chunks(
                source, 0, reuse_state=FrameReuseState(),
                v3_wire=True, compress_planes=compressed,
            ))
            with ThreadPoolExecutor(max_workers=3) as pool:
                parallel = b"".join(frame_chunks(
                    source, 0, reuse_state=FrameReuseState(),
                    v3_wire=True, compress_planes=compressed, executor=pool,
                ))
            self.assertEqual(serial, parallel)
            _, version, _, mlen, payload = HEADER.unpack(parallel[:28])
            self.assertEqual(version, 3)
            entries = json.loads(parallel[28:28+mlen])["layers"]
            self.assertEqual(len(entries), 3)
            self.assertEqual(sum(item["wire_bytes"] for item in entries), payload)
            if not compressed:
                self.assertEqual([item["compression"] for item in entries],
                                 ["raw"] * 3)

    def test_v3_raw_frame_can_follow_lz4_and_reuse_previous(self):
        from app.renderer.native_frame_transport import supports_lz4_transport
        if not supports_lz4_transport():
            self.skipTest("lz4 wheel not available")
        def changed(byte):
            source = frame()
            source.layers[0].pixels = bytes((byte, 2, 3, 255)) * 4
            return source
        cache = FrameReuseState()
        compressed = b"".join(frame_chunks(
            changed(1), 0, reuse_state=cache, compress_planes=True,
        ))
        raw = b"".join(frame_chunks(
            changed(2), 1, reuse_state=cache, v3_wire=True, compress_planes=False,
        ))
        _, v0, _, _, _ = HEADER.unpack(compressed[:28])
        _, v1, _, length, payload = HEADER.unpack(raw[:28])
        self.assertEqual((v0, v1, payload), (3, 3, 16))
        self.assertEqual(json.loads(raw[28:28+length])["layers"][0]["compression"], "raw")
        self.assertEqual(raw[28+length:], changed(2).layers[0].pixels)
        repeated = b"".join(frame_chunks(
            changed(2), 2, reuse_state=cache, v3_wire=True,
        ))
        _, version, _, length, payload = HEADER.unpack(repeated[:28])
        self.assertEqual((version, payload), (3, 0))
        self.assertEqual(json.loads(repeated[28:28+length])["layers"][0]["compression"],
                         "previous")

    def test_auto_compression_calibration_chooses_actual_fastest_wire(self):
        for compressed_elapsed, expected in ((0.035, False), (0.004, True)):
            probe = AdaptiveCompressionBudget()
            for i in range(probe.PROBE_FRAMES):
                choice = probe.use_compression(i)
                self.assertEqual(choice, i % 2 == 0)
                probe.record(choice, compressed_elapsed if choice else 0.010, 4_000_000)
            self.assertEqual(probe.use_compression(probe.PROBE_FRAMES), expected)
            self.assertEqual(probe.decision, "lz4" if expected else "raw")
            self.assertEqual(len(probe.samples[True]), 32)
            self.assertEqual(len(probe.samples[False]), 32)
        empty = AdaptiveCompressionBudget()
        empty.record(True, 0.001, 16)
        self.assertFalse(empty.use_compression(empty.PROBE_FRAMES))
        self.assertEqual(empty.decision, "raw")

    def test_v3_requires_bundled_lz4_and_reuse(self):
        with self.assertRaises(ValueError):
            list(frame_chunks(frame(), 0, compress_planes=True))

    def test_no_progress_and_invalid_layers_fail(self):
        class Blocked:
            def write(self, data): return 0
        with self.assertRaises(OSError): write_frame(Blocked(), frame(), 0)
        for change in (dict(width=3842), dict(layers=()), dict(layers=frame().layers*9)):
            value = frame()
            value.__dict__.update(change)
            with self.assertRaises(ValueError): list(frame_chunks(value, 0))
        value = frame()
        value.layers[0].opacity = float("nan")
        with self.assertRaises(ValueError): list(frame_chunks(value, 0))
