"""Protocol units require no Qt, GPU or frame spool."""
import io
import json
from types import SimpleNamespace as NS
import unittest

from app.renderer.native_frame_transport import HEADER, FrameReuseState, frame_chunks, write_frame


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

    def test_v3_only_transmits_changed_rows_and_preserves_exact_pixels(self):
        width = height = 512
        stride = width * 4
        first = bytes((10, 30, 80, 255)) * (width * height)
        modified = bytearray(first)
        modified[120 * stride:128 * stride] = bytes((80, 30, 10, 255)) * (width * 8)
        cache = FrameReuseState()
        def packet(pixels):
            return NS(width=width, height=height, schema=1, layers=(
                NS(stream_key="base", width=width, height=height,
                   opacity=1.0, pixels=pixels),
            ))
        first_wire = b"".join(frame_chunks(packet(first), 0,
                                           reuse_state=cache, row_deltas=True))
        self.assertEqual(HEADER.unpack(first_wire[:28])[1], 3)
        second_wire = b"".join(frame_chunks(packet(bytes(modified)), 1,
                                            reuse_state=cache, row_deltas=True))
        _, version, pts, metadata_len, payload_len = HEADER.unpack(second_wire[:28])
        manifest = json.loads(second_wire[28:28+metadata_len])
        self.assertEqual((version, pts, payload_len), (3, 1, 8 * stride))
        self.assertEqual(manifest["row_deltas"], [[120, 128]])
        self.assertEqual(manifest["reuse_previous_frame"], [False])
        self.assertEqual(second_wire[28+metadata_len:], bytes(modified)[120*stride:128*stride])
        self.assertEqual(cache.row_delta_planes, 1)
        self.assertEqual(cache.row_delta_bytes_saved, len(first) - 8 * stride)
        self.assertEqual(cache.saved_bytes, 0)
        self.assertGreaterEqual(cache.row_delta_compare_seconds, 0)

        repeated = b"".join(frame_chunks(packet(bytes(modified)), 2,
                                        reuse_state=cache, row_deltas=True))
        _, _, _, meta, payload = HEADER.unpack(repeated[:28])
        data = json.loads(repeated[28:28+meta])
        self.assertEqual(payload, 0)
        self.assertEqual(data["reuse_previous_frame"], [True])
        self.assertEqual(data["row_deltas"], [None])

        # A full-scene change is cheaper to send as an ordinary plane.
        changed_all = bytes((40, 60, 90, 255)) * (width * height)
        wide = b"".join(frame_chunks(packet(changed_all), 3,
                                    reuse_state=cache, row_deltas=True))
        _, _, _, meta, payload = HEADER.unpack(wide[:28])
        self.assertEqual(payload, len(changed_all))
        self.assertEqual(json.loads(wide[28:28+meta])["row_deltas"], [None])

    def test_v3_changed_geometry_refuses_prior_row_patch(self):
        # Even if the byte sizes happen to match, a different shape must
        # transmit the complete plane rather than patching a prior frame.
        pixels = bytes((1, 2, 3, 255)) * (512 * 512)
        state = FrameReuseState()
        def pkt(width, height, data):
            return NS(width=width, height=height, schema=1, layers=(
                NS(stream_key="base", width=width, height=height,
                   opacity=1.0, pixels=data),
            ))
        _ = b"".join(frame_chunks(pkt(512, 512, pixels), 0,
                                   reuse_state=state, row_deltas=True))
        changed = bytes((2, 3, 4, 255)) * (1024 * 256)
        wire = b"".join(frame_chunks(pkt(1024, 256, changed), 1,
                                      reuse_state=state, row_deltas=True))
        _, _, _, meta, payload = HEADER.unpack(wire[:28])
        self.assertEqual(payload, len(changed))
        self.assertEqual(json.loads(wire[28:28+meta])["row_deltas"], [None])

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
