from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
import shutil
import time
import unittest
import zipfile
from unittest.mock import patch

from PySide6.QtGui import QColor, QImage

from app import __version__
from app.models.playlist import PlaylistTrack
from app.models.project import ProjectContent, ProjectDocument, ProjectSettings
from app.models.source import Source, SourceType
from app.services.project_service import ProjectError, ProjectLoadCancelled, ProjectService
from app.services.project_media_service import ProjectMediaService


class ProjectServiceTests(unittest.TestCase):
    def test_package_save_enforces_the_same_limits_as_load_before_replacing_a_file(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "work.pvsproj"
            document = ProjectDocument(sources=[Source(SourceType.TEXT, "Text", text="가사" * 100)])
            target.write_bytes(b"previous project")
            payload_size = len(json.dumps(document.to_dict(), ensure_ascii=False, indent=2).encode("utf-8"))
            with patch.object(ProjectService, "MAX_MANIFEST_BYTES", payload_size - 1):
                with self.assertRaisesRegex(ProjectError, "manifest"):
                    ProjectService.save(target, document)
            self.assertEqual(target.read_bytes(), b"previous project")
            with patch.object(ProjectService, "MAX_MANIFEST_BYTES", payload_size + 100):
                ProjectService.save(target, document)
                self.assertEqual(ProjectService.load(target).sources[0].text, document.sources[0].text)
                self.assertEqual(ProjectService.inspect(target).title, document.settings.title)

            audio = root / "song.wav"
            audio.write_bytes(b"audio")
            document.playlist = [PlaylistTrack(str(audio), "Song")]
            previous = target.read_bytes()
            with patch.object(ProjectService, "MAX_ASSETS", 0):
                with self.assertRaisesRegex(ProjectError, "too many assets"):
                    ProjectService.save(target, document)
            self.assertEqual(target.read_bytes(), previous)
            self.assertEqual(list(root.glob("*.tmp")), [])

    def test_invalid_json_save_preserves_the_last_valid_project(self):
        with TemporaryDirectory() as raw:
            target = ProjectService.save(Path(raw) / "work.json", ProjectDocument())
            previous = target.read_bytes()
            invalid = ProjectDocument(playlist=[PlaylistTrack("song.wav", "Song", lyrics=[
                {"start": 10, "end": 5, "text": "Invalid"},
            ])])
            with self.assertRaises(ProjectError):
                ProjectService.save(target, invalid)
            self.assertEqual(target.read_bytes(), previous)

    def test_cleanup_keeps_media_leased_by_a_running_editor(self):
        with TemporaryDirectory() as raw, patch("app.services.project_service.gettempdir", return_value=raw), ExitStack() as cleanup:
            cleanup.callback(ProjectService.release_cache_locks)
            root = Path(raw)
            audio = root / "song.wav"
            audio.write_bytes(b"audio")
            package = ProjectService.save(root / "work.pvsproj", ProjectDocument(
                playlist=[PlaylistTrack(str(audio), "Song")],
            ))
            cached = Path(ProjectService.load(package).playlist[0].file_path)
            cache_root = cached.parent.parent
            stale = cache_root.with_name("unused-cache")
            stale.mkdir()
            old = time.time() - 31 * 86_400
            for directory in (cache_root, stale):
                os.utime(directory, (old, old))
            ProjectService.cleanup_cache()
            self.assertEqual(cached.read_bytes(), b"audio")
            self.assertFalse(stale.exists())
            ProjectService._cache_locks.pop(cache_root).unlock()
            ProjectService.cleanup_cache()
            self.assertFalse(cache_root.exists())

    def test_recovery_reextracts_missing_media_and_keeps_unsaved_edits_and_external_paths(self):
        with TemporaryDirectory() as raw, patch("app.services.project_service.gettempdir", return_value=raw), ExitStack() as cleanup:
            cleanup.callback(ProjectService.release_cache_locks)
            root = Path(raw)
            audio = root / "song.wav"
            audio.write_bytes(b"audio")
            image = root / "cover.png"
            thumbnail = QImage(16, 16, QImage.Format.Format_RGB32)
            thumbnail.fill(QColor("red"))
            thumbnail.save(str(image))
            package = ProjectService.save(root / "work.pvsproj", ProjectDocument(
                sources=[Source(SourceType.IMAGE, "Cover", content_path=str(image))],
                playlist=[PlaylistTrack(str(audio), "Song", cover_path=str(image), video_paths=[str(image)])],
                content_library=[ProjectContent(str(audio), "audio")],
                settings=ProjectSettings(thumbnail_mode="custom", thumbnail_path=str(image)),
            ), thumbnail)
            document = ProjectService.load(package)
            document.playlist[0].title = "Unsaved title"
            external = str(root / "missing-external.png")
            document.sources.append(Source(SourceType.IMAGE, "Missing", content_path=external))
            previous_path = Path(document.playlist[0].file_path)
            shutil.rmtree(previous_path.parent.parent)
            stat = package.stat()
            os.utime(package, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
            with patch.object(ProjectService, "load", wraps=ProjectService.load) as load:
                ProjectService.restore_recovery_media(document, package, load)
                load.assert_called_once_with(package)
            self.assertNotEqual(Path(document.playlist[0].file_path), previous_path)
            self.assertEqual(document.playlist[0].title, "Unsaved title")
            self.assertEqual(Path(document.playlist[0].file_path).read_bytes(), b"audio")
            self.assertTrue(Path(document.sources[0].content_path).is_file())
            self.assertTrue(Path(document.playlist[0].cover_path).is_file())
            self.assertTrue(Path(document.playlist[0].video_paths[0]).is_file())
            self.assertTrue(Path(document.content_library[0].path).is_file())
            self.assertTrue(Path(document.settings.thumbnail_path).is_file())
            self.assertEqual(document.sources[1].content_path, external)

    def test_simultaneous_editors_use_separate_media_and_reuse_their_own_cache(self):
        with TemporaryDirectory() as raw, patch("app.services.project_service.gettempdir", return_value=raw), ExitStack() as cleanup:
            cleanup.callback(ProjectService.release_cache_locks)
            root = Path(raw)
            audio = root / "song.wav"
            audio.write_bytes(b"audio")
            package = ProjectService.save(root / "work.pvsproj", ProjectDocument(
                playlist=[PlaylistTrack(str(audio), "Song")],
            ))
            first = Path(ProjectService.load(package).playlist[0].file_path)
            first_lease = ProjectService._cache_locks.pop(first.parent.parent)
            cleanup.callback(first_lease.unlock)
            second = Path(ProjectService.load(package).playlist[0].file_path)
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            first_lease.unlock()
            self.assertEqual(ProjectService.load(package).playlist[0].file_path, str(second.resolve()))

    def test_relative_media_prefers_project_folder_over_working_directory(self) -> None:
        with TemporaryDirectory(prefix="pvs-relative-") as directory:
            project_directory = Path(directory)
            media = project_directory / "track.wav"
            media.write_bytes(b"project audio")
            with patch.object(Path, "is_file", return_value=True):
                resolved = ProjectMediaService._resolve_existing_path(
                    "track.wav", project_directory,
                )
            self.assertEqual(resolved, media.resolve())

    def test_inspect_invalid_settings_reports_project_error(self) -> None:
        with TemporaryDirectory(prefix="pvs-inspect-") as directory:
            path = Path(directory) / "invalid.json"
            for settings in (None, [], "invalid", 42):
                with self.subTest(settings=settings):
                    path.write_text(json.dumps({"settings": settings}), encoding="utf-8")
                    with self.assertRaises(ProjectError):
                        ProjectService.inspect(path)

    def test_missing_custom_track_cover_can_be_relinked_or_cleared(self) -> None:
        with TemporaryDirectory(prefix="pvs-cover-relink-") as raw_directory:
            directory = Path(raw_directory)
            audio = directory / "audio.wav"
            audio.write_bytes(b"audio")
            missing_cover = directory / "missing-cover.png"
            document = ProjectDocument(playlist=[PlaylistTrack(
                str(audio), "Track", cover_path=str(missing_cover),
            )])
            missing = ProjectMediaService.validate(
                document, directory / "project.pvsproj",
            )
            self.assertEqual([entry.kind for entry in missing], ["cover"])

            replacement = directory / "replacement.png"
            image = QImage(32, 32, QImage.Format.Format_ARGB32)
            image.fill(QColor("#2563EB"))
            self.assertTrue(image.save(str(replacement)))
            missing[0].replacement_path = str(replacement)
            ProjectMediaService.apply_replacements(document, missing)
            self.assertEqual(
                document.playlist[0].cover_path, str(replacement.resolve()),
            )

            missing[0].replacement_path = ""
            ProjectMediaService.apply_replacements(document, missing)
            self.assertEqual(document.playlist[0].cover_path, "")

    def test_missing_lyrics_file_keeps_cues_embedded_in_project(self) -> None:
        with TemporaryDirectory(prefix="pvs-lyrics-relink-") as raw_directory:
            directory = Path(raw_directory)
            audio = directory / "audio.wav"
            audio.write_bytes(b"audio")
            cues = [{"start": 1.0, "end": 3.0, "text": "Keep me"}]
            document = ProjectDocument(playlist=[PlaylistTrack(
                str(audio), "Track",
                lyrics_path=str(directory / "missing.lrc"),
                lyrics=[cue.copy() for cue in cues],
            )])
            missing = ProjectMediaService.validate(
                document, directory / "project.pvsproj",
            )

            self.assertEqual([entry.kind for entry in missing], ["lyrics"])
            ProjectMediaService.apply_replacements(document, missing)

            self.assertEqual(document.playlist[0].lyrics_path, "")
            self.assertEqual(document.playlist[0].lyrics, cues)

    def test_portable_package_embeds_and_reloads_content(self) -> None:
        with TemporaryDirectory(prefix="pvs-project-test-") as raw_directory:
            directory = Path(raw_directory)
            cover = directory / "cover.png"
            image = QImage(64, 64, QImage.Format.Format_ARGB32)
            image.fill(QColor("#243B55"))
            self.assertTrue(image.save(str(cover), "PNG"))
            audio = directory / "audio.wav"
            audio.write_bytes(b"test-audio-fixture")
            track_cover = directory / "track-cover.png"
            self.assertTrue(image.save(str(track_cover), "PNG"))
            document = ProjectDocument(
                sources=[Source(
                    SourceType.BACKGROUND, "Cover", content_path=str(cover),
                    background_mode="image",
                )],
                playlist=[PlaylistTrack(
                    str(audio), "Track", cover_path=str(track_cover),
                )],
                settings=ProjectSettings(title="Round trip", content_mode="embed"),
            )
            package = ProjectService.save(directory / "round-trip.pvsproj", document, image)
            restored = ProjectService.load(package)
            summary = ProjectService.inspect(package)
            self.assertTrue(Path(restored.sources[0].content_path).is_file())
            self.assertTrue(Path(restored.playlist[0].file_path).is_file())
            self.assertTrue(Path(restored.playlist[0].cover_path).is_file())
            self.assertEqual(
                Path(restored.playlist[0].cover_path).read_bytes(),
                track_cover.read_bytes(),
            )
            self.assertEqual(summary.title, "Round trip")
            self.assertTrue(summary.thumbnail)
            with zipfile.ZipFile(package) as archive:
                manifest = json.loads(archive.read(ProjectService.MANIFEST_NAME))
            self.assertEqual(manifest["app_version"], __version__)

    def test_package_load_reports_byte_progress_and_can_be_cancelled(self) -> None:
        import threading

        with TemporaryDirectory(prefix="pvs-project-progress-") as raw_directory:
            directory = Path(raw_directory)
            audio = directory / "audio.wav"
            audio.write_bytes(b"x" * (3 * 1024 * 1024 + 7))  # several chunks
            package = ProjectService.save(
                directory / "progress.pvsproj",
                ProjectDocument(
                    playlist=[PlaylistTrack(str(audio), "Track")],
                    settings=ProjectSettings(content_mode="embed"),
                ),
            )
            reports: list[tuple[int, int]] = []
            ProjectService.load(package, progress=lambda done, total: reports.append((done, total)))
            total = audio.stat().st_size
            self.assertEqual(reports[0], (0, total))
            self.assertEqual(reports[-1], (total, total))
            self.assertEqual([done for done, _ in reports], sorted(done for done, _ in reports))

            cancel = threading.Event()
            cancel.set()
            with self.assertRaises(ProjectLoadCancelled):
                ProjectService.load(package, cancel_event=cancel)
            self.assertTrue(issubclass(ProjectLoadCancelled, ProjectError))

    def test_repeated_package_saves_do_not_accumulate_hash_prefixes(self) -> None:
        with TemporaryDirectory(prefix="pvs-project-resave-") as raw_directory:
            directory = Path(raw_directory)
            audio = directory / "01. A Tribe Called Jazzyfact.m4a"
            audio.write_bytes(b"audio")
            package = directory / "repeated.pvsproj"
            document = ProjectDocument(
                playlist=[PlaylistTrack(str(audio), "Track")],
                settings=ProjectSettings(content_mode="embed"),
            )

            for _ in range(24):
                ProjectService.save(package, document)
                document = ProjectService.load(package)

            with zipfile.ZipFile(package) as archive:
                asset_names = [
                    Path(name).name for name in archive.namelist()
                    if name.startswith("assets/")
                ]
            self.assertEqual(len(asset_names), 1)
            self.assertLessEqual(
                len(asset_names[0]),
                ProjectService._MAX_ASSET_BASENAME_LENGTH + 13,
            )

    def test_legacy_package_with_overlong_asset_name_is_recovered(self) -> None:
        with TemporaryDirectory(prefix="pvs-project-long-name-") as raw_directory:
            directory = Path(raw_directory)
            package = directory / "legacy.pvsproj"
            long_name = f"{'1eebca161124_' * 22}01. Track.m4a"
            archive_path = f"assets/{long_name}"
            document = ProjectDocument(
                playlist=[PlaylistTrack(archive_path, "Track")],
                settings=ProjectSettings(content_mode="embed"),
            )
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr(
                    ProjectService.MANIFEST_NAME,
                    json.dumps(document.to_dict()).encode("utf-8"),
                )
                archive.writestr(archive_path, b"legacy-audio")

            restored = ProjectService.load(package)
            restored_audio = Path(restored.playlist[0].file_path)
            self.assertTrue(restored_audio.is_file())
            self.assertEqual(restored_audio.read_bytes(), b"legacy-audio")
            self.assertLessEqual(
                len(restored_audio.name),
                ProjectService._MAX_ASSET_BASENAME_LENGTH + 13,
            )

    @staticmethod
    def _write_package(package: Path, track_path: str, entries: dict[str, bytes]) -> None:
        document = ProjectDocument(
            playlist=[PlaylistTrack(track_path, "Track")],
            settings=ProjectSettings(content_mode="embed"),
        )
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr(
                ProjectService.MANIFEST_NAME,
                json.dumps(document.to_dict()).encode("utf-8"),
            )
            for name, payload in entries.items():
                archive.writestr(name, payload)

    def test_package_rejects_internal_references_that_were_not_extracted(self) -> None:
        with TemporaryDirectory(prefix="pvs-project-unsafe-") as raw_directory:
            directory = Path(raw_directory)
            (directory / "secret.txt").write_text("secret", encoding="utf-8")
            real = {"assets/0123456789ab_track.mp3": b"audio"}
            for reference in (
                "assets/../../secret.txt",
                "assets/../secret.txt",
                "assets/missing.mp3",
                "assets\\..\\..\\secret.txt",
                "assets/%2e%2e/secret.txt",
                "assets//0123456789ab_track.mp3",
                "assets/./0123456789ab_track.mp3",
                "Assets/0123456789ab_track.mp3",
                "assets",
            ):
                with self.subTest(reference=reference):
                    package = directory / "unsafe.pvsproj"
                    self._write_package(package, reference, real)
                    with self.assertRaises(ProjectError):
                        ProjectService.load(package)

    def test_package_rejects_duplicate_and_case_colliding_asset_entries(self) -> None:
        with TemporaryDirectory(prefix="pvs-project-dupe-") as raw_directory:
            package = Path(raw_directory) / "dupe.pvsproj"
            self._write_package(package, "assets/a_track.mp3", {
                "assets/a_track.mp3": b"one", "assets/A_track.mp3": b"two",
            })
            with self.assertRaises(ProjectError):
                ProjectService.load(package)
            with patch("warnings.warn"):  # zipfile warns on duplicate names
                self._write_package(package, "assets/a_track.mp3", {})
                with zipfile.ZipFile(package, "a") as archive:
                    archive.writestr("assets/a_track.mp3", b"one")
                    archive.writestr("assets/a_track.mp3", b"two")
            with self.assertRaises(ProjectError):
                ProjectService.load(package)

    def test_package_keeps_valid_internal_and_external_references(self) -> None:
        with TemporaryDirectory(prefix="pvs-project-valid-") as raw_directory:
            directory = Path(raw_directory)
            package = directory / "valid.pvsproj"
            name = "assets/0123456789ab_곡 ♪.mp3"
            self._write_package(package, name, {name: b"audio"})
            restored = Path(ProjectService.load(package).playlist[0].file_path)
            self.assertEqual(restored.read_bytes(), b"audio")

            # A leading slash is an (external) absolute path, never an asset.
            external = str((directory / "outside.mp3").resolve())
            for reference in ("/assets/file.mp3", external):
                with self.subTest(reference=reference):
                    self._write_package(package, reference, {})
                    self.assertEqual(
                        ProjectService.load(package).playlist[0].file_path, reference,
                    )

    def test_legacy_json_keeps_external_absolute_paths(self) -> None:
        with TemporaryDirectory(prefix="pvs-project-json-") as raw_directory:
            directory = Path(raw_directory)
            external = str((directory / "elsewhere" / "track.mp3").resolve())
            project = directory / "legacy.json"
            project.write_text(json.dumps(ProjectDocument(
                playlist=[PlaylistTrack(external, "Track")],
            ).to_dict()), encoding="utf-8")
            self.assertEqual(ProjectService.load(project).playlist[0].file_path, external)


if __name__ == "__main__":
    unittest.main()
