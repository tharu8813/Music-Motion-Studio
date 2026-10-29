"""Capture recently added help screens without network requests or user settings."""
from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QSettings
from app.ui.design_system import apply_studio_style
from app.utils.i18n import Translator, Language
from app.models.source import Source, SourceType
from app.models.playlist import PlaylistTrack
from app.services.source_store import SourceStore
from app.inspector.source_inspector import SourceInspector
from app.dialogs.lyrics_search_dialog import LyricsSearchDialog


def main():
    app = QApplication.instance() or QApplication([])
    app.setApplicationName('Playlist Canvas Help Capture')
    app.setOrganizationName('Playlist Canvas Help Capture')
    apply_studio_style(app)
    for language, code in ((Language.KOREAN, 'ko'), (Language.ENGLISH, 'en')):
        translator = Translator()
        translator.set_language(language)
        track = PlaylistTrack('', 'Paper Planes', artist='Mira Sol', album='Blue Hours')
        dialog = LyricsSearchDialog([track], translator, initial_track_id=track.id)
        dialog.show()
        app.processEvents()
        dialog.grab().save(str(ROOT / f'app/resources/help/{code}/lyrics_search.png'))
        dialog.close()
        store = SourceStore()
        inspector = SourceInspector(store, translator)
        inspector.resize(460, 980)
        store.add(Source(SourceType.LYRICS, 'Lyrics', subtitle_auto_wrap=True,
                         subtitle_wrap_to_source=False, subtitle_wrap_width=320))
        inspector.property_tabs.setCurrentIndex(inspector._tab_indices['special'])
        inspector.show()
        app.processEvents()
        inspector.grab().save(str(ROOT / f'app/resources/help/{code}/lyrics_wrapping.png'))
        inspector.close()
        inspector.deleteLater()
        dialog.deleteLater()
    QSettings().clear()


if __name__ == '__main__':
    main()
