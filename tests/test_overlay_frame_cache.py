"""Cache state transitions without a player, decoder or QWidget."""

import unittest

from app.preview.overlay_frame_cache import OverlayFrameCache


class OverlayFrameCacheTests(unittest.TestCase):
    def test_exact_fallback_and_signatures_do_not_cross_tracks_or_effect_sets(self):
        cache = OverlayFrameCache()
        first, second, future = (object(),), (object(),), (object(),)
        cache.store("a", (0,), [(4, first), (9, future)])
        cache.store("a", (1,), [(4, second)])
        self.assertEqual(cache.layers_for_frame("a", 5, (0,)), first)
        self.assertEqual(cache.layers_for_frame("a", 4, (1,)), second)
        self.assertEqual(cache.layers_for_frame("b", 5, (0,)), ())
        self.assertEqual(cache.layers_for_frame("a", 5, (2,)), ())
        self.assertEqual(cache.layers_for_frame("a", 3, (0,)), ())
        self.assertEqual(cache.layers_for_frame("a", 9, (0,)), future)
        self.assertEqual(cache.layers_for_frame("a", 10, (0,)), future)
        # A seek clears the displayed fallback before asking for earlier time.
        cache.clear("a")
        self.assertEqual(cache.layers_for_frame("a", 5, (0,)), ())

    def test_trim_keeps_only_a_short_window_of_the_drawn_track(self):
        cache = OverlayFrameCache()
        cache.store("a", (0,), [(index, (object(),)) for index in range(100)])
        cache.store("b", (0,), [(50, (object(),))])
        cache.trim("a", 50, 6)
        self.assertEqual(cache.first_missing("a", (0,), 46, 1), 46)
        self.assertIsNone(cache.first_missing("a", (0,), 47, 22))
        self.assertEqual(cache.first_missing("a", (0,), 69, 1), 69)
        self.assertEqual(cache.first_missing("b", (0,), 50, 1), 50)

    def test_clear_one_track_preserves_other_track_and_changes_revision(self):
        cache = OverlayFrameCache()
        layers = (object(),)
        cache.store("a", (0,), [(4, layers)])
        cache.store("b", (0,), [(5, layers)])
        self.assertEqual(list(cache.images()), [layers[0], layers[0]])
        self.assertEqual(cache.layers_for_frame("b", 5, (0,)), layers)
        revision = cache.revision
        cache.clear("a")
        self.assertGreater(cache.revision, revision)
        self.assertEqual(cache.first_missing("a", (0,), 4, 1), 4)
        self.assertEqual(cache.layers_for_frame("b", 6, (0,)), layers)
        cache.clear()
        self.assertEqual(cache.layers_for_frame("b", 6, (0,)), ())
        self.assertEqual(list(cache.images()), [])


if __name__ == "__main__":
    unittest.main()
