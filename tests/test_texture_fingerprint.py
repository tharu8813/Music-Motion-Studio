"""Preview texture signatures avoid extra copies of per-frame pixel buffers."""

import unittest
from hashlib import blake2b
from unittest.mock import patch

from app.preview.texture_fingerprint import image_content_signature


class FakeImage:
    def __init__(self) -> None:
        self.pixels = memoryview(bytearray(range(32)))

    def sizeInBytes(self) -> int:
        return len(self.pixels)

    def format(self) -> int:
        return 4

    def width(self) -> int:
        return 4

    def height(self) -> int:
        return 2

    def bytesPerLine(self) -> int:
        return 16

    def constBits(self):
        return self.pixels


class TextureFingerprintTests(unittest.TestCase):
    def test_hashes_qimage_buffer_without_copying_it_to_bytes(self):
        image = FakeImage()
        with patch("app.preview.texture_fingerprint.blake2b", wraps=blake2b) as digest:
            signature = image_content_signature(image)
        self.assertIs(digest.call_args.args[0], image.pixels)
        self.assertEqual(signature, (
            4, 4, 2, 16, blake2b(image.pixels, digest_size=16).digest(),
        ))

    def test_too_large_or_unreadable_buffer_falls_back_to_texture_upload(self):
        image = FakeImage()
        self.assertIsNone(image_content_signature(image, max_bytes=8))
        with patch.object(image, "constBits", side_effect=TypeError("not exposed")):
            self.assertIsNone(image_content_signature(image))


if __name__ == "__main__":
    unittest.main()
