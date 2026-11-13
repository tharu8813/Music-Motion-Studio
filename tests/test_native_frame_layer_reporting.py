"""Native per-stream diagnostics must describe real output order without private paths."""
from __future__ import annotations

from types import SimpleNamespace as NS
import unittest

from app.renderer.native_project_frames import describe_native_frame_layers


class NativeFrameLayerReportingTests(unittest.TestCase):
    def test_mixed_static_and_dynamic_source_types_follow_packet_order(self):
        def item(type_name, private_name):
            return NS(source=NS(source_type=NS(value=type_name), name=private_name))
        stack = NS(
            order=(("canvas", 0), ("dynamic", 1), ("canvas", 1),
                   ("dynamic", 0)),
            source_ids=("particle-id", "visualizer-id"),
        )
        bands = (
            (item("background", "personal-background-name"),
             item("text", "private lyrics")),
            (item("lyrics", "my-lyrics"),),
        )
        sources = [
            NS(id="particle-id", source_type=NS(value="particle_overlay"),
               file_path="C:/private/path"),
            NS(id="visualizer-id", source_type=NS(value="audio_visualizer"),
               file_path="C:/another/private"),
        ]
        results = describe_native_frame_layers(stack, bands, sources)
        self.assertEqual(list(results), ["base", "layer:0", "layer:1", "layer:2"])
        self.assertEqual(results["base"], {
            "kind": "qt_canvas", "source_count": 2,
            "source_types": ["background", "text"],
        })
        self.assertEqual(results["layer:0"]["source_types"], ["audio_visualizer"])
        self.assertEqual(results["layer:1"]["source_types"], ["lyrics"])
        self.assertEqual(results["layer:2"]["source_types"], ["particle_overlay"])
        text = str(results)
        for secret in ("personal-background-name", "private lyrics", "my-lyrics",
                       "C:/private", "particle-id", "visualizer-id"):
            self.assertNotIn(secret, text)

    def test_unknown_input_kind_fails_instead_of_mislabelling_source(self):
        stack = NS(order=(("not-a-real-kind", 0),), source_ids=())
        with self.assertRaisesRegex(ValueError, "Unknown native layer"):
            describe_native_frame_layers(stack, (), ())


if __name__ == "__main__":
    unittest.main()
