"""Salvage keeps the original package intact and never relaxes strict loading."""

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import struct
import threading
import unittest
import warnings
import zipfile

from app.models.playlist import PlaylistTrack
from app.models.project import ProjectDocument, ProjectSettings
from app.services.project_service import ProjectError, ProjectLoadCancelled, ProjectService
from app.services.project_salvage_service import ProjectSalvageService


def damage_entry(path: Path, name: str) -> None:
    # Stored entry: flip its first payload byte, leaving ZIP central directory
    # and recorded CRC intact. Other entries remain independently readable.
    with zipfile.ZipFile(path) as archive:
        offset = archive.getinfo(name).header_offset
    with path.open("r+b") as stream:
        stream.seek(offset + 26)
        filename_length, extra_length = struct.unpack("<HH", stream.read(4))
        position = offset + 30 + filename_length + extra_length
        stream.seek(position)
        first = stream.read(1)
        stream.seek(position)
        stream.write(bytes((first[0] ^ 0xff,)))


class ProjectSalvageTests(unittest.TestCase):
    def test_damaged_asset_yields_relinkable_copy_and_preserves_original(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "damaged.pvsproj"
            document = ProjectDocument(
                settings=ProjectSettings(title="Salvage fixture"),
                playlist=[
                    PlaylistTrack("assets/good.wav", "Good"),
                    PlaylistTrack("assets/bad.wav", "Bad"),
                ],
            )
            with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr("project.json", json.dumps(document.to_dict()))
                archive.writestr("assets/good.wav", b"good audio")
                archive.writestr("assets/bad.wav", b"bad audio")
            damage_entry(source, "assets/bad.wav")
            original = source.read_bytes()
            with self.assertRaises(ProjectError):
                ProjectService.load(source)
            result = ProjectSalvageService.salvage(source, root / "recovery")
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(len(result.recovered_assets), 1)
            self.assertEqual(result.recovered_assets[0].read_bytes(), b"good audio")
            self.assertIsNotNone(result.project_path)
            restored = ProjectService.load(result.project_path)
            self.assertEqual(restored.settings.title, "Salvage fixture")
            self.assertEqual(restored.settings.content_mode, "reference")
            self.assertEqual(Path(restored.playlist[0].file_path).read_bytes(), b"good audio")
            self.assertFalse(Path(restored.playlist[1].file_path).exists())
            self.assertTrue(any(problem.entry == "assets/bad.wav" for problem in result.problems))
            self.assertTrue((result.directory / "recovery-report.json").is_file())

    def test_invalid_manifest_still_exports_safe_independent_assets(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "broken.pvsproj"
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(source, "w") as archive:
                    archive.writestr("project.json", b"{")
                    archive.writestr("assets/good.wav", b"media")
                    archive.writestr("assets/../escape.wav", b"unsafe")
                    archive.writestr("assets/same.wav", b"first")
                    archive.writestr("assets/SAME.wav", b"second")
            original = source.read_bytes()
            result = ProjectSalvageService.salvage(source, root / "recovery")
            self.assertIsNone(result.project_path)
            self.assertEqual(len(result.recovered_assets), 1)
            self.assertEqual(result.recovered_assets[0].read_bytes(), b"media")
            self.assertFalse((root / "escape.wav").exists())
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual({problem.entry for problem in result.problems},
                             {"project.json", "assets/../escape.wav", "assets/same.wav", "assets/SAME.wav"})

    def test_cancel_and_existing_directory_never_modify_existing_data(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            source = ProjectService.save(root / "work.pvsproj", ProjectDocument())
            original = source.read_bytes()
            existing = root / "existing"
            existing.mkdir()
            marker = existing / "keep.txt"
            marker.write_bytes(b"keep")
            with self.assertRaises(ProjectError):
                ProjectSalvageService.salvage(source, existing)
            self.assertEqual(marker.read_bytes(), b"keep")
            cancel = threading.Event()
            cancel.set()
            with self.assertRaises(ProjectLoadCancelled):
                ProjectSalvageService.salvage(source, root / "cancelled", cancel_event=cancel)
            self.assertFalse((root / "cancelled").exists())
            self.assertEqual(source.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
