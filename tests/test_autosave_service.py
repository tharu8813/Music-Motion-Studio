from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import json
import os
from unittest.mock import patch

from app.models.project import ProjectDocument
from app.models.playlist import PlaylistTrack
from app.services.autosave_service import AutosaveService, AutosaveSuperseded
from app.services.autosave_worker import AutosaveWorker
from app.services.project_service import ProjectError, ProjectRevision


def _document() -> ProjectDocument:
    return ProjectDocument(
        playlist=[PlaylistTrack("C:/music/a.mp3", "A", "Artist", "Album",
                                duration_seconds=120.0)],
    )


class AutosaveServiceTests(unittest.TestCase):
    def test_recovery_worker_preserves_original_revision_and_legacy_metadata_is_safe(self):
        with TemporaryDirectory(prefix="pvs-autosave-") as directory:
            root = Path(directory)
            service = AutosaveService(root)
            target = root / "work.pvsproj"
            revision = ProjectRevision(1, 2, 300, 400, 500)
            worker = AutosaveWorker(service, _document().to_dict(), target,
                                    base_revision=revision)
            worker.run()
            snapshot = service.latest_recovery()
            self.assertEqual(snapshot.base_revision, revision)
            self.assertEqual(snapshot.project_path, target)
            payload = json.loads(snapshot.path.read_text(encoding="utf-8"))
            self.assertEqual(payload["base_revision"]["size"], 300)
            payload.pop("base_revision")
            snapshot.path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertIsNone(service.load_snapshot(snapshot.path).base_revision)
            payload["base_revision"] = dict(device=1, inode=True, size=300,
                                            modified_ns=400, changed_ns=500)
            snapshot.path.write_text(json.dumps(payload), encoding="utf-8")
            recovered = service.load_snapshot(snapshot.path)
            self.assertIsNone(recovered.base_revision)
            self.assertEqual(recovered.document.playlist[0].title, "A")

    def test_recovery_byte_limit_is_symmetric_and_failed_save_preserves_old_snapshot(self) -> None:
        with TemporaryDirectory(prefix="pvs-autosave-") as directory:
            service = AutosaveService(Path(directory))
            saved_at = datetime(2026, 11, 28, tzinfo=UTC)
            document = _document()
            document.playlist[0].title = "한글 가사"
            data = document.to_dict()
            target = service.write_document_data(data, None, saved_at)
            previous = target.read_bytes()
            with patch.object(AutosaveService, "MAX_SNAPSHOT_BYTES", len(previous)):
                service.write_document_data(data, None, saved_at)
                self.assertEqual(target.read_bytes(), previous)
                self.assertEqual(service.load_snapshot(target).document.playlist[0].title, "한글 가사")
                document.playlist[0].title += "가"
                with self.assertRaisesRegex(ProjectError, "size limit"):
                    service.write_document_data(document.to_dict(), None, saved_at)
                self.assertEqual(target.read_bytes(), previous)
                self.assertEqual(list(service.directory.glob("*.tmp")), [])
            with patch.object(AutosaveService, "MAX_SNAPSHOT_BYTES", len(previous) - 1):
                with self.assertRaisesRegex(ProjectError, "size limit"):
                    service.load_snapshot(target)
            self.assertEqual(target.read_bytes(), previous)

    def test_oversized_and_malformed_recoveries_do_not_hide_valid_snapshot(self) -> None:
        with TemporaryDirectory(prefix="pvs-autosave-") as directory:
            service = AutosaveService(Path(directory))
            valid = service.save(_document(), None)
            limit = max(valid.path.stat().st_size, 8192)
            broken = service.directory / "broken.recovery.json"
            payload = json.loads(valid.path.read_text(encoding="utf-8"))
            invalid = [
                b"x" * (limit + 1),
                b"\\xff\\xfe",
                b"[" * 1500 + b"0" + b"]" * 1500,
                b'{"schema_version":NaN}',
                json.dumps(dict(payload, schema_version=True)).encode("utf-8"),
                json.dumps(dict(payload, project_path=0)).encode("utf-8"),
                json.dumps(dict(payload, project_path=[])).encode("utf-8"),
            ]
            with patch.object(AutosaveService, "MAX_SNAPSHOT_BYTES", limit):
                for content in invalid:
                    with self.subTest(prefix=content[:40]):
                        broken.write_bytes(content)
                        with self.assertRaises(ProjectError):
                            service.load_snapshot(broken)
                        self.assertEqual([snap.path for snap in service.recoveries()], [valid.path])
                        self.assertEqual(broken.read_bytes(), content)

    def test_recovery_syncs_before_replacing_previous_snapshot(self) -> None:
        with TemporaryDirectory(prefix="pvs-autosave-") as directory:
            service = AutosaveService(Path(directory))
            existing = service.save(_document(), None)
            previous = existing.path.read_bytes()
            updated = _document()
            updated.playlist[0].title = "Updated"
            actual_sync = os.fsync
            def sync_complete_snapshot(fd):
                self.assertEqual(existing.path.read_bytes(), previous)
                temporary, = service.directory.glob("*.tmp")
                payload = json.loads(temporary.read_text(encoding="utf-8"))
                self.assertEqual(ProjectDocument.from_dict(payload["document"]).playlist[0].title, "Updated")
                actual_sync(fd)
            with patch("app.services.autosave_service.os.fsync", side_effect=sync_complete_snapshot) as sync:
                service.save(updated, None)
                sync.assert_called_once()
            self.assertEqual(service.latest_recovery().document.playlist[0].title, "Updated")
            self.assertEqual(list(service.directory.glob("*.tmp")), [])

    def test_failed_sync_or_replace_keeps_last_good_snapshot(self) -> None:
        with TemporaryDirectory(prefix="pvs-autosave-") as directory:
            service = AutosaveService(Path(directory))
            existing = service.save(_document(), None)
            previous = existing.path.read_bytes()
            updated = _document()
            updated.playlist[0].title = "Not saved"
            for operation in ("fsync", "replace"):
                with self.subTest(operation=operation):
                    mock = (patch("app.services.autosave_service.os.fsync", side_effect=OSError("disk sync failed"))
                            if operation == "fsync" else
                            patch.object(Path, "replace", side_effect=PermissionError("target locked")))
                    with mock, self.assertRaises(ProjectError):
                        service.save(updated, None)
                    self.assertEqual(existing.path.read_bytes(), previous)
                    self.assertEqual(service.latest_recovery().document.playlist[0].title, "A")
                    self.assertEqual(list(service.directory.glob("*.tmp")), [])

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

    def test_superseded_recovery_write_cannot_recreate_cleared_snapshot(self) -> None:
        with TemporaryDirectory(prefix="pvs-autosave-") as directory:
            service = AutosaveService(Path(directory))
            existing = service.save(_document(), None)
            expected = service.generation
            service.clear(None)
            self.assertFalse(existing.path.exists())
            with self.assertRaises(AutosaveSuperseded):
                service.write_document_data(
                    _document().to_dict(), None, expected_generation=expected,
                )
            self.assertFalse(existing.path.exists())
            self.assertEqual(list(service.directory.glob("*.tmp")), [])

    def test_stale_autosave_worker_emits_neither_success_nor_failure(self) -> None:
        with TemporaryDirectory(prefix="pvs-autosave-") as directory:
            service = AutosaveService(Path(directory))
            worker = AutosaveWorker(service, _document().to_dict(), None)
            success, failure = [], []
            worker.succeeded.connect(success.append)
            worker.failed.connect(failure.append)
            service.clear(None)
            worker.run()
            self.assertEqual(success, [])
            self.assertEqual(failure, [])
            self.assertEqual(service.recoveries(), [])

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
