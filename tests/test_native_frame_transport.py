"""Protocol units require no Qt, GPU or frame spool."""
import io
import json
from types import SimpleNamespace as NS
import unittest

from app.renderer.native_frame_transport import HEADER, frame_chunks, write_frame


def frame():
    return NS(width=2, height=2, layers=(NS(stream_key="base", opacity=1.0,
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
