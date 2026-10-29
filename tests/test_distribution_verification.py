"""The release gate must reject builds missing new runtime assets."""

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import verify_distribution


class DistributionVerificationTests(unittest.TestCase):
    def test_complete_build_passes_and_missing_yamnet_or_help_fails(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            distribution = Path(directory)
            required = set(verify_distribution.REQUIRED_FILES)
            required.update(path.relative_to(root) for path in (root / "app/resources/help").glob("*/*.png"))
            required.update((Path("numpy/_core/_multiarray_umath.test.pyd"),
                             Path("onnxruntime/capi/onnxruntime_pybind11_state.test.pyd")))
            (distribution / "Music Motion Studio.exe").touch()
            for path in required:
                target = distribution / "_internal" / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()
            with patch("sys.argv", ["verify_distribution.py", str(distribution)]), redirect_stdout(StringIO()):
                self.assertEqual(verify_distribution.main(), 0)
                # A stale Python 3.12 DLL must not satisfy the 3.14 release gate.
                (distribution / "_internal" / "python312.dll").touch()
                for missing in ("app/automix/analysis/models/yamnet.onnx",
                                "app/resources/help/ko/lyrics_transition.png",
                                "python314.dll",
                                "assets/icons/spin_up_disabled.svg"):
                    target = distribution / "_internal" / missing
                    target.unlink()
                    self.assertEqual(verify_distribution.main(), 1, missing)
                    target.touch()
