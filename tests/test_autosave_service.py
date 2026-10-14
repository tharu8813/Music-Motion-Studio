from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import json

from app.models.project import ProjectDocument
from app.models.playlist import PlaylistTrack
from app.services.autosave_service import AutosaveService
from app.services.project_service import ProjectError


def _document() -> ProjectDocument:
    return ProjectDocument(
        playlist=[PlaylistTrack("C:/music/a.mp3", "A", "Artist", "Album",
                                duration_seconds=120.0)],
    )


class AutosaveServiceTests(unittest.TestCase):
    def test_malformed_recovery_does_not_hide_valid_snapshots(self) -> None:
        with TemporaryDirectory(prefix="pvs-autosave-") as directory:
            service = AutosaveService(Path(directory))
            valid = service.save(_document(), None)
            for payload in ([], None, "invalid"):
                with self.subTest(payload=payload):
                    (service.directory / "broken.recovery.json").write_text(
                        json.dumps(payload), encoding="utf-8",
                    )
                    self.assertEqual([s.path for s in service.recoveries()], [valid.path])

    def test_naive_recovery_timestamp_does_not_break_sorting(self) -> None:
        with TemporaryDirectory(prefix="pvs-autosave-") as directory:
            service = AutosaveService(Path(directory))
            service.save(_document(), None)
            service.write_document_data(
                _document().to_dict(), Path(directory) / "older.json",
                datetime(2020, 1, 1),
            )
            snapshots = service.recoveries()
            self.assertEqual(len(snapshots), 2)
            self.assertEqual(snapshots[-1].saved_at, datetime(2020, 1, 1, tzinfo=UTC))

    def test_save_round_trips_through_recoveries(self) -> None:
        with TemporaryDirectory(prefix="pvs-autosave-") as directory:
            service = AutosaveService(Path(directory))
            snapshot = service.save(_document(), None)
            self.assertTrue(snapshot.path.is_file())
            loaded = service.latest_recovery()
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.document.playlist[0].title, "A")

    def test_update_snapshot_preserves_dirty_state_and_legacy_defaults(self) -> None:
        with TemporaryDirectory(prefix="pvs-update-recovery-") as directory:
            service = AutosaveService(Path(directory))
            snapshot = service.save(_document(), None, dirty=False)
            self.assertFalse(service.load_snapshot(snapshot.path).dirty)
            payload = json.loads(snapshot.path.read_text(encoding="utf-8"))
            payload.pop("dirty")
            snapshot.path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertTrue(service.load_snapshot(snapshot.path).dirty)
            payload["dirty"] = "false"
            snapshot.path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ProjectError):
                service.load_snapshot(snapshot.path)

    def test_write_document_data_matches_save_and_is_dict_only(self) -> None:
        with TemporaryDirectory(prefix="pvs-autosave-") as directory:
            service = AutosaveService(Path(directory))
            data = _document().to_dict()
            saved_at = datetime(2026, 9, 4, tzinfo=UTC)
            target = service.write_document_data(data, None, saved_at)
            self.assertTrue(target.is_file())
            recovered = service.latest_recovery()
            self.assertEqual(recovered.saved_at, saved_at)
            self.assertEqual(recovered.document.playlist[0].title, "A")

    def test_write_document_data_leaves_no_temp_file_on_failure(self) -> None:
        with TemporaryDirectory(prefix="pvs-autosave-") as directory:
            service = AutosaveService(Path(directory))
            with self.assertRaises(ProjectError):
                # object() is not JSON serializable -> json.dump raises TypeError
                service.write_document_data({"document": object()}, None)
            leftovers = list(service.directory.glob("*.tmp"))
            self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
