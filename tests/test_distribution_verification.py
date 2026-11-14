"""The release gate must reject builds missing new runtime assets."""

from contextlib import redirect_stdout
import hashlib
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import verify_distribution


class DistributionVerificationTests(unittest.TestCase):
    @staticmethod
    def _write_manifest(distribution: Path) -> None:
        records = {
            p.relative_to(distribution).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in distribution.rglob("*")
            if p.is_file() and p.name != "integrity.json"
        }
        (distribution / "integrity.json").write_text(
            json.dumps({"files": records}), encoding="utf-8",
        )

    def test_complete_build_passes_and_missing_yamnet_or_help_fails(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            distribution = Path(directory)
            required = set(verify_distribution.REQUIRED_FILES)
            required.update(path.relative_to(root) for path in (root / "app/resources/help").glob("*/*.png"))
            required.update((Path("numpy/_core/_multiarray_umath.test.pyd"),
                             Path("onnxruntime/capi/onnxruntime_pybind11_state.test.pyd"),
                             Path("ctranslate2/_ext.test.pyd"), Path("sentencepiece/_sentencepiece.test.pyd"),
                             Path("music_motion_native.test.pyd")))
            (distribution / "Music Motion Studio.exe").touch()
            for path in required:
                target = distribution / "_internal" / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()
            self._write_manifest(distribution)
            with patch("sys.argv", ["verify_distribution.py", str(distribution)]), redirect_stdout(StringIO()):
                self.assertEqual(verify_distribution.main(), 0)
                # A stale Python 3.12 DLL must not satisfy the 3.14 release gate.
                (distribution / "_internal" / "python312.dll").touch()
                for missing in ("app/automix/analysis/models/yamnet.onnx",
                                "app/resources/help/ko/lyrics_transition.png",
                                "python314.dll",
                                "ctranslate2/_ext.test.pyd", "sentencepiece/_sentencepiece.test.pyd",
                                "music_motion_native.test.pyd",
                                "native/d3d11_qt_canvas_stream_export.exe",
                                "assets/icons/spin_up_disabled.svg"):
                    target = distribution / "_internal" / missing
                    target.unlink()
                    self.assertEqual(verify_distribution.main(), 1, missing)
                    target.touch()

    def test_integrity_manifest_is_required_and_covers_gpu_worker(self):
        with TemporaryDirectory() as directory:
            distribution = Path(directory)
            for name in ("Music Motion Studio.exe",
                         "_internal/python314.dll",
                         "_internal/native/d3d11_qt_canvas_stream_export.exe"):
                target = distribution / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"verified-payload")
            required = {
                Path("python314.dll"),
                Path("native/d3d11_qt_canvas_stream_export.exe"),
            }
            verify = verify_distribution._verify_integrity_manifest
            self.assertIn("integrity.json (missing or unreadable)",
                          verify(distribution, required))
            manifest = distribution / "integrity.json"
            manifest.write_text("{broken", encoding="utf-8")
            self.assertIn("integrity.json (missing or unreadable)",
                          verify(distribution, required))
            manifest.write_text('{"files": []}', encoding="utf-8")
            self.assertIn("integrity.json (invalid files map)",
                          verify(distribution, required))
            self._write_manifest(distribution)
            self.assertEqual(verify(distribution, required), [])
            doc = json.loads(manifest.read_text(encoding="utf-8"))
            del doc["files"]["_internal/native/d3d11_qt_canvas_stream_export.exe"]
            manifest.write_text(json.dumps(doc), encoding="utf-8")
            self.assertTrue(any(
                "missing valid SHA-256: _internal/native/" in error
                for error in verify(distribution, required)
            ))

    def test_tampered_bundled_models_licenses_and_locales_fail_release(self):
        # Standalone fixture: no 48 MB model files or Windows binary needed.
        with TemporaryDirectory() as directory:
            distribution = Path(directory)
            assets = (
                "app/automix/analysis/models/beat_this_final0_int8.onnx",
                "app/automix/analysis/models/umxhq_vocals_int8.onnx",
                "app/automix/analysis/models/yamnet.onnx",
                "app/automix/analysis/models/yamnet_class_map.csv",
                "app/automix/analysis/models/BEAT_THIS_LICENSE.txt",
                "app/automix/analysis/models/OPEN_UNMIX_LICENSE.txt",
                "app/automix/analysis/models/YAMNET_LICENSE.txt",
                "app/automix/analysis/models/YAMNET_NOTICE.md",
                "app/resources/ko.json",
                "app/resources/en.json",
                "app/resources/help/ko/canvas_workspace.png",
            )
            required = {Path(name) for name in assets}
            for name in assets:
                file = distribution / "_internal" / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(b"original-release-asset")
            self._write_manifest(distribution)
            verify = verify_distribution._verify_integrity_manifest
            self.assertEqual(verify(distribution, required), [])
            for name in assets:
                with self.subTest(path=name):
                    file = distribution / "_internal" / name
                    file.write_bytes(b"changed-after-manifest")
                    self.assertIn(
                        "integrity SHA-256 mismatch: _internal/" + name,
                        verify(distribution, required),
                    )
                    file.write_bytes(b"original-release-asset")
            self.assertEqual(verify(distribution, required), [])

    def test_tampering_with_critical_binary_fails_release_verification(self):
        with TemporaryDirectory() as directory:
            distribution = Path(directory)
            for name in ("Music Motion Studio.exe",
                         "_internal/python314.dll",
                         "_internal/native/d3d11_qt_canvas_stream_export.exe"):
                target = distribution / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"original-binary")
            self._write_manifest(distribution)
            required = {
                Path("python314.dll"),
                Path("native/d3d11_qt_canvas_stream_export.exe"),
            }
            verify = verify_distribution._verify_integrity_manifest
            for name in verify_distribution.CRITICAL_HASH_PATHS:
                with self.subTest(path=name):
                    target = distribution / name
                    target.write_bytes(b"damaged-binary")
                    self.assertIn(
                        "integrity SHA-256 mismatch: " + name.as_posix(),
                        verify(distribution, required),
                    )
                    target.write_bytes(b"original-binary")
            self.assertEqual(verify(distribution, required), [])
