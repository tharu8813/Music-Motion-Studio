"""Regenerate the User Guide screenshots in app/resources/help/<language>/.

Builds a small sample project (synthesized songs, covers and LRC lyrics), opens
each window the guide describes, and saves a screenshot with numbered callouts
that match the ①②③ lists in app/dialogs/help_content.py. Run it again after a
UI change, once per release is plenty:

    python tools/capture_help_images.py            # both languages
    python tools/capture_help_images.py --lang ko  # one language
    python tools/capture_help_images.py --only lyrics_editor  # one window

It needs FFmpeg on PATH (song analysis and the audio previews use it). On a
machine without a display it runs offscreen; install a CJK font (for example
Noto Sans CJK) so the Korean screenshots do not show empty boxes.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import wave
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSettings, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QPen  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

OUTPUT = ROOT / "app" / "resources" / "help"
SAMPLE_RATE = 44_100
MAX_WIDTH = 1280
ACCENT = QColor("#79C7B4")

SONGS = (
    # title, artist, album, bpm, seconds, root midi note, cover colors
    ("Night Drive", "Lumen Avenue", "City Lights", 122, 52, 45, ("#1C3D5A", "#79C7B4")),
    ("Paper Planes", "Mira Sol", "Blue Hours", 124, 48, 50, ("#4A2545", "#F2A65A")),
    ("Afterglow", "The Low Tides", "Afterglow", 120, 50, 43, ("#2D1E4A", "#E86A92")),
    ("Slow Morning", "Hana Park", "Weekend", 96, 46, 48, ("#3B4A2E", "#E9D985")),
)

LYRICS = (
    "Streetlights are humming a song",
    "I keep the window down",
    "Every mile is a memory",
    "Take me home before the dawn",
    "We are running on a feeling",
    "Hold on, hold on",
)


# -- sample media ---------------------------------------------------------------------------


def _tone(frequency: float, seconds: float, *, decay: float = 0.0) -> np.ndarray:
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    wave_ = np.sin(2 * np.pi * frequency * t)
    if decay:
        wave_ *= np.exp(-t * decay)
    return wave_


def _midi(note: float) -> float:
    return 440.0 * 2 ** ((note - 69) / 12)


def _song(bpm: int, seconds: int, root: int, seed: int) -> np.ndarray:
    """A plain drum-and-chords loop with a sung-like melody in the middle: enough to analyze."""
    rng = np.random.default_rng(seed)
    total = int(seconds * SAMPLE_RATE)
    mix = np.zeros(total)
    beat = 60.0 / bpm
    kick = _tone(55, 0.25, decay=18) * 0.9
    hat = rng.standard_normal(int(0.04 * SAMPLE_RATE)) * np.exp(-np.arange(int(0.04 * SAMPLE_RATE)) / 300) * 0.18
    progression = (0, 5, 7, 3)
    bar = beat * 4
    intro, outro = bar * 4, seconds - bar * 4
    for index in range(int(seconds / beat)):
        start = int(index * beat * SAMPLE_RATE)
        if start + len(kick) < total:
            mix[start:start + len(kick)] += kick
        off = int((index + 0.5) * beat * SAMPLE_RATE)
        if off + len(hat) < total:
            mix[off:off + len(hat)] += hat
    for bar_index in range(int(seconds / bar) + 1):
        start = int(bar_index * bar * SAMPLE_RATE)
        length = min(total - start, int(bar * SAMPLE_RATE))
        if length <= 0:
            break
        chord_root = root + progression[bar_index % 4]
        pad = sum(_tone(_midi(chord_root + 12 + step), bar)[:length] for step in (0, 4, 7)) * 0.07
        bass = _tone(_midi(chord_root - 12), bar)[:length] * 0.25
        mix[start:start + length] += pad + bass
        position = bar_index * bar
        if intro <= position < outro:  # the "vocal": a vibrato line over the chords
            t = np.arange(length) / SAMPLE_RATE
            melody = _midi(chord_root + 24 + (bar_index % 3) * 2)
            voice = np.sin(2 * np.pi * melody * t + 0.02 * melody * np.sin(2 * np.pi * 5.5 * t) / 5.5)
            voice += 0.4 * np.sin(4 * np.pi * melody * t)
            envelope = np.minimum(1.0, t * 8) * np.minimum(1.0, (bar - t) * 8)
            mix[start:start + length] += voice * envelope * 0.12
    fade = int(SAMPLE_RATE * 1.5)
    mix[-fade:] *= np.linspace(1, 0, fade)
    return mix / max(1e-6, np.abs(mix).max()) * 0.8


def _write_wav(path: Path, samples: np.ndarray) -> None:
    stereo = np.repeat((samples * 32767).astype("<i2")[:, None], 2, axis=1)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(stereo.tobytes())


def _write_cover(path: Path, title: str, colors: tuple[str, str]) -> None:
    image = QImage(600, 600, QImage.Format.Format_RGB32)
    painter = QPainter(image)
    gradient = QLinearGradient(0, 0, 600, 600)
    gradient.setColorAt(0, QColor(colors[0]))
    gradient.setColorAt(1, QColor(colors[1]))
    painter.fillRect(image.rect(), gradient)
    painter.setPen(QPen(QColor(255, 255, 255, 60), 18))
    painter.drawEllipse(QPointF(300, 300), 180, 180)
    painter.setPen(QColor("white"))
    font = QFont("Noto Sans", 44)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(QRect(40, 420, 520, 140), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom), title)
    painter.end()
    image.save(str(path))


def _write_lrc(path: Path, title: str, artist: str, seconds: int, bpm: int) -> None:
    bar = 240.0 / bpm
    lines = [f"[ti:{title}]", f"[ar:{artist}]"]
    time = bar * 4
    for index, text in enumerate(LYRICS):
        minutes, rest = divmod(time, 60)
        lines.append(f"[{int(minutes):02d}:{rest:05.2f}]{text}")
        time += bar * 1.5
        if time > seconds - bar * 4:
            break
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_sample_media(folder: Path) -> list[dict]:
    songs = []
    for index, (title, artist, album, bpm, seconds, root, colors) in enumerate(SONGS):
        stem = f"{index + 1:02d} {title}"
        audio = folder / f"{stem}.wav"
        _write_wav(audio, _song(bpm, seconds, root, index))
        cover = folder / f"{stem}.png"
        _write_cover(cover, title, colors)
        lyrics = folder / f"{stem}.lrc"
        _write_lrc(lyrics, title, artist, seconds, bpm)
        songs.append({"audio": audio, "cover": cover, "lyrics": lyrics, "title": title,
                      "artist": artist, "album": album})
    return songs


# -- screenshots ----------------------------------------------------------------------------


def settle(app: QApplication, rounds: int = 25) -> None:
    for _ in range(rounds):
        app.processEvents()
        # Rebuilt rows deleteLater() their old widgets; without a real event loop
        # those only go away here, and a grab would show both.
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def wait(app: QApplication, milliseconds: int) -> None:
    done = []
    QTimer.singleShot(milliseconds, lambda: done.append(True))
    while not done:
        app.processEvents()


def rect_in(window: QWidget, widget: QWidget) -> QRect:
    return QRect(widget.mapTo(window, QPoint(0, 0)), widget.size())


def annotate(image: QImage, callouts: list[tuple[int, QRect, str]]) -> QImage:
    """Outline each rect and put its number in a filled circle at the given corner.

    ``corner`` is two letters: vertical t(op)/m(iddle)/b(ottom)/u(nder the rect),
    then horizontal l(eft)/c(enter)/r(ight).
    """
    result = image.convertToFormat(QImage.Format.Format_ARGB32)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    scale = result.width() / 1400
    radius = max(13.0, 15 * scale)
    font = QFont("Noto Sans", 1)
    font.setPixelSize(round(radius * 1.25))
    font.setBold(True)
    painter.setFont(font)
    for number, rect, corner in callouts:
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(ACCENT, max(2.0, 2.5 * scale)))
        painter.drawRoundedRect(QRectF(rect).adjusted(2, 2, -2, -2), 6, 6)
        x = {"l": rect.left() + radius + 6, "r": rect.right() - radius - 6,
             "c": rect.center().x()}[corner[1]]
        y = {"t": rect.top() + radius + 6, "b": rect.bottom() - radius - 6,
             "m": rect.center().y(), "u": rect.bottom() + radius + 4}[corner[0]]
        painter.setPen(QPen(QColor("#10302A"), 2))
        painter.setBrush(ACCENT)
        painter.drawEllipse(QPointF(x, y), radius, radius)
        painter.setPen(QColor("#0E1A18"))
        painter.drawText(QRectF(x - radius, y - radius, radius * 2, radius * 2),
                         int(Qt.AlignmentFlag.AlignCenter), str(number))
    painter.end()
    return result


def save(image: QImage, language: str, name: str) -> None:
    folder = OUTPUT / language
    folder.mkdir(parents=True, exist_ok=True)
    if image.width() > MAX_WIDTH:
        image = image.scaledToWidth(MAX_WIDTH, Qt.TransformationMode.SmoothTransformation)
    image.convertToFormat(QImage.Format.Format_RGB32).save(str(folder / f"{name}.png"), "PNG", 9)
    print(f"saved {language}/{name}.png ({image.width()}×{image.height()})")


def grab(widget: QWidget) -> QImage:
    return widget.grab().toImage()


# -- the sample project in a main window ------------------------------------------------------


class Session:
    """One MainWindow in one language, holding the sample playlist and its analyses."""

    def __init__(self, app: QApplication, language: str, songs: list[dict], ffmpeg: Path,
                 *, analyze: bool = True) -> None:
        from app.automix.analysis.registry import create_analysis_provider
        from app.automix.analysis.service import AnalysisService
        from app.presets.preset_service import PresetService
        from app.services.lyrics_service import LyricsService
        from app.ui.main_window import MainWindow

        self.app = app
        self.language = language
        self.ffmpeg = ffmpeg
        QSettings().setValue("language", language)
        window = MainWindow()
        window.resize(1500, 940)
        window.show()
        settle(app)
        # CPU preview: an offscreen run has no OpenGL, and GPU mode would show its fallback banner.
        window.settings_service.save(replace(
            window.settings_service.current, ffmpeg_path=str(ffmpeg), preview_backend="cpu",
        ))
        window._preview_backend_for_session = "cpu"
        tracks = []
        for candidate, song in zip(window.playlist_service.inspect_files([s["audio"] for s in songs]), songs):
            track = candidate.track
            track.title, track.artist, track.album = song["title"], song["artist"], song["album"]
            track.cover_path = str(song["cover"])
            track.lyrics_path = str(song["lyrics"])
            track.lyrics = LyricsService.load(song["lyrics"])
            tracks.append(track)
        window.playlist_service.add_tracks(tracks)
        window.project_settings = replace(
            window.project_settings, transition_mode="automix" if analyze else "none",
            title="Late Night Drive", author="Music Motion Studio",
        )
        preset = next(p for p in PresetService().all() if p.identifier == "aurora")
        window._apply_preset(preset)
        if analyze:
            result = AnalysisService(create_analysis_provider("auto", ffmpeg)).analyze_tracks(tracks)
            window._automix_analyses_received(result.analyses)
            try:
                from app.automix.structure.service import StructureAnalysisService
                from app.automix.structure.sonara import SonaraStructureProvider

                structures = StructureAnalysisService(SonaraStructureProvider(ffmpeg)).analyze_tracks(tracks)
                window._automix_structures_received(structures.analyses)
            except Exception as error:  # noqa: BLE001 - structure is optional
                print(f"structure analysis skipped: {error}")
        self.window = window
        self.tracks = window.playlist_service.tracks
        self.refresh()

    def refresh(self) -> None:
        settle(self.app)
        wait(self.app, 400)
        self.window.repaint()
        settle(self.app)

    def close(self) -> None:
        self.window.project_session.dirty = False
        self.window.close()
        settle(self.app)


def capture_canvas(session: Session) -> None:
    window = session.window
    title = next((s for s in window.store.sources() if s.source_type.value == "text"), None)
    if title is not None:
        window.store.select(title.id)
    window.bottom_tabs.setCurrentIndex(0)
    session.refresh()
    image = grab(window)
    top = QRect(0, 0, window.width(), rect_in(window, window.toolbar).bottom())
    callouts = [
        (1, QRect(top.left(), top.top(), rect_in(window, window.toolbar).width() // 3, top.height()), "bl"),
        (2, rect_in(window, window.left_workspace), "tr"),
        (3, rect_in(window, window.canvas_stack), "tr"),
        (4, rect_in(window, window.inspector_stack), "tr"),
        (5, rect_in(window, window.bottom_tabs), "tr"),
        (6, rect_in(window, window.project_status_label).united(rect_in(window, window.export_button))
         .adjusted(-40, -4, 4, 4), "ml"),
        (7, rect_in(window, window.statusBar()), "mc"),
    ]
    save(annotate(image, callouts), session.language, "canvas_workspace")
    save(grab(window.left_workspace), session.language, "canvas_sources")
    save(grab(window.inspector_stack), session.language, "canvas_inspector")
    save(grab(window.bottom_tabs), session.language, "canvas_playlist")
    window.left_tabs.setCurrentIndex(2)
    session.refresh()
    save(grab(window.left_workspace), session.language, "canvas_layers")
    window.left_tabs.setCurrentIndex(1)
    session.refresh()
    save(grab(window.left_workspace), session.language, "canvas_content")
    window.left_tabs.setCurrentIndex(0)
    window.bottom_tabs.setCurrentIndex(1)
    session.refresh()
    save(grab(window.bottom_tabs), session.language, "canvas_timeline")
    window.bottom_tabs.setCurrentIndex(0)
    session.refresh()


def capture_preview(session: Session) -> None:
    window = session.window
    window.bottom_tabs.setCurrentIndex(2)
    session.refresh()
    preview = window._inline_preview
    for _ in range(240):  # the AutoMix preview mix: up to a minute
        details = getattr(preview, "automix_details", None)
        if details is not None and "✓" in details.status_label.text():
            break
        wait(session.app, 250)
    preview._seek_to_seconds(30.0)
    window.activity_progress.clear()
    session.refresh()
    wait(session.app, 800)
    session.refresh()
    window.activity_progress.clear()
    image = grab(window)
    callouts = [
        (1, rect_in(window, preview.preview_stage), "tl"),
        (3, rect_in(window, window.inspector_stack), "tr"),
        (4, rect_in(window, preview.now_playing_card), "tc"),
        (5, rect_in(window, preview.timeline_card), "tc"),
        (6, rect_in(window, preview.transport_card), "ml"),
    ]
    if getattr(preview, "automix_details", None) is not None:
        callouts.append((7, rect_in(window, preview.automix_details), "tr"))
    save(annotate(image, callouts), session.language, "preview_screen")
    if getattr(preview, "automix_details", None) is not None:
        preview._open_transition_window()
        inspector = preview._transition_window
        inspector.resize(1180, 760)
        session.refresh()
        wait(session.app, 500)
        session.refresh()
        details = [
            (1, rect_in(inspector, inspector.overview_title).united(rect_in(inspector, inspector.overview)), "tr"),
            (2, rect_in(inspector, inspector.list_title).united(rect_in(inspector, inspector.list)), "tr"),
            (3, rect_in(inspector, inspector.heading_label).united(rect_in(inspector, inspector.jump_button))
             .adjusted(-6, -6, 6, 6), "tr"),
            (4, rect_in(inspector, inspector.detail_tabs), "tr"),
            (5, rect_in(inspector, inspector.position_slider).united(rect_in(inspector, inspector.previous_button))
             .united(rect_in(inspector, inspector.volume_value_label)).adjusted(-6, -6, 6, 6), "mc"),
            (6, rect_in(inspector, inspector.export_button).adjusted(-40, -4, 4, 4), "ml"),
        ]
        save(annotate(grab(inspector), details), session.language, "preview_transition_details")
        inspector.close()
    window.bottom_tabs.setCurrentIndex(0)
    session.refresh()


def capture_track(session: Session) -> None:
    from app.dialogs.track_details_dialog import TrackDetailsDialog

    window = session.window
    track = session.tracks[1]
    dialog = TrackDetailsDialog(
        track, window.translator, window,
        analysis=window.automix_analyses.get(track.id),
        structure=window.automix_structures.get(track.id),
        ffmpeg_executable=session.ffmpeg,
    )
    dialog.resize(1000, 700)
    dialog.show()
    session.refresh()
    header = dialog.header_cover.parentWidget()
    tab_bar = dialog.tabs.tabBar()
    callouts = [
        (1, rect_in(dialog, header), "tr"),
        (2, rect_in(dialog, tab_bar).adjusted(-4, -4, 40, 4), "mr"),
        (3, rect_in(dialog, dialog.tabs).adjusted(0, tab_bar.height(), 0, 0), "tr"),
    ]
    buttons = [button for button in dialog.buttons.buttons() if button.isVisible()]
    if buttons:
        area = rect_in(dialog, buttons[0])
        for button in buttons[1:]:
            area = area.united(rect_in(dialog, button))
        callouts.append((4, area.adjusted(-40, -4, 4, 4), "ml"))
    save(annotate(grab(dialog), callouts), session.language, "track_info")
    for index, name in ((1, "track_analysis"), (2, "track_lyrics"), (3, "track_videos"), (4, "track_audio")):
        dialog.tabs.setCurrentIndex(index)
        session.refresh()
        save(grab(dialog), session.language, name)
    dialog.media_player.stop()
    dialog.reject()
    dialog.deleteLater()
    session.refresh()


def capture_lyrics_editor(session: Session) -> None:
    from app.dialogs.lrc_generator_dialog import LrcGeneratorDialog

    window = session.window
    track = session.tracks[0]
    dialog = LrcGeneratorDialog(
        window.project_content_service.items, window.translator, window,
        playlist_tracks=window.playlist_service.tracks,
    )
    dialog.resize(1080, 860)
    dialog.show()
    session.refresh()
    save(grab(dialog), session.language, "lyrics_audio")
    dialog.close()
    dialog.deleteLater()
    dialog = LrcGeneratorDialog(
        [], window.translator, window, track_edit_mode=True,
        initial_audio_path=track.file_path, initial_cues=track.lyrics,
        initial_title=track.title, initial_artist=track.artist,
    )
    dialog.resize(1080, 860)
    dialog.show()
    dialog.pages.setCurrentIndex(1)
    session.refresh()
    save(grab(dialog), session.language, "lyrics_input")
    dialog.pages.setCurrentIndex(2)
    session.refresh()
    if len(dialog.timestamps) > 2:
        dialog.timestamps[-2:] = [None, None]
        dialog.current_index = len(dialog.timestamps) - 2
    dialog._position_changed(int((dialog.timestamps[1] or 8.0) * 1000) + 500)
    dialog._refresh_table()
    dialog.timeline_table.selectRow(1)
    session.refresh()
    callouts = [
        (1, rect_in(dialog, dialog.step_label).united(rect_in(dialog, dialog.step_description))
         .united(rect_in(dialog, dialog.step_dots)).adjusted(-6, -6, 6, 6), "mc"),
        (2, rect_in(dialog, dialog.timeline_table), "tr"),
        (3, rect_in(dialog, dialog.record_button), "mr"),
        (4, rect_in(dialog, dialog.history_tools_group).united(rect_in(dialog, dialog.lyric_tools_group)), "tr"),
        (5, rect_in(dialog, dialog.calibration_label).united(rect_in(dialog, dialog.calibration_help)), "mr"),
        (6, rect_in(dialog, dialog.preview_group), "tr"),
        (7, rect_in(dialog, dialog.back_button).united(rect_in(dialog, dialog.close_button))
         .adjusted(-40, -4, 4, 4), "ml"),
    ]
    save(annotate(grab(dialog), callouts), session.language, "lyrics_timing")
    dialog.pages.setCurrentIndex(3)
    session.refresh()
    save(grab(dialog), session.language, "lyrics_review")
    dialog.media_player.stop()
    dialog._draft_dirty = False
    dialog.done(0)
    dialog.deleteLater()
    session.refresh()


def capture_automix_editor(session: Session) -> None:
    from app.dialogs.automix_editor_dialog import AutoMixEditorDialog

    window = session.window
    editor = AutoMixEditorDialog(window, (session.tracks[0].id, session.tracks[1].id), session.ffmpeg)
    editor.resize(1400, 860)
    editor.show()
    session.refresh()
    wait(session.app, 1500)
    session.refresh()
    transport = editor.play_button.parentWidget()
    callouts = [
        (1, rect_in(editor, editor.previous_button).united(rect_in(editor, editor.next_button))
         .adjusted(-6, -6, 6, 6), "ml"),
        (2, rect_in(editor, editor.simple_button).united(rect_in(editor, editor.advanced_button))
         .united(rect_in(editor, editor.help_button)).adjusted(-6, -6, 6, 6), "ul"),
        (3, rect_in(editor, editor.tools_bar), "mc"),
        (4, rect_in(editor, editor.timeline_scroll), "bl"),
        (5, rect_in(editor, editor.properties_scroll), "tr"),
        (6, rect_in(editor, transport), "tr"),
    ]
    save(annotate(grab(editor), callouts), session.language, "automix_editor")
    editor._set_advanced(True)
    session.refresh()
    wait(session.app, 500)
    session.refresh()
    save(grab(editor), session.language, "automix_editor_advanced")
    save(grab(editor.properties_scroll), session.language, "automix_properties")
    editor._set_advanced(False)
    editor.close()
    session.refresh()


def capture_dialogs(session: Session) -> None:
    from app.dialogs.export_settings_dialog import ExportSettingsDialog
    from app.dialogs.new_project_dialog import NewProjectDialog
    from app.dialogs.project_settings_dialog import ProjectSettingsDialog
    from app.dialogs.settings_dialog import SettingsDialog

    window = session.window
    window.settings_service.save(replace(window.settings_service.current, preview_backend="gpu_layers"))

    def shoot(dialog, name: str, size: tuple[int, int] | None = None) -> None:
        if size:
            dialog.resize(*size)
        dialog.show()
        session.refresh()
        save(grab(dialog), session.language, name)

    dialog = NewProjectDialog(window.translator, window)
    shoot(dialog, "new_project")
    dialog.reject()
    thumbnail = window.canvas.grab()
    dialog = ProjectSettingsDialog(window.project_settings, window.translator, thumbnail, window)
    dialog.tabs.setCurrentIndex(2)
    shoot(dialog, "project_settings")
    dialog.reject()
    settings = window.settings_service.current
    dialog = SettingsDialog(settings, window.translator.language, window.theme_service.preference,
                            window.translator, window)
    shoot(dialog, "settings_general")
    for index, name in ((5, "settings_maintenance"),):
        dialog.tabs.setCurrentIndex(index)
        session.refresh()
        save(grab(dialog), session.language, name)
    dialog.reject()
    dialog = ExportSettingsDialog(settings, len(session.tracks), 180.0, window.translator,
                                  Path(tempfile.gettempdir()) / "Late Night Drive.mp4", window)
    shoot(dialog, "export_settings")
    dialog.reject()
    session.refresh()


def capture_source_effects(session: Session) -> None:
    """Capture source customization using sample data; no audio analysis is needed."""
    from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1
    from app.dialogs.lyrics_animation_dialog import LyricsAnimationDialog
    from app.dialogs.lyrics_line_style_dialog import LyricsLineStyleDialog
    from app.dialogs.mp3_metadata_editor_dialog import Mp3MetadataEditorDialog
    from app.models.source import Source, SourceType

    window = session.window
    source = next((s for s in window.store.sources() if s.source_type == SourceType.LYRICS), None)
    if source is None:
        source = Source(SourceType.LYRICS, "가사 / 자막" if session.language == "ko" else "Lyrics / subtitles",
                        x=740, y=130, width=1000, height=720, font_size=42)
        window.store.add(source)
    source = replace(source, subtitle_advanced_categories=["motion", "styles", "intro"],
                     fill_color="#00000000", font_size=30,
                     subtitle_animation="cascade", subtitle_stagger=0.12,
                     subtitle_intro_enabled=True, subtitle_intro_midtrack=True)
    dialog = LyricsAnimationDialog(source, window.translator, window)
    dialog.show()
    session.refresh()
    save(grab(dialog), session.language, "lyrics_transition")
    dialog.tabs.setCurrentIndex(2)
    dialog.role.setCurrentIndex(2)
    session.refresh()
    save(grab(dialog), session.language, "lyrics_role_styles")
    dialog.tabs.setCurrentIndex(3)
    session.refresh()
    save(grab(dialog), session.language, "lyrics_intro")
    dialog.reject()
    dialog.deleteLater()
    settle(session.app)

    dialog = LyricsLineStyleDialog(replace(source, subtitle_advanced_categories=[],
        subtitle_line_styles=[{}, {"font_size_offset": -10.0}]),
        window.translator, window)
    dialog.show()
    session.refresh()
    save(grab(dialog), session.language, "lyrics_line_styles")
    dialog.reject()
    dialog.deleteLater()
    settle(session.app)

    sample = Path(session.tracks[0].file_path).with_name("Night Drive metadata.mp3")
    tags = ID3()
    tags.add(TIT2(encoding=3, text="Night Drive"))
    tags.add(TPE1(encoding=3, text="Lumen"))
    tags.add(TALB(encoding=3, text="After Hours"))
    tags.add(APIC(encoding=3, mime="image/png", type=3,
                  data=Path(session.tracks[0].cover_path).read_bytes()))
    tags.save(sample)
    dialog = Mp3MetadataEditorDialog(window.translator, window)
    dialog.load_file(sample)
    dialog._file_label.setText(r"C:\Music\Night Drive.mp3")
    dialog.show()
    session.refresh()
    save(grab(dialog), session.language, "mp3_metadata")
    dialog.reject()
    dialog.deleteLater()
    settle(session.app)

    window.store.update(source.id, music_reactive_enabled=True)
    window.store.select(source.id)
    inspector = window.inspector
    inspector.property_tabs.setCurrentIndex(inspector._tab_indices["animation"])
    session.refresh()
    groups = [inspector._sections[("animation", key)] for key in ("music_reaction", "music_response")]
    # Stack the two real settings groups so every option is legible in the guide.
    captures = [grab(group) for group in groups]
    image = QImage(max(i.width() for i in captures), sum(i.height() for i in captures), QImage.Format.Format_RGB32)
    image.fill(QColor("#202427"))
    painter = QPainter(image)
    y = 0
    for capture in captures:
        painter.drawImage(0, y, capture)
        y += capture.height()
    painter.end()
    save(image, session.language, "canvas_music_reaction")
    window.bottom_tabs.setCurrentIndex(1)
    timed_source = next(s for s in window.store.sources() if s.source_type == SourceType.PROGRESS_BAR)
    window.store.update(timed_source.id, timeline_start=18.0, timeline_duration=65.0)
    window.store.select(timed_source.id)
    window.workspace_splitter.lock_edge_sizes({1: 470})
    window.workspace_splitter.setSizes([450, 470])
    session.refresh()
    window.timeline_panel.visual.verticalScrollBar().setValue(0)
    settle(session.app)
    save(grab(window.bottom_tabs), session.language, "canvas_timeline")


CAPTURES = (capture_canvas, capture_source_effects, capture_track, capture_lyrics_editor, capture_automix_editor,
            capture_preview, capture_dialogs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lang", choices=("ko", "en"), action="append",
                        help="language to capture (repeatable; default: both)")
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg"), help="ffmpeg executable")
    parser.add_argument("--only", action="append", choices=[c.__name__.removeprefix("capture_") for c in CAPTURES],
                        help="capture just this window (repeatable; default: all)")
    arguments = parser.parse_args()
    if not arguments.ffmpeg:
        parser.error("FFmpeg was not found on PATH; pass --ffmpeg")
    app = QApplication(sys.argv[:1])
    # Separate settings: the capture never touches the real user settings.
    app.setApplicationName("Music Motion Studio Help Capture")
    app.setOrganizationName("Music Motion Studio Help Capture")
    from app.ui.design_system import apply_studio_style

    apply_studio_style(app)
    for key in ("guides/welcome_seen", "guides/automix_editor_seen"):
        QSettings().setValue(key, True)
    with tempfile.TemporaryDirectory(prefix="pc-help-") as raw:
        folder = Path(raw) / "Music"
        folder.mkdir()
        songs = build_sample_media(folder)
        for language in arguments.lang or ["ko", "en"]:
            session = Session(app, language, songs, Path(arguments.ffmpeg),
                              analyze=not arguments.only or bool(set(arguments.only) - {"canvas", "source_effects"}))
            for capture in CAPTURES:
                if not arguments.only or capture.__name__.removeprefix("capture_") in arguments.only:
                    capture(session)
            session.close()
    from capture_recent_help_images import main as capture_recent
    capture_recent()
    QSettings().clear()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
