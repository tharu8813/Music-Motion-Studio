"""Inspector panel for editing the active Source."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, QSettings, Signal
from PySide6.QtGui import QColor, QFontDatabase
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.inspector.editors import (
    album_cover_editor,
    audio_level_meter_editor,
    audio_visualizer_editor,
    audio_waveform_editor,
    background_editor,
    image_editor,
    logo_editor,
    lyrics_editor,
    now_playing_editor,
    particle_overlay_editor,
    progress_editor,
    shape_editor,
    text_editor,
    time_editor,
    track_list_editor,
    video_editor,
    watermark_editor,
)
from app.inspector.editors.audio_level_meter_editor import LevelMeterSection
from app.inspector.editors.audio_visualizer_editor import VisualizerSection
from app.animation.curves import ANIMATION_STYLES
from app.inspector.editors.base import MotionSection, TypographySection, shows_mask
from app.inspector.editors.lyrics_editor import LyricsSection
from app.inspector.editors.now_playing_editor import NowPlayingSection
from app.inspector.editors.particle_overlay_editor import ParticleSection
from app.inspector.editors.track_list_editor import TrackListSection
from app.models.source import TEXT_SOURCE_TYPES, Source, SourceType
from app.models.source_registry import source_registry
from app.dialogs.color_editor_dialog import ColorEditorDialog
from app.dialogs.lyrics_line_style_dialog import LyricsLineStyleDialog
from app.dialogs.lyrics_animation_dialog import LyricsAnimationDialog
from app.dialogs.text_editor_dialog import TextEditorDialog
from app.dialogs.video_source_dialog import VideoSourceDialog
from app.services.source_store import SourceStore
from app.utils.font_loader import load_application_font
from app.utils.i18n import Translator
from app.widgets.token_text_editor import TokenLineEdit


class _CollapsibleGroup(QWidget):
    """A titled, collapsible sub-section that holds a small form of fields.

    Used to break a crowded property tab (a visualizer or track list can expose
    a dozen-plus fields) into a few labelled, foldable groups.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("inspectorSection")
        self.field_keys: list[str] = []
        self._title = ""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 8, 0, 0)
        outer.setSpacing(0)
        self.header = QPushButton()
        self.header.setObjectName("inspectorSectionHeader")
        self.header.setCheckable(True)
        self.header.setChecked(True)
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header.toggled.connect(self._on_toggled)
        outer.addWidget(self.header)
        self._body = QWidget()
        self._body.setObjectName("inspectorSectionBody")
        self.form = QFormLayout(self._body)
        self.form.setContentsMargins(9, 8, 2, 2)
        self.form.setSpacing(7)
        outer.addWidget(self._body)

    def set_title(self, title: str) -> None:
        self._title = title
        self._sync_header()

    def add_field(self, label: QLabel, widget: QWidget) -> None:
        self.form.addRow(label, widget)

    def _on_toggled(self, expanded: bool) -> None:
        self._body.setVisible(expanded)
        self._sync_header()

    def _sync_header(self) -> None:
        self.header.setText(
            f"{'▾' if self.header.isChecked() else '▸'}  {self._title}"
        )


class SourceInspector(QScrollArea):
    """Editable property panel with guarded, two-way SourceStore binding."""

    animation_preview_requested = Signal(str)
    karaoke_requested = Signal()

    _ANIMATION_LABELS = {
        "none": ("없음", "None"), "fade": ("페이드", "Fade"),
        "slide_left": ("왼쪽 슬라이드", "Slide left"),
        "slide_right": ("오른쪽 슬라이드", "Slide right"),
        "slide_up": ("위쪽 슬라이드", "Slide up"),
        "slide_down": ("아래쪽 슬라이드", "Slide down"),
        "zoom": ("줌", "Zoom"), "zoom_out": ("줌 아웃", "Zoom out"),
        "pop": ("팝", "Pop"), "bounce": ("통통 튀기", "Bounce"),
        "rise": ("떠오르기", "Rise"), "drop": ("떨어지기", "Drop"),
        "rotate": ("회전", "Rotate"), "spin": ("스핀", "Spin"),
        "swing": ("흔들며 등장", "Swing"), "flip": ("뒤집기", "Flip"),
    }

    IMAGE_BACKED_TYPES = {
        SourceType.IMAGE,
        SourceType.VIDEO,
        SourceType.BACKGROUND,
        SourceType.ALBUM_COVER,
        SourceType.LOGO,
        SourceType.WATERMARK,
    }

    def __init__(self, store: SourceStore, translator: Translator,
                 parent: QWidget | None = None,
                 tracks_provider: Callable[[], list] | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self.translator = translator
        self._tracks_provider = tracks_provider
        self._updating = False
        self._applying_batch = False
        self._source_id: str | None = None
        self._source_ids: tuple[str, ...] = ()
        self._mixed_fields: set[str] = set()
        self._dirty_line_fields: set[str] = set()
        self._form_labels: dict[str, QLabel] = {}
        self._field_widgets: dict[str, QWidget] = {}
        self._field_hosts: dict[str, QWidget] = {}
        self._slider_hosts: dict[QWidget, QWidget] = {}
        self._linked_sliders: dict[QWidget, QSlider] = {}
        self._field_visibility: dict[str, bool] = {}
        self.setMinimumWidth(290)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setObjectName("sourceInspector")
        self._content = QWidget()
        self._content.setObjectName("inspectorContent")
        self._content.setMinimumWidth(0)
        self._content.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred,
        )
        self.setWidget(self._content)
        self.empty_state = QLabel(self.viewport())
        self.empty_state.setObjectName("inspectorEmptyState")
        self.empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_state.setWordWrap(True)
        self.empty_state.setContentsMargins(28, 28, 28, 28)
        self.empty_state.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout = QVBoxLayout(self._content)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)
        self.title = QLabel("Inspector")
        self.title.setObjectName("panelTitle")
        self.subtitle = QLabel("Select an object to edit its properties.")
        self.subtitle.setObjectName("mutedLabel")
        self.subtitle.setWordWrap(True)
        layout.addWidget(self.title)
        layout.addWidget(self.subtitle)
        self.lyrics_mode_note = QLabel()
        self.lyrics_mode_note.setObjectName("mutedLabel")
        self.lyrics_mode_note.setWordWrap(True)
        self.lyrics_mode_note.hide()
        self._lyrics_locked_fields = set()
        layout.addWidget(self.lyrics_mode_note)

        self.property_tabs = QTabWidget()
        self.property_tabs.setObjectName("inspectorPropertyTabs")
        self.property_tabs.setDocumentMode(True)
        self.property_tabs.tabBar().setExpanding(False)
        self.property_tabs.tabBar().setUsesScrollButtons(True)
        layout.addWidget(self.property_tabs, 1)

        def property_page(name: str) -> tuple[QWidget, QFormLayout]:
            page = QWidget()
            page.setObjectName(f"inspector{name.title()}Page")
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(8, 12, 8, 14)
            page_layout.setSpacing(0)
            form = QFormLayout()
            page_layout.addLayout(form)
            page_layout.addStretch()
            return page, form

        self.content_group, content_form = property_page("special")
        self.transform_group, transform = property_page("layout")
        self.text_group, text_form = property_page("text")
        self.appearance_group, shape_form = property_page("shape")
        self.fill_group, fill_form = property_page("fill")
        self.filter_group, filter_form = property_page("filter")
        self.animation_group, animation_form = property_page("animation")
        self.other_group, other_form = property_page("other")
        self._category_pages = {
            "special": self.content_group,
            "layout": self.transform_group,
            "text": self.text_group,
            "shape": self.appearance_group,
            "fill": self.fill_group,
            "filter": self.filter_group,
            "animation": self.animation_group,
            "other": self.other_group,
        }
        self._category_forms = {
            "special": content_form,
            "layout": transform,
            "text": text_form,
            "shape": shape_form,
            "fill": fill_form,
            "filter": filter_form,
            "animation": animation_form,
            "other": other_form,
        }
        self._field_categories: dict[str, str] = {}
        self._field_sections: dict[str, tuple[str, str]] = {}
        self._sections: dict[tuple[str, str], _CollapsibleGroup] = {}
        self._tab_indices = {
            category: self.property_tabs.addTab(page, "")
            for category, page in self._category_pages.items()
        }
        saved_tab = QSettings().value("inspector/property_tab", 0, type=int)
        self.property_tabs.setCurrentIndex(
            max(0, min(self.property_tabs.count() - 1, int(saved_tab)))
        )
        self.property_tabs.currentChanged.connect(self._remember_property_tab)

        self.name_edit = QLineEdit()
        self.text_edit = TokenLineEdit(translator)
        self.expand_text_button = QPushButton()
        self.expand_text_button.setObjectName("compactButton")
        text_row = QWidget()
        text_layout = QHBoxLayout(text_row)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(5)
        text_layout.addWidget(self.text_edit, 1)
        text_layout.addWidget(self.expand_text_button)
        self.file_path_edit = QLineEdit()
        self.file_path_edit.setReadOnly(True)
        self.file_button = QPushButton()
        self.clear_file_button = QPushButton()
        file_row = QWidget()
        file_layout = QHBoxLayout(file_row)
        file_layout.setContentsMargins(0, 0, 0, 0)
        file_layout.setSpacing(5)
        file_layout.addWidget(self.file_path_edit, 1)
        file_layout.addWidget(self.file_button)
        file_layout.addWidget(self.clear_file_button)
        self._add_labeled_row(transform, "name", self.name_edit)
        self._add_labeled_row(text_form, "text", text_row)
        self._add_labeled_row(content_form, "file", file_row)
        self.video_settings_button = QPushButton()
        self._add_labeled_row(content_form, "video_settings", self.video_settings_button)

        self.shape_kind_combo = QComboBox()
        for label, value in (("Rectangle", "rectangle"), ("Circle", "circle")):
            self.shape_kind_combo.addItem(label, value)
        self.progress_style_combo = QComboBox()
        for label, value in (
            ("Rounded", "rounded"), ("Spotify", "spotify"), ("Apple Music", "apple"),
            ("YouTube", "youtube"), ("Gradient", "gradient"),
        ):
            self.progress_style_combo.addItem(label, value)
        self.visualizer = VisualizerSection(self._spin)
        self.text_alignment_combo = QComboBox()
        for label, value in (("Left", "left"), ("Center", "center"), ("Right", "right")):
            self.text_alignment_combo.addItem(label, value)
        self.text_overflow_combo = QComboBox()
        for label, value in (
            ("Automatic wrap", "wrap"),
            ("Ellipsis (…)", "ellipsis"),
            ("Clip", "clip"),
        ):
            self.text_overflow_combo.addItem(label, value)
        self.image_fit_combo = QComboBox()
        for label, value in (("Cover", "cover"), ("Contain", "contain"), ("Stretch", "stretch")):
            self.image_fit_combo.addItem(label, value)
        self.background_mode_combo = QComboBox()
        for label, value in (("Color / gradient", "color"), ("Image", "image"),
                             ("Current album cover", "album_art")):
            self.background_mode_combo.addItem(label, value)
        self.background_ambient_check = QCheckBox()
        self.background_ambient_blur_spin = self._spin(0, 80, 1)
        self.background_ambient_motion_spin = self._spin(0, 3, 0.1)
        self.background_bass_reactive_check = QCheckBox()
        self.background_bass_strength_spin = self._spin(0, 0.2, 0.01)
        self.background_track_transition_check = QCheckBox()
        self.background_track_transition_seconds_spin = self._spin(0.2, 3.0, 0.1)
        self.progress_value_spin = self._spin(0, 1, 0.01)
        self.progress_track_color_button = self._color_button()
        self.progress_mode_combo = QComboBox()
        for label, value in (("Current track", "track"), ("Whole video", "video")):
            self.progress_mode_combo.addItem(label, value)
        self.progress_knob_combo = QComboBox()
        for label, value in (("Style default", "auto"), ("None", "none"),
                             ("Circle", "circle"), ("Bar", "bar")):
            self.progress_knob_combo.addItem(label, value)
        self.album_frame_combo = QComboBox()
        for label, value in (("Rounded", "rounded"), ("Circle", "circle"), ("Polaroid", "polaroid"), ("Glass", "glass")):
            self.album_frame_combo.addItem(label, value)
        self.track_list = TrackListSection(self._spin, self._color_button)
        self.now_playing = NowPlayingSection(self._spin)
        self.lyrics = LyricsSection(self._spin, self._color_button)
        self.lyrics_line_styles_button = QPushButton()
        self.lyrics_transition_button = QPushButton()
        self.lyrics_karaoke_button = QPushButton()
        self.lyrics_karaoke_button.clicked.connect(lambda _checked=False: self.karaoke_requested.emit())
        self.lyrics_animation_button = QPushButton()
        self.typography = TypographySection(self._spin)
        self.motion = MotionSection(self._spin)
        self.visualizer_center_cover_button = QPushButton()
        self.waveform_style_combo = QComboBox()
        for label, value in (("Line", "line"), ("Filled", "filled"), ("Mirror", "mirror")):
            self.waveform_style_combo.addItem(label, value)
        self.level_meter = LevelMeterSection(self._spin, self._color_button)
        self.particle = ParticleSection(self._spin, self._color_button)
        self._add_labeled_row(content_form, "shape", self.shape_kind_combo)
        self._add_labeled_row(content_form, "progress_style", self.progress_style_combo)
        self.visualizer.add_rows(self._add_labeled_row, content_form, VisualizerSection.EARLY_KEYS)
        self._add_labeled_row(text_form, "text_alignment", self.text_alignment_combo)
        self._add_labeled_row(text_form, "text_overflow", self.text_overflow_combo)
        self._add_labeled_row(content_form, "image_fit", self.image_fit_combo)
        self._add_labeled_row(content_form, "background_mode", self.background_mode_combo)
        self._add_labeled_row(content_form, "background_ambient", self.background_ambient_check)
        self._add_labeled_row(
            content_form, "background_ambient_blur", self.background_ambient_blur_spin,
        )
        self._add_labeled_row(
            content_form, "background_ambient_motion", self.background_ambient_motion_spin,
        )
        self._add_labeled_row(
            content_form, "background_bass_reactive", self.background_bass_reactive_check,
        )
        self._add_labeled_row(
            content_form, "background_bass_strength", self.background_bass_strength_spin,
        )
        self._add_labeled_row(
            content_form, "background_track_transition",
            self.background_track_transition_check,
        )
        self._add_labeled_row(
            content_form, "background_track_transition_seconds",
            self.background_track_transition_seconds_spin,
        )
        self._add_labeled_row(content_form, "progress_value", self.progress_value_spin)
        self._add_labeled_row(content_form, "progress_track_color", self.progress_track_color_button)
        self._add_labeled_row(content_form, "progress_mode", self.progress_mode_combo)
        self._add_labeled_row(content_form, "progress_knob", self.progress_knob_combo)
        self.visualizer.add_rows(self._add_labeled_row, content_form, VisualizerSection.LATE_KEYS)
        self._add_labeled_row(
            content_form, "visualizer_center_cover", self.visualizer_center_cover_button,
        )
        self._add_labeled_row(content_form, "album_frame", self.album_frame_combo)
        self.track_list.add_rows(self._add_labeled_row, content_form)
        self.now_playing.add_rows(self._add_labeled_row, content_form)
        content_form.addRow(self.lyrics_transition_button)
        self.lyrics.add_rows(
            self._add_labeled_row, content_form,
            keys=tuple(key for key in self.lyrics.widgets if key not in LyricsSection.TRANSITION_KEYS),
        )
        self._sections[("special", "sub_karaoke")].form.addRow(self.lyrics_karaoke_button)
        self._add_labeled_row(
            content_form, "subtitle_line_styles", self.lyrics_line_styles_button,
            section="sub_layout",
        )
        self._add_labeled_row(content_form, "waveform_style", self.waveform_style_combo)
        self.level_meter.add_rows(self._add_labeled_row, content_form)
        self.particle.add_rows(self._add_labeled_row, content_form)
        self.x_spin = self._spin(-5000, 5000, 1)
        self.y_spin = self._spin(-5000, 5000, 1)
        self.width_spin = self._spin(32, 5000, 1)
        self.height_spin = self._spin(24, 5000, 1)
        self.rotation_spin = self._spin(-360, 360, 1)
        self.scale_spin = self._spin(0.1, 10, 0.05)
        self._slider_spin_editor(self.width_spin, slider_maximum=1920)
        self._slider_spin_editor(self.height_spin, slider_maximum=1080)
        self._slider_spin_editor(self.rotation_spin)
        self._slider_spin_editor(self.scale_spin, slider_maximum=3.0)
        for key, widget in (
            ("x", self.x_spin), ("y", self.y_spin), ("width", self.width_spin),
            ("height", self.height_spin), ("rotation", self.rotation_spin),
            ("scale", self.scale_spin),
        ):
            self._add_labeled_row(transform, key, widget)
        self.opacity_spin = self._spin(0, 1, 0.05)
        self.radius_spin = self._spin(0, 300, 1)
        self.outline_spin = self._spin(0, 40, 1)
        self.font_size_spin = self._spin(8, 120, 1)
        self._slider_spin_editor(self.opacity_spin)
        self._slider_spin_editor(self.radius_spin, slider_maximum=100)
        self._slider_spin_editor(self.font_size_spin)
        self.font_weight_combo = QComboBox()
        for label, value in (
            ("Light · 300", 300), ("Regular · 400", 400),
            ("Medium · 500", 500), ("Semi bold · 600", 600),
            ("Bold · 700", 700), ("Extra bold · 800", 800),
            ("Black · 900", 900),
        ):
            self.font_weight_combo.addItem(label, value)
        self.font_family_combo = QComboBox()
        self.font_family_combo.setEditable(True)
        self.font_family_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.font_family_combo.addItems(sorted(QFontDatabase.families(), key=str.casefold))
        self.font_add_button = QPushButton()
        font_row = QWidget()
        font_layout = QHBoxLayout(font_row)
        font_layout.setContentsMargins(0, 0, 0, 0)
        font_layout.setSpacing(5)
        font_layout.addWidget(self.font_family_combo, 1)
        font_layout.addWidget(self.font_add_button)
        self.fill_color_button = self._color_button()
        self.outline_color_button = self._color_button()
        self.text_color_button = self._color_button()
        self.text_stroke_color_button = self._color_button()
        self.text_stroke_width_spin = self._spin(0, 12, 0.5)
        self.gradient_check = QCheckBox()
        self.gradient_start_button = self._color_button()
        self.gradient_end_button = self._color_button()
        self.gradient_angle_spin = self._spin(-180, 180, 5)
        self._slider_spin_editor(self.gradient_angle_spin)
        self.blur_spin = self._spin(0, 40, 1)
        self.brightness_spin = self._spin(-100, 100, 1)
        self.contrast_spin = self._spin(-100, 100, 1)
        self._slider_spin_editor(self.blur_spin)
        self._slider_spin_editor(self.brightness_spin)
        self._slider_spin_editor(self.contrast_spin)
        self.shadow_check = QCheckBox()
        self.shadow_color_button = self._color_button()
        self.shadow_opacity_spin = self._spin(0, 1, 0.05)
        self.shadow_blur_spin = self._spin(0, 50, 1)
        self.shadow_x_spin = self._spin(-100, 100, 1)
        self.shadow_y_spin = self._spin(-100, 100, 1)
        self._slider_spin_editor(self.shadow_opacity_spin)
        self._slider_spin_editor(self.shadow_blur_spin)
        self.animation_in_combo = QComboBox()
        self.animation_out_combo = QComboBox()
        for combo in (self.animation_in_combo, self.animation_out_combo):
            for value in ANIMATION_STYLES:
                combo.addItem(self._ANIMATION_LABELS[value][1], value)
        self.animation_in_duration_spin = self._spin(0.1, 3, 0.05)
        self.animation_out_duration_spin = self._spin(0.1, 3, 0.05)
        # Crossfade/AutoMix only: stretch the entrance/exit over the audio mix
        # where the Canvas changes track (see preview.frame_state.mix_animation).
        self._mix_transitions_active = False
        self.animation_fit_mix_check = QCheckBox()
        self.animation_preview_button = QPushButton()
        self.animation_preview_button.setObjectName("primaryButton")
        self.z_spin = QSpinBox()
        self.z_spin.setRange(-100, 100)
        self.visible_check = QCheckBox()
        self.locked_check = QCheckBox()
        for key, widget in (
            ("font_size", self.font_size_spin), ("font_weight", self.font_weight_combo),
            ("font_family", font_row),
            ("text_color", self.text_color_button),
            ("text_stroke_color", self.text_stroke_color_button),
            ("text_stroke_width", self.text_stroke_width_spin),
        ):
            self._add_labeled_row(text_form, key, widget)
        self.typography.add_rows(self._add_labeled_row, text_form, TypographySection.TEXT_KEYS)
        for key, widget in (
            ("opacity", self.opacity_spin),
            ("shadow", self.shadow_check), ("shadow_color", self.shadow_color_button),
            ("shadow_opacity", self.shadow_opacity_spin), ("shadow_blur", self.shadow_blur_spin),
            ("shadow_x", self.shadow_x_spin), ("shadow_y", self.shadow_y_spin),
        ):
            self._add_labeled_row(shape_form, key, widget)
        self.typography.add_rows(self._add_labeled_row, shape_form, ("text_shadow_glyph",))
        self.motion.add_rows(self._add_labeled_row, shape_form, ("mask_shape",))
        for key, widget in (
            ("border_radius", self.radius_spin), ("outline", self.outline_spin),
            ("fill_color", self.fill_color_button),
            ("outline_color", self.outline_color_button),
            ("gradient", self.gradient_check),
            ("gradient_start", self.gradient_start_button),
            ("gradient_end", self.gradient_end_button),
            ("gradient_angle", self.gradient_angle_spin),
        ):
            self._add_labeled_row(fill_form, key, widget)
        self.typography.add_rows(self._add_labeled_row, fill_form, ("text_gradient",))
        for key, widget in (
            ("blur", self.blur_spin), ("brightness", self.brightness_spin),
            ("contrast", self.contrast_spin),
        ):
            self._add_labeled_row(filter_form, key, widget)
        self.lyrics.add_rows(self._add_labeled_row, animation_form, LyricsSection.TRANSITION_KEYS)
        self._sections[("animation", "sub_transition")].form.addRow(self.lyrics_animation_button)
        for key, widget in (
            ("animation_in", self.animation_in_combo),
            ("animation_in_duration", self.animation_in_duration_spin),
            ("animation_out", self.animation_out_combo),
            ("animation_out_duration", self.animation_out_duration_spin),
            ("animation_fit_mix", self.animation_fit_mix_check),
        ):
            self._add_labeled_row(animation_form, key, widget)
        self.motion.add_rows(self._add_labeled_row, animation_form, MotionSection.LOOP_KEYS)
        self.motion.add_rows(self._add_labeled_row, animation_form, MotionSection.REACTIVE_KEYS)
        animation_form.addRow("", self.animation_preview_button)
        self._add_labeled_row(other_form, "layer", self.z_spin)
        other_form.addRow(self.visible_check)
        other_form.addRow(self.locked_check)

        self._editors = [
            self.name_edit, self.text_edit, self.expand_text_button,
            self.file_path_edit, self.file_button,
            self.clear_file_button, self.shape_kind_combo, self.progress_style_combo,
            *(self.visualizer.widgets[key] for key in VisualizerSection.EARLY_KEYS),
            self.x_spin, self.y_spin,
            self.text_alignment_combo, self.text_overflow_combo,
            self.image_fit_combo, self.progress_value_spin,
            self.progress_mode_combo, self.progress_knob_combo,
            *self.typography.widgets.values(), self.gradient_angle_spin,
            *self.motion.widgets.values(), self.visualizer_center_cover_button,
            self.background_mode_combo, self.background_ambient_check,
            self.background_ambient_blur_spin, self.background_ambient_motion_spin,
            self.background_bass_reactive_check, self.background_bass_strength_spin,
            self.background_track_transition_check,
            self.background_track_transition_seconds_spin,
            self.progress_track_color_button,
            *(widget for key, widget in self.visualizer.widgets.items()
              if key not in VisualizerSection.EARLY_KEYS),
            self.album_frame_combo,
            *self.track_list.widgets.values(),
            *self.now_playing.widgets.values(),
            *self.lyrics.widgets.values(),
            self.lyrics_transition_button,
            self.lyrics_animation_button,
            self.waveform_style_combo,
            *self.level_meter.widgets.values(),
            *self.particle.widgets.values(),
            self.width_spin, self.height_spin, self.rotation_spin, self.scale_spin,
            self.opacity_spin, self.radius_spin, self.outline_spin, self.font_size_spin,
            self.font_weight_combo,
            self.font_family_combo, self.font_add_button,
            self.fill_color_button, self.outline_color_button, self.text_color_button,
            self.text_stroke_color_button, self.text_stroke_width_spin,
            self.gradient_check,
            self.gradient_start_button, self.gradient_end_button, self.z_spin,
            self.blur_spin, self.brightness_spin, self.contrast_spin, self.shadow_check,
            self.shadow_color_button, self.shadow_opacity_spin, self.shadow_blur_spin,
            self.shadow_x_spin, self.shadow_y_spin,
            self.animation_in_combo, self.animation_in_duration_spin,
            self.animation_out_combo, self.animation_out_duration_spin,
            self.animation_fit_mix_check, self.animation_preview_button,
            self.visible_check, self.locked_check,
        ]
        self._editors.extend(self._linked_sliders.values())
        for form in self._content.findChildren(QFormLayout):
            form.setFieldGrowthPolicy(
                QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
            )
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            form.setLabelAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
            form.setHorizontalSpacing(10)
            form.setVerticalSpacing(7)
        for widget in self._field_widgets.values():
            widget.setMinimumWidth(0)
            if widget in self._slider_hosts:
                continue
            if widget.sizePolicy().horizontalPolicy() in {
                QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum,
            }:
                widget.setSizePolicy(
                    QSizePolicy.Policy.Expanding,
                    widget.sizePolicy().verticalPolicy(),
                )
        self._connect_fields()
        self._set_enabled(False)
        self._update_source_specific_fields(None)
        self._show_empty_state(True)
        store.selection_set_changed.connect(self.set_sources)
        store.source_changed.connect(self.refresh_source)
        translator.language_changed.connect(self.retranslate)
        self.retranslate()

    def _add_labeled_row(
        self, layout: QFormLayout, key: str, widget: QWidget,
        section: str | None = None,
    ) -> None:
        label = QLabel()
        self._form_labels[key] = label
        self._field_widgets[key] = widget
        host = self._slider_hosts.get(widget, widget)
        self._field_hosts[key] = host
        self._field_visibility[key] = True
        category = None
        for cat, category_layout in getattr(self, "_category_forms", {}).items():
            if layout is category_layout:
                category = cat
                self._field_categories[key] = cat
                break
        if section and category:
            group = self._sections.get((category, section))
            if group is None:
                group = _CollapsibleGroup()
                self._sections[(category, section)] = group
                layout.addRow(group)
            group.field_keys.append(key)
            self._field_sections[key] = (category, section)
            group.add_field(label, host)
        else:
            layout.addRow(label, host)

    def _refresh_sections(self) -> None:
        """Fold away sub-sections whose fields are all hidden for this source."""
        for group in self._sections.values():
            group.setVisible(any(
                self._field_visibility.get(key, False)
                for key in group.field_keys
            ))

    @staticmethod
    def _remember_property_tab(index: int) -> None:
        settings = QSettings()
        settings.setValue("inspector/property_tab", index)
        settings.sync()

    def _property_help_text(self, key: str) -> str:
        """Return localized, user-facing guidance for one Inspector property."""
        korean = self.translator.is_korean
        common = {
            "name": ("레이어와 캔버스에서 이 요소를 구분하는 이름입니다. 영상에는 직접 표시되지 않습니다.", "Identifies this source in Layers and on the Canvas. It is not rendered into the video."),
            "text": ("표시할 문구입니다. %title%, %artist%, %album% 같은 토큰은 재생 중인 곡 정보로 자동 교체됩니다.", "Text to display. Tokens such as %title%, %artist%, and %album% are replaced with current-track data."),
            "file": ("이 요소에 사용할 이미지 파일입니다. 프로젝트를 다른 PC로 옮길 때는 포함 저장을 권장합니다.", "Image used by this source. Embedded project storage is recommended when moving the project to another PC."),
            "shape": ("캔버스에 그릴 사각형 또는 원을 선택합니다.", "Chooses whether this source is drawn as a rectangle or circle."),
            "progress_style": ("진행 바의 모서리와 채움 형태를 미리 정의된 디자인으로 변경합니다.", "Changes the progress bar's corners and fill treatment using a preset design."),
            "text_alignment": ("요소 영역 안에서 텍스트를 왼쪽, 가운데 또는 오른쪽으로 정렬합니다.", "Aligns text left, center, or right inside the source box."),
            "text_overflow": ("영역보다 긴 문장을 줄바꿈할지, 말줄임표로 줄일지, 영역 밖을 자를지 정합니다.", "Chooses whether long text wraps, ends with an ellipsis, or is clipped to the source box."),
            "image_fit": ("원본 비율을 유지하며 채우기, 전체 이미지 맞추기 또는 영역에 늘이기 중 하나를 선택합니다.", "Chooses cover, contain, or stretch behavior for the image inside its source box."),
            "background_mode": ("단색·그라데이션, 지정 이미지 또는 현재 앨범 커버를 배경으로 사용합니다.", "Uses a color/gradient, selected image, or current album artwork as the background."),
            "background_ambient": ("앨범 커버를 확대하고 흐리게 처리해 캔버스를 채우는 앰비언트 배경을 만듭니다.", "Expands and blurs album artwork to create an ambient full-Canvas background."),
            "background_ambient_blur": ("앨범 색상이 퍼지는 정도입니다. 값이 클수록 더 부드럽고 추상적으로 보입니다.", "Controls how broadly the album colors spread. Larger values look softer and more abstract."),
            "background_ambient_motion": ("앰비언트 색상 흐름의 속도입니다. 0이면 멈춥니다.", "Speed of the ambient color flow. Set to 0 to freeze it."),
            "background_bass_reactive": ("현재 곡의 저음에 맞춰 배경을 부드럽게 확대합니다. 전체 미리보기와 내보내기에 적용됩니다.", "Gently zooms the background with the current track's bass in Full Preview and export."),
            "background_bass_strength": ("베이스가 가장 강할 때 추가되는 확대 비율입니다.", "Extra zoom applied at the strongest bass hit."),
            "background_track_transition": ("곡이 바뀔 때 이전 곡 배경에서 새 곡 배경으로 부드럽게 크로스페이드합니다. 편집 화면에는 나타나지 않고 미리보기와 내보내기에서만 적용됩니다.", "Cross-fades from the previous track's background to the new one at each track change. It appears only in preview and export, not on the editing canvas."),
            "background_track_transition_seconds": ("배경 크로스페이드가 진행되는 시간(초)입니다.", "How long the background cross-fade lasts, in seconds."),
            "progress_value": ("편집 화면에서 확인할 진행 비율입니다. 실제 미리보기와 내보내기에서는 재생 시간으로 자동 계산됩니다.", "Preview progress used while editing. Playback and export calculate it automatically from time."),
            "progress_track_color": ("아직 재생되지 않은 진행 바 뒷부분의 색상입니다.", "Color of the unplayed track behind the filled progress portion."),
            "progress_mode": ("현재 곡의 진행 시간 또는 전체 영상의 진행 시간을 기준으로 채웁니다.", "Fills according to either current-track time or complete-video time."),
            "album_frame": ("앨범 커버의 잘림 형태와 테두리 느낌을 선택합니다.", "Chooses the crop shape and frame treatment for album artwork."),
            "waveform_style": ("파형을 선, 채운 면 또는 위아래 대칭 형태로 표시합니다.", "Displays the waveform as a line, filled area, or mirrored shape."),
            "x": ("캔버스 왼쪽에서 요소 왼쪽 가장자리까지의 가로 위치입니다. 값이 커지면 오른쪽으로 이동합니다.", "Horizontal position from the Canvas left edge. Larger values move the source right."),
            "y": ("캔버스 위쪽에서 요소 위쪽 가장자리까지의 세로 위치입니다. 값이 커지면 아래로 이동합니다.", "Vertical position from the Canvas top edge. Larger values move the source down."),
            "width": ("배율 적용 전 요소 영역의 너비입니다. 이미지와 텍스트의 배치 영역에도 영향을 줍니다.", "Source-box width before scale is applied. It also affects image and text layout."),
            "height": ("배율 적용 전 요소 영역의 높이입니다. 이미지와 텍스트의 배치 영역에도 영향을 줍니다.", "Source-box height before scale is applied. It also affects image and text layout."),
            "rotation": ("요소를 시계 방향으로 회전하는 각도입니다. 음수 값은 반시계 방향입니다.", "Clockwise rotation in degrees. Negative values rotate counter-clockwise."),
            "scale": ("너비와 높이를 함께 확대하거나 축소하는 배율입니다. 1은 원래 크기입니다.", "Uniformly enlarges or shrinks width and height. A value of 1 is the original size."),
            "opacity": ("요소 전체의 불투명도입니다. 0은 완전히 투명하고 1은 완전히 보입니다.", "Overall source opacity. 0 is fully transparent and 1 is fully visible."),
            "border_radius": ("사각형 모서리를 둥글게 만드는 반경입니다. 값이 클수록 더 둥글어집니다.", "Rounds rectangular corners. Larger values produce rounder corners."),
            "outline": ("요소 가장자리에 그리는 윤곽선의 두께입니다. 0이면 표시하지 않습니다.", "Width of the outline drawn around the source. Set to 0 to hide it."),
            "font_size": ("텍스트의 기준 글꼴 크기입니다. 요소 크기와 배율은 별도로 적용됩니다.", "Base text size. Source dimensions and scale are applied separately."),
            "font_weight": ("글자의 굵기를 가늘게부터 매우 굵게까지 선택합니다. 글꼴이 지원하지 않는 굵기는 가장 가까운 굵기로 표시될 수 있습니다.", "Selects the glyph weight from light to black. Fonts without an exact weight may use the nearest available weight."),
            "font_family": ("텍스트에 사용할 글꼴입니다. 글꼴 추가 버튼으로 TTF 또는 OTF 파일을 등록할 수 있습니다.", "Font used for text. Add Font can register a TTF or OTF file."),
            "fill_color": ("도형, 텍스트 또는 효과의 주 색상입니다. 색상 창에서 알파를 0으로 설정하면 요소 전체 투명도는 유지하면서 배경만 완전히 투명하게 만들 수 있습니다.", "Primary fill or background color. Set alpha to 0 in the color dialog to make the background fully transparent without changing overall source opacity."),
            "text_color": ("글자에 직접 적용되는 기본 색상입니다. 색상 창에서 현재 곡의 퍼스널 컬러와 밝기·채도·색조 보정도 함께 설정할 수 있습니다.", "The primary color applied directly to the text. The color dialog also supports the current track's personal color with brightness, saturation, and hue adjustments."),
            "outline_color": ("윤곽선에 사용할 색상입니다. 윤곽선 두께가 0보다 클 때 보입니다.", "Outline color, visible when outline width is greater than zero."),
            "text_stroke_color": ("글자 자체에 두르는 테두리 색상입니다. 테두리 두께가 0보다 클 때 보입니다.", "Colour of the outline drawn around the glyphs, visible when the text outline width is greater than zero."),
            "text_stroke_width": ("글자 둘레에 그리는 테두리 두께(px)입니다. 0이면 테두리가 없습니다.", "Thickness in pixels of the outline drawn around each glyph. 0 disables it."),
            "gradient": ("단색 대신 시작 색과 끝 색이 이어지는 그라데이션 채우기를 사용합니다.", "Uses a blend between start and end colors instead of a solid fill."),
            "gradient_start": ("그라데이션이 시작되는 쪽의 색상입니다.", "Color at the start of the gradient."),
            "gradient_end": ("그라데이션이 끝나는 쪽의 색상입니다.", "Color at the end of the gradient."),
            "gradient_angle": ("그라데이션 방향을 기본 대각선(왼쪽 위→오른쪽 아래)에서 시계 방향으로 회전합니다.", "Rotates the gradient clockwise from its default top-left to bottom-right diagonal."),
            "visualizer_center_cover": ("원형 비주얼라이저 안쪽 원에 맞춘 원형 앨범 커버를 추가하고, 비주얼라이저와 한 그룹으로 묶습니다.", "Adds a circular album cover sized to the radial visualizer's inner ring and groups the two together."),
            **MotionSection.HELP,
            "progress_knob": ("진행 위치에 표시할 핸들입니다. 원이나 막대를 고르면 트랙이 얇아지고 핸들이 강조됩니다.", "Handle drawn at the playback position. Circle or bar slims the track so the handle stands out."),
            "blur": ("이미지를 부드럽게 흐립니다. 높은 값은 미리보기와 렌더링 부하를 늘릴 수 있습니다.", "Softens the image. High values can increase preview and rendering cost."),
            "brightness": ("이미지를 어둡게 또는 밝게 보정합니다. 0은 원본 밝기입니다.", "Darkens or brightens the image. 0 preserves original brightness."),
            "contrast": ("밝고 어두운 영역의 차이를 줄이거나 강조합니다. 0은 원본 대비입니다.", "Reduces or emphasizes differences between light and dark areas. 0 preserves the original."),
            "shadow": ("요소 뒤에 그림자를 표시해 배경과 분리된 깊이감을 만듭니다.", "Draws a shadow behind the source to separate it from the background."),
            "shadow_color": ("그림자에 사용할 색상입니다.", "Color used for the shadow."),
            "shadow_opacity": ("그림자의 불투명도입니다. 값이 작을수록 은은해집니다.", "Shadow opacity. Smaller values make it subtler."),
            "shadow_blur": ("그림자 가장자리의 퍼짐 정도입니다. 값이 클수록 부드럽고 넓게 퍼집니다.", "Softness and spread of shadow edges. Larger values make a wider, softer shadow."),
            "shadow_x": ("그림자를 가로로 이동합니다. 양수는 오른쪽, 음수는 왼쪽입니다.", "Horizontal shadow offset. Positive moves right; negative moves left."),
            "shadow_y": ("그림자를 세로로 이동합니다. 양수는 아래, 음수는 위입니다.", "Vertical shadow offset. Positive moves down; negative moves up."),
            "animation_in": ("곡에서 이 요소가 나타날 때 재생할 등장 효과입니다.", "Entrance effect played when this source appears during a track."),
            "animation_out": ("곡에서 이 요소가 사라질 때 재생할 종료 효과입니다.", "Exit effect played when this source disappears during a track."),
            "animation_in_duration": ("곡 시작 애니메이션이 재생되는 시간입니다.", "Duration of the track-start animation."),
            "animation_out_duration": ("곡 종료 애니메이션이 재생되는 시간입니다.", "Duration of the track-end animation."),
            "animation_fit_mix": (
                "크로스페이드·AutoMix에서는 두 곡이 겹치는 구간의 중간에 화면이 다음 곡으로 넘어갑니다. 켜면 종료 효과가 겹침 앞 절반, 등장 효과가 뒤 절반 동안 재생되어 전환 길이가 믹스 길이에 자동으로 맞춰집니다. 끄면 위의 애니메이션 시간을 씁니다.",
                "With crossfade/AutoMix the Canvas moves to the next track in the middle of the overlap. When on, the exit plays over the overlap's first half and the entrance over its second half, so the transition follows the mix length. When off, the durations above are used.",
            ),
            "layer": ("요소의 쌓임 순서입니다. 값이 큰 요소가 값이 작은 요소 위에 표시됩니다.", "Stacking order. Sources with larger values are drawn above sources with smaller values."),
        }
        if key in common:
            return common[key][0 if korean else 1]

        families = {
            "visualizer_": VisualizerSection.FAMILY,
            "track_list_": TrackListSection.FAMILY,
            "now_playing_": NowPlayingSection.FAMILY,
            "subtitle_": LyricsSection.FAMILY,
            "level_meter_": LevelMeterSection.FAMILY,
            "particle_": ParticleSection.FAMILY,
            "text_": TypographySection.FAMILY,
        }
        prefix = next((entry for entry in families if key.startswith(entry)), "")
        suffix = key[len(prefix):] if prefix else key
        details = {
            "style": ("표현 디자인을 선택합니다. 데이터와 타이밍은 유지되고 모양만 바뀝니다.", "Chooses the visual design while preserving data and timing."),
            # Shared by the visualizer and the level meter.
            "sensitivity": ("오디오 입력을 증폭하는 정도입니다. 값이 크면 작은 소리에도 크게 반응합니다.", "Audio-input gain. Larger values react more strongly to quiet sound."),
            "min_level": ("입력이 작거나 무음일 때 유지할 최소 표시 높이입니다.", "Minimum displayed level for quiet input or silence."),
            "max_level": ("가장 큰 입력에서 사용할 최대 표시 높이입니다.", "Maximum displayed level at the loudest input."),
            "attack": ("소리가 커질 때 표시가 상승하는 속도입니다. 높을수록 피크를 빠르게 따라갑니다.", "How quickly the display rises with louder sound. Higher values follow peaks faster."),
            "release": ("소리가 작아질 때 표시가 내려오는 속도입니다. 낮추면 움직임이 더 오래 남습니다.", "How quickly the display falls as sound gets quieter. Lower values linger longer."),
            **VisualizerSection.HELP,
            **TrackListSection.HELP,
            **NowPlayingSection.HELP,
            **LyricsSection.HELP,
            **LevelMeterSection.HELP,
            **ParticleSection.HELP,
            **TypographySection.HELP,
        }
        detail = details.get(suffix)
        if detail is None:
            return (
                "이 요소 전용 속성입니다. 값을 변경하면 캔버스와 미리보기에 즉시 반영됩니다."
                if korean else
                "This is a source-specific property. Changes appear immediately on the Canvas and in Preview."
            )
        family = families[prefix][0 if korean else 1] if prefix else ""
        explanation = detail[0 if korean else 1]
        return f"{family} 설정입니다. {explanation}" if korean else f"{family.capitalize()} setting. {explanation}"

    def _install_property_tooltips(self) -> None:
        """Apply rich, localized hover help to labels and their editor controls."""
        korean = self.translator.is_korean
        for key, label in self._form_labels.items():
            widget = self._field_widgets[key]
            description = self._property_help_text(key)
            note = self._field_notes.get(key, "")
            if note:
                description = f"{description} {note}"
            if key in self._lyrics_locked_fields:
                description += (" 고급 모드가 활성화되어 있으므로 속성 탭에서 설정할 수 없습니다. 자막 전환 설정에서 변경하세요."
                                if korean else " Advanced mode is active; this property cannot be edited here. Change it in Subtitle transition settings.")
            range_text = ""
            if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                minimum = f"{widget.minimum():g}"
                maximum = f"{widget.maximum():g}"
                step = f"{widget.singleStep():g}"
                range_text = (
                    f"<br><br><b>범위</b> {minimum}–{maximum} &nbsp;·&nbsp; <b>조절 단위</b> {step}"
                    if korean else
                    f"<br><br><b>Range</b> {minimum}–{maximum} &nbsp;·&nbsp; <b>Step</b> {step}"
                )
            tooltip = (
                f"<div style='width: 340px'><b>{label.text()}</b><br>"
                f"{description}{range_text}</div>"
            )
            label.setToolTip(tooltip)
            label.setToolTipDuration(15_000)
            widget.setToolTip(tooltip)
            widget.setToolTipDuration(15_000)
            widget.setAccessibleDescription(description)
            host = self._field_hosts[key]
            host.setToolTip(tooltip)
            host.setToolTipDuration(15_000)
            slider = self._linked_sliders.get(widget)
            if slider is not None:
                slider.setAccessibleName(label.text())
                slider.setAccessibleDescription(description)
                slider.setToolTip(tooltip)
                slider.setToolTipDuration(15_000)
            for child in widget.findChildren(QWidget):
                child.setToolTip(tooltip)
                child.setToolTipDuration(15_000)

        standalone = {
            self.visible_check: (
                "요소를 삭제하지 않고 캔버스와 최종 영상에서 숨깁니다."
                if korean else "Hides the source from the Canvas and final video without deleting it."
            ),
            self.locked_check: (
                "실수로 이동하거나 크기를 바꾸지 않도록 캔버스 편집을 잠급니다. 속성에서는 잠금을 해제할 수 있습니다."
                if korean else "Prevents accidental Canvas movement or resizing. It can still be unlocked here."
            ),
        }
        for widget, description in standalone.items():
            widget.setToolTip(f"<div style='width: 340px'><b>{widget.text()}</b><br>{description}</div>")
            widget.setToolTipDuration(15_000)
            widget.setAccessibleDescription(description)

    def _set_field_visible(self, key: str, visible: bool) -> None:
        self._field_visibility[key] = bool(visible)
        self._form_labels[key].setVisible(visible)
        self._field_hosts[key].setVisible(visible)

    @staticmethod
    def _uses_primary_text_color(source: Source) -> bool:
        return source.source_type in {
            SourceType.TEXT, SourceType.TIME, SourceType.LYRICS,
            SourceType.NOW_PLAYING,
        }

    def _source_type_display_name(self, source_type: SourceType | None) -> str:
        korean = self.translator.is_korean
        names = {
            SourceType.IMAGE: ("이미지", "Image"),
            SourceType.VIDEO: ("비디오", "Video"),
            SourceType.TEXT: ("텍스트", "Text"),
            SourceType.SHAPE: ("도형", "Shape"),
            SourceType.PROGRESS_BAR: ("진행 바", "Progress bar"),
            SourceType.TIME: ("시간", "Time"),
            SourceType.ALBUM_COVER: ("앨범 커버", "Album cover"),
            SourceType.LOGO: ("로고", "Logo"),
            SourceType.WATERMARK: ("워터마크", "Watermark"),
            SourceType.BACKGROUND: ("배경", "Background"),
            SourceType.AUDIO_VISUALIZER: ("오디오 비주얼라이저", "Audio visualizer"),
            SourceType.LYRICS: ("자막/가사", "Lyrics / subtitles"),
            SourceType.TRACK_LIST: ("트랙 목록", "Track list"),
            SourceType.NOW_PLAYING: ("현재 재생", "Now playing"),
            SourceType.AUDIO_WAVEFORM: ("오디오 파형", "Audio waveform"),
            SourceType.AUDIO_LEVEL_METER: ("오디오 레벨 미터", "Audio level meter"),
            SourceType.PARTICLE_OVERLAY: ("파티클/노이즈", "Particles / noise"),
        }
        if source_type is None:
            return "요소 전용" if korean else "Source"
        return names[source_type][0 if korean else 1]

    def _refresh_property_tabs(self, sources: list[Source]) -> None:
        """Show useful categories and name the special tab after its source."""
        self._refresh_sections()
        source_types = {source.source_type for source in sources}
        music_title = MotionSection.SECTION_TITLES["music_reaction"][0 if self.translator.is_korean else 1]
        if source_types == {SourceType.LYRICS}:
            music_title += " · 현재 자막만" if self.translator.is_korean else " · Current cue only"
        self._sections[("animation", "music_reaction")].set_title(music_title)
        self.lyrics_transition_button.setVisible(source_types == {SourceType.LYRICS})
        self.lyrics_karaoke_button.setVisible(source_types == {SourceType.LYRICS})
        if source_types == {SourceType.LYRICS}:
            karaoke = sources[0].subtitle_karaoke_enabled
            korean = self.translator.is_korean
            self._form_labels["subtitle_accent_color"].setText(
                ("부른 가사 색" if karaoke else "현재 줄 강조 색") if korean else
                ("Sung lyric color" if karaoke else "Current line accent color")
            )
        special_type = next(iter(source_types)) if len(source_types) == 1 else None
        self.property_tabs.setTabText(
            self._tab_indices["special"],
            self._source_type_display_name(special_type),
        )
        for category, page in self._category_pages.items():
            fields = [
                key for key, owner in self._field_categories.items()
                if owner == category
            ]
            has_visible_field = any(
                self._field_visibility.get(key, False) for key in fields
            )
            if category in {"animation", "other"}:
                has_visible_field = has_visible_field or bool(sources)
            self.property_tabs.setTabVisible(
                self._tab_indices[category], bool(sources) and has_visible_field,
            )
        if self.property_tabs.currentIndex() < 0 or not self.property_tabs.isTabVisible(
            self.property_tabs.currentIndex()
        ):
            for index in range(self.property_tabs.count()):
                if self.property_tabs.isTabVisible(index):
                    self.property_tabs.setCurrentIndex(index)
                    break

    def _update_source_specific_fields(self, source: Source | None) -> None:
        """Dispatch type-specific editing through the registered legacy adapter."""
        if source is None:
            self._update_legacy_source_specific_fields(None)
            return
        inspector = source_registry.get(source.source_type).inspector
        if inspector is None:
            raise RuntimeError(f"No inspector registered for {source.source_type.value}")
        inspector(self, source)

    def _update_legacy_source_specific_fields(self, source: Source | None) -> None:
        source_type = source.source_type if source else None
        is_background = source_type is SourceType.BACKGROUND
        text_types = {
            SourceType.TEXT, SourceType.TIME, SourceType.LYRICS,
            SourceType.TRACK_LIST, SourceType.NOW_PLAYING,
        }
        self._set_field_visible("text", source_type in text_types)
        self._set_field_visible("font_size", source_type in text_types)
        self._set_field_visible("font_weight", source_type in text_types)
        self._set_field_visible(
            "font_family",
            source_type in text_types,
        )
        self._set_field_visible("text_stroke_color", source_type in text_types)
        self._set_field_visible("text_stroke_width", source_type in text_types)
        for key in self.typography.widgets:
            self._set_field_visible(key, source_type in text_types)
        self._set_field_visible(
            "text_color",
            source is not None and self._uses_primary_text_color(source),
        )
        self._set_field_visible(
            "file", source_type in self.IMAGE_BACKED_TYPES
            and source_type is not SourceType.VIDEO
            and (not is_background or (source is not None and source.background_mode == "image"))
        )
        self._set_field_visible("shape", source_type is SourceType.SHAPE)
        self._set_field_visible("video_settings", source_type is SourceType.VIDEO)
        self._set_field_visible("progress_style", source_type is SourceType.PROGRESS_BAR)
        self._set_field_visible("visualizer_style", source_type is SourceType.AUDIO_VISUALIZER)
        self._set_field_visible("visualizer_bars", source_type is SourceType.AUDIO_VISUALIZER)
        self._set_field_visible("visualizer_line_width", source_type is SourceType.AUDIO_VISUALIZER)
        self._set_field_visible("visualizer_sensitivity", source_type is SourceType.AUDIO_VISUALIZER)
        self._set_field_visible("visualizer_reactivity", source_type is SourceType.AUDIO_VISUALIZER)
        for key in (
            "visualizer_noise_gate", "visualizer_min_level", "visualizer_max_level",
            "visualizer_attack", "visualizer_release", "visualizer_smoothing",
            "visualizer_curve", "visualizer_inner_radius", "visualizer_center_cover",
        ):
            self._set_field_visible(key, source_type is SourceType.AUDIO_VISUALIZER)
        self._set_field_visible("text_alignment", source_type in {SourceType.TEXT, SourceType.TIME, SourceType.LYRICS, SourceType.TRACK_LIST, SourceType.NOW_PLAYING})
        self._set_field_visible(
            "text_overflow", source_type in {SourceType.TEXT, SourceType.TRACK_LIST}
        )
        self._set_field_visible("image_fit", source_type in self.IMAGE_BACKED_TYPES)
        self._set_field_visible("background_mode", is_background)
        album_art_background = (
            is_background and source is not None
            and source.background_mode == "album_art"
        )
        self._set_field_visible("background_ambient", album_art_background)
        ambient_background = (
            album_art_background and source is not None and source.background_ambient
        )
        self._set_field_visible("background_ambient_blur", ambient_background)
        self._set_field_visible("background_ambient_motion", ambient_background)
        self._set_field_visible("background_bass_reactive", ambient_background)
        self._set_field_visible(
            "background_bass_strength",
            ambient_background and source is not None and source.background_bass_reactive,
        )
        self._set_field_visible(
            "background_track_transition", album_art_background,
        )
        self._set_field_visible(
            "background_track_transition_seconds",
            album_art_background and source is not None
            and source.background_track_transition,
        )
        self._set_field_visible("progress_value", source_type is SourceType.PROGRESS_BAR)
        self._set_field_visible("progress_track_color", source_type is SourceType.PROGRESS_BAR)
        self._set_field_visible("progress_mode", source_type is SourceType.PROGRESS_BAR)
        self._set_field_visible("progress_knob", source_type is SourceType.PROGRESS_BAR)
        self._set_field_visible("album_frame", source_type is SourceType.ALBUM_COVER)
        for key in (
            "track_list_count", "track_list_style", "track_list_window",
            "track_list_show_number", "track_list_show_artist",
            "track_list_show_album", "track_list_marker", "track_list_row_spacing",
            "track_list_item_padding", "track_list_current_color",
            "track_list_inactive_color", "track_list_current_background",
            "track_list_inactive_opacity", "track_list_current_scale",
            "track_list_show_dividers",
        ):
            self._set_field_visible(key, source_type is SourceType.TRACK_LIST)
        for key in self.now_playing.widgets:
            self._set_field_visible(key, source_type is SourceType.NOW_PLAYING)
        for key in self.lyrics.widgets:
            self._set_field_visible(key, source_type is SourceType.LYRICS)
        self._set_field_visible(
            "subtitle_line_styles", source_type is SourceType.LYRICS,
        )
        self._set_field_visible("waveform_style", source_type is SourceType.AUDIO_WAVEFORM)
        for key in (
            "level_meter_mode", "level_meter_style", "level_meter_orientation",
            "level_meter_sensitivity", "level_meter_attack", "level_meter_release",
            "level_meter_min_level", "level_meter_max_level", "level_meter_segments",
            "level_meter_gap", "level_meter_show_peak", "level_meter_peak_hold",
            "level_meter_peak_decay", "level_meter_track_color", "level_meter_low_color",
            "level_meter_mid_color", "level_meter_high_color",
        ):
            self._set_field_visible(key, source_type is SourceType.AUDIO_LEVEL_METER)
        for key in (
            "particle_style", "particle_density", "particle_speed",
            "particle_min_size", "particle_max_size", "particle_opacity",
            "particle_direction", "particle_drift", "particle_twinkle",
            "particle_glow", "particle_secondary_color", "particle_seed",
        ):
            self._set_field_visible(key, source_type is SourceType.PARTICLE_OVERLAY)
        self._set_field_visible(
            "blur", source_type in self.IMAGE_BACKED_TYPES and not ambient_background,
        )
        for key in ("brightness", "contrast"):
            self._set_field_visible(key, source_type in self.IMAGE_BACKED_TYPES)
        for key in MotionSection.REACTIVE_KEYS:
            self._set_field_visible(key, source_type in {SourceType.TEXT, SourceType.LYRICS})
        for key in (
            "shadow", "shadow_color", "shadow_opacity", "shadow_blur",
            "shadow_x", "shadow_y", *MotionSection.LOOP_KEYS,
        ):
            self._set_field_visible(key, source is not None)
        self._set_field_visible("mask_shape", source is not None and shows_mask(source))
        self._hide_inactive_dependent_fields(source)
        self._refresh_property_tabs([source] if source else [])

    def _hide_inactive_dependent_fields(self, source: Source | None) -> None:
        """Show sub-properties only while their enabling toggle or value is set.

        Runs after the type-based pass. Rows that have no independent type rule
        (gradient/outline/animation-duration) are toggled both ways here; rows
        that are already type-gated (shadow, level meter, lyric context) are
        only hidden when their toggle is off and left to the type pass to show.
        """
        if source is None:
            return
        toggled_both_ways = {
            "gradient_start": source.gradient.enabled,
            "gradient_end": source.gradient.enabled,
            "gradient_angle": source.gradient.enabled,
            # Text-like sources use outline_color as their primary glyph
            # colour.  Keep it editable even when the separate source outline
            # is disabled; non-text sources retain the old dependent behavior.
            "outline_color": (
                not self._uses_primary_text_color(source)
                and source.outline_width > 0.0
            ),
            "animation_in_duration": source.animation_in != "none",
            "animation_out_duration": source.animation_out != "none",
            # Only while the project mixes tracks (crossfade/AutoMix).
            "animation_fit_mix": self._mix_transitions_active and (
                source.animation_in != "none" or source.animation_out != "none"
            ),
        }
        for key, active in toggled_both_ways.items():
            if key in self._field_widgets:
                self._set_field_visible(key, active)
        hidden_when_off = {
            "text_stroke_color": source.text_stroke_width > 0.0,
            "shadow_color": source.shadow.enabled,
            "shadow_opacity": source.shadow.enabled,
            "shadow_blur": source.shadow.enabled,
            "shadow_x": source.shadow.enabled,
            "shadow_y": source.shadow.enabled,
            **self.level_meter.hidden_when_off(source),
            **self.lyrics.hidden_when_off(source),
            **self.typography.hidden_when_off(source),
            **self.motion.hidden_when_off(source),
            **self.visualizer.hidden_when_off(source),
        }
        for key, active in hidden_when_off.items():
            if not active and key in self._field_widgets:
                self._set_field_visible(key, False)

    @staticmethod
    def _spin(minimum: float, maximum: float, step: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setSingleStep(step)
        spin.setDecimals(2)
        spin.setKeyboardTracking(False)
        return spin

    def _slider_spin_editor(
        self, spin: QDoubleSpinBox, *, slider_minimum: float | None = None,
        slider_maximum: float | None = None,
    ) -> QWidget:
        """Pair a drag-friendly slider with the existing precise number editor."""
        minimum = spin.minimum() if slider_minimum is None else slider_minimum
        maximum = spin.maximum() if slider_maximum is None else slider_maximum
        step = spin.singleStep()
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setObjectName("inspectorValueSlider")
        slider.setRange(0, max(1, round((maximum - minimum) / step)))
        slider.setMinimumWidth(64)
        slider.setAccessibleName("Property slider")
        # Room for a signed two-decimal value ("-360.00") plus the stepper
        # buttons; 82px clipped four-digit widths to "760.".
        spin.setMinimumWidth(96)
        spin.setMaximumWidth(104)

        def update_spin(position: int) -> None:
            spin.setValue(minimum + position * step)

        def update_slider(value: float) -> None:
            position = round((min(maximum, max(minimum, value)) - minimum) / step)
            previous = slider.blockSignals(True)
            slider.setValue(position)
            slider.blockSignals(previous)

        slider.valueChanged.connect(update_spin)
        spin.valueChanged.connect(update_slider)
        update_slider(spin.value())
        host = QWidget()
        host.setObjectName("inspectorSliderSpinEditor")
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(slider, 1)
        row.addWidget(spin)
        self._slider_hosts[spin] = host
        self._linked_sliders[spin] = slider
        return host

    @staticmethod
    def _color_button() -> QPushButton:
        button = QPushButton()
        button.setMinimumWidth(112)
        return button

    def _connect_fields(self) -> None:
        self.name_edit.textEdited.connect(lambda _value: self._dirty_line_fields.add("name"))
        self.text_edit.textEdited.connect(lambda _value: self._dirty_line_fields.add("text"))
        self.text_edit.pairedTextEdited.connect(
            lambda _value: self._dirty_line_fields.add("text")
        )
        self.name_edit.editingFinished.connect(
            lambda: self._update_line("name", self.name_edit)
        )
        self.text_edit.editingFinished.connect(
            lambda: self._update_line("text", self.text_edit)
        )
        self.expand_text_button.clicked.connect(self._open_expanded_text_editor)
        self.file_button.clicked.connect(self._choose_content_file)
        self.video_settings_button.clicked.connect(self._open_video_settings)
        self.clear_file_button.clicked.connect(lambda: self._update("content_path", ""))
        self.shape_kind_combo.currentIndexChanged.connect(
            lambda _index: self._update("shape_kind", self.shape_kind_combo.currentData())
        )
        self.progress_style_combo.currentIndexChanged.connect(
            lambda _index: self._update("progress_style", self.progress_style_combo.currentData())
        )
        self.visualizer.connect(self._update)
        self.text_alignment_combo.currentIndexChanged.connect(lambda _index: self._update("text_alignment", self.text_alignment_combo.currentData()))
        self.text_overflow_combo.currentIndexChanged.connect(
            lambda _index: self._update(
                "text_overflow", self.text_overflow_combo.currentData()
            )
        )
        self.image_fit_combo.currentIndexChanged.connect(lambda _index: self._update("image_fit_mode", self.image_fit_combo.currentData()))
        self.background_mode_combo.currentIndexChanged.connect(self._update_background_mode)
        self.background_ambient_check.toggled.connect(lambda value: self._update("background_ambient", value))
        self.background_ambient_blur_spin.valueChanged.connect(
            lambda value: self._update("background_ambient_blur", value)
        )
        self.background_ambient_motion_spin.valueChanged.connect(
            lambda value: self._update("background_ambient_motion", value)
        )
        self.background_bass_reactive_check.toggled.connect(
            lambda value: self._update("background_bass_reactive", value)
        )
        self.background_bass_strength_spin.valueChanged.connect(
            lambda value: self._update("background_bass_strength", value)
        )
        self.background_track_transition_check.toggled.connect(
            self._update_background_track_transition
        )
        self.background_track_transition_seconds_spin.valueChanged.connect(
            lambda _value: self._update(
                "background_track_transition_seconds",
                self.background_track_transition_seconds_spin.value(),
            )
        )
        self.progress_value_spin.valueChanged.connect(lambda _value: self._update("progress_value", self.progress_value_spin.value()))
        self.progress_mode_combo.currentIndexChanged.connect(
            lambda _index: self._update("progress_mode", self.progress_mode_combo.currentData())
        )
        self.album_frame_combo.currentIndexChanged.connect(lambda _index: self._update("album_frame_style", self.album_frame_combo.currentData()))
        self.track_list.connect(
            self._update, choose_color=self._choose_color,
            apply_mixed_checkbox=self._apply_mixed_checkbox,
        )
        self.now_playing.connect(self._update)
        self.motion.connect(self._update, apply_mixed_checkbox=self._apply_mixed_checkbox)
        self.motion.widgets["loop_motion"].currentIndexChanged.connect(
            self._update_animation_preview_button
        )
        self.motion.widgets["music_reactive_enabled"].toggled.connect(self._update_animation_preview_button)
        self.visualizer_center_cover_button.clicked.connect(self._add_center_cover)
        for section in (self.lyrics, self.typography):
            section.connect(
                self._update, choose_color=self._choose_color,
                apply_mixed_checkbox=self._apply_mixed_checkbox,
            )
        self.lyrics_line_styles_button.clicked.connect(self._open_lyrics_line_styles)
        self.lyrics_transition_button.clicked.connect(self._show_lyrics_transitions)
        self.lyrics_animation_button.clicked.connect(self._show_lyrics_transitions)
        self.progress_knob_combo.currentIndexChanged.connect(
            lambda _index: self._update("progress_knob", self.progress_knob_combo.currentData())
        )
        self.gradient_angle_spin.valueChanged.connect(
            lambda value: self._update_nested("gradient", "angle", value)
        )
        self.waveform_style_combo.currentIndexChanged.connect(lambda _index: self._update("waveform_style", self.waveform_style_combo.currentData()))
        self.level_meter.connect(
            self._update, choose_color=self._choose_color,
            apply_mixed_checkbox=self._apply_mixed_checkbox,
        )
        self.particle.connect(self._update, choose_color=self._choose_color)
        self.progress_track_color_button.clicked.connect(lambda: self._choose_color("progress_track_color", self.progress_track_color_button))
        self.animation_in_combo.currentIndexChanged.connect(lambda _index: self._update("animation_in", self.animation_in_combo.currentData()))
        self.animation_out_combo.currentIndexChanged.connect(lambda _index: self._update("animation_out", self.animation_out_combo.currentData()))
        self.animation_in_combo.currentIndexChanged.connect(self._update_animation_preview_button)
        self.animation_out_combo.currentIndexChanged.connect(self._update_animation_preview_button)
        self.animation_in_combo.currentIndexChanged.connect(self._autoplay_animation_preview)
        self.animation_out_combo.currentIndexChanged.connect(self._autoplay_animation_preview)
        self.animation_in_duration_spin.valueChanged.connect(
            lambda value: self._update("animation_in_duration", value)
        )
        self.animation_out_duration_spin.valueChanged.connect(
            lambda value: self._update("animation_out_duration", value)
        )
        self.animation_preview_button.clicked.connect(self._request_animation_preview)
        for field, widget in (
            ("x", self.x_spin), ("y", self.y_spin), ("width", self.width_spin),
            ("height", self.height_spin), ("rotation", self.rotation_spin),
            ("scale", self.scale_spin), ("opacity", self.opacity_spin),
            ("border_radius", self.radius_spin), ("outline_width", self.outline_spin),
            ("font_size", self.font_size_spin),
        ):
            widget.valueChanged.connect(
                lambda _value, key=field, control=widget: self._update(key, control.value())
            )
        self.font_family_combo.currentTextChanged.connect(
            lambda value: self._update("font_family", value.strip() or "Segoe UI")
        )
        self.font_weight_combo.currentIndexChanged.connect(
            lambda _index: self._update(
                "font_weight", self.font_weight_combo.currentData(),
            )
        )
        self.font_add_button.clicked.connect(self._add_font_file)
        self.z_spin.valueChanged.connect(lambda _value: self._update("z_index", self.z_spin.value()))
        self.animation_fit_mix_check.toggled.connect(
            lambda value: self._update("animation_fit_mix", value))
        self.visible_check.toggled.connect(lambda value: self._update("visible", value))
        self.locked_check.toggled.connect(lambda value: self._update("locked", value))
        self.fill_color_button.clicked.connect(
            lambda: self._choose_color("fill_color", self.fill_color_button)
        )
        self.outline_color_button.clicked.connect(
            lambda: self._choose_color("outline_color", self.outline_color_button)
        )
        self.text_color_button.clicked.connect(
            lambda: self._choose_color("outline_color", self.text_color_button)
        )
        self.text_stroke_color_button.clicked.connect(
            lambda: self._choose_color("text_stroke_color", self.text_stroke_color_button)
        )
        self.text_stroke_width_spin.valueChanged.connect(
            lambda _value: self._update("text_stroke_width", self.text_stroke_width_spin.value())
        )
        self.gradient_check.toggled.connect(self._update_gradient_enabled)
        self.gradient_start_button.clicked.connect(
            lambda: self._choose_gradient_color("start_color", self.gradient_start_button)
        )
        self.gradient_end_button.clicked.connect(
            lambda: self._choose_gradient_color("end_color", self.gradient_end_button)
        )
        for field, widget in (("blur", self.blur_spin), ("brightness", self.brightness_spin), ("contrast", self.contrast_spin)):
            widget.valueChanged.connect(lambda _value, key=field, control=widget: self._update(key, control.value()))
        self.shadow_check.toggled.connect(lambda value: self._update_shadow("enabled", value))
        self.shadow_color_button.clicked.connect(self._choose_shadow_color)
        for field, widget in (("opacity", self.shadow_opacity_spin), ("blur_radius", self.shadow_blur_spin), ("offset_x", self.shadow_x_spin), ("offset_y", self.shadow_y_spin)):
            widget.valueChanged.connect(lambda _value, key=field, control=widget: self._update_shadow(key, control.value()))

        direct_checks = (
            ("background_ambient", self.background_ambient_check),
            ("background_bass_reactive", self.background_bass_reactive_check),
            ("background_track_transition", self.background_track_transition_check),
            ("animation_fit_mix", self.animation_fit_mix_check),
            ("visible", self.visible_check), ("locked", self.locked_check),
        )
        for field, checkbox in direct_checks:
            checkbox.clicked.connect(
                lambda checked=False, key=field: self._apply_mixed_checkbox(key, checked)
            )
        self.gradient_check.clicked.connect(
            lambda checked=False: self._apply_mixed_nested_checkbox(
                "gradient.enabled", "gradient", "enabled", checked,
            )
        )
        self.shadow_check.clicked.connect(
            lambda checked=False: self._apply_mixed_nested_checkbox(
                "shadow.enabled", "shadow", "enabled", checked,
            )
        )

    def _update(self, field: str, value: object) -> None:
        if self._updating or not self._source_ids or field in self._lyrics_locked_fields:
            return
        self._apply_updates({field: value})

    def _update_line(self, field: str, editor: QLineEdit) -> None:
        if field in self._mixed_fields and field not in self._dirty_line_fields:
            return
        self._dirty_line_fields.discard(field)
        self._update(field, editor.text())

    def _open_expanded_text_editor(self) -> None:
        """Edit the selected source text without constraining it to one line."""
        if not self._source_ids:
            return
        dialog = TextEditorDialog(self.text_edit.text(), self.translator, self)
        if not dialog.exec():
            return
        value = dialog.text()
        self._dirty_line_fields.add("text")
        self.text_edit.setText(value)
        self._update_line("text", self.text_edit)

    def _show_lyrics_transitions(self) -> None:
        source = self.store.get(self._source_id)
        if source is None or source.source_type is not SourceType.LYRICS:
            return
        dialog = LyricsAnimationDialog(source, self.translator, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._apply_updates(dialog.settings())

    def _open_lyrics_line_styles(self) -> None:
        source = self.store.get(self._source_id)
        if source is None or source.source_type is not SourceType.LYRICS:
            return
        dialog = LyricsLineStyleDialog(source, self.translator, self)
        if dialog.exec():
            self._update("subtitle_line_styles", dialog.styles())

    def _apply_updates(self, changes: dict[str, object]) -> None:
        source_ids = tuple(self._source_ids)
        if not source_ids:
            return
        self._applying_batch = True
        try:
            for source_id in source_ids:
                self.store.update(source_id, **changes)
        finally:
            self._applying_batch = False
        self._refresh_current_selection()

    def _apply_mixed_checkbox(self, field: str, checked: bool) -> None:
        if field in self._mixed_fields:
            self._update(field, checked)

    def _apply_mixed_nested_checkbox(
        self, mixed_key: str, container: str, field: str, checked: bool,
    ) -> None:
        if mixed_key in self._mixed_fields:
            self._update_nested(container, field, checked)

    def _update_background_mode(self, _index: int) -> None:
        """Apply the selected background mode and refresh dependent fields."""
        if self._updating:
            return
        self._update("background_mode", self.background_mode_combo.currentData())
        self._update_common_visibility(self._selected_sources())

    def _update_background_track_transition(self, enabled: bool) -> None:
        """Toggle the cross-fade and show or hide its length field."""
        if self._updating:
            return
        self._update("background_track_transition", enabled)
        self._update_common_visibility(self._selected_sources())

    def _add_font_file(self) -> None:
        """Register a user font and bind its primary family to the selected source."""
        if not self._source_id:
            return
        korean = self.translator.is_korean
        path, _ = QFileDialog.getOpenFileName(
            self,
            "글꼴 파일 추가" if korean else "Add font file",
            "",
            "Font files (*.ttf *.otf);;TrueType/OpenType (*.ttf *.otf)",
        )
        if not path:
            return
        families = load_application_font(path)
        if not families:
            self.subtitle.setText(
                "글꼴 파일을 등록할 수 없습니다." if korean else "The font file could not be registered."
            )
            return
        family = families[0]
        if self.font_family_combo.findText(family) < 0:
            self.font_family_combo.addItem(family)
        self._updating = True
        try:
            self.font_family_combo.setCurrentText(family)
        finally:
            self._updating = False
        self._apply_updates({"font_family": family, "font_path": path})

    def _choose_content_file(self) -> None:
        if not self._source_id:
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            "이미지 선택" if self.translator.is_korean else "Choose image",
            self.file_path_edit.text(),
            "Images (*.jpg *.jpeg *.png *.webp *.svg)",
        )
        if path:
            self._update("content_path", path)

    def _open_video_settings(self) -> None:
        source = self.store.get(self._source_id)
        if source is None or source.source_type is not SourceType.VIDEO:
            return
        dialog = VideoSourceDialog(
            source, self.translator.is_korean, self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._apply_updates(dialog.values)

    def _update_video_settings_button(self, source: Source | None) -> None:
        """Summarize the video's scope before opening its detailed settings."""
        korean = self.translator.is_korean
        if source is None or source.source_type is not SourceType.VIDEO:
            self.video_settings_button.setText(
                "영상 재생 설정…" if korean else "Video playback settings…"
            )
            return
        if source.video_timing_mode == "track":
            text = (
                "곡마다 다른 영상 · 설정…"
                if korean else "Different videos per track · Settings…"
            )
            description = (
                "각 곡의 ‘이 곡의 영상’ 목록을 사용합니다."
                if korean else "Uses each track's ‘Videos for this track’ list."
            )
        else:
            count = len(source.video_paths)
            text = (
                f"전체에서 같은 영상 · {count}개 · 설정…"
                if korean else f"Same videos for whole playlist · {count} · Settings…"
            )
            description = (
                "곡이 바뀌어도 이 요소에 등록한 영상 목록을 이어서 사용합니다."
                if korean else
                "Continues using this source's video list when the track changes."
            )
        self.video_settings_button.setText(text)
        self.video_settings_button.setToolTip(description)
        self.video_settings_button.setAccessibleDescription(description)

    def _dialog_tracks(self) -> list:
        if self._tracks_provider is None:
            return []
        try:
            return list(self._tracks_provider())
        except Exception:  # pragma: no cover - provider is best-effort
            return []

    def _choose_color(self, field: str, _button: QPushButton) -> None:
        source = self.store.get(self._source_id)
        if source is None:
            return
        korean = self.translator.is_korean
        dialog = ColorEditorDialog(
            QColor(str(getattr(source, field))),
            source,
            self.translator,
            "색상 편집" if korean else "Edit color",
            self,
            tracks=self._dialog_tracks(),
            show_personal_color=all(field in entry.personal_color_fields for entry in self._selected_sources()),
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._commit_color_dialog(dialog, "source", field)

    def _update_gradient_enabled(self, enabled: bool) -> None:
        self._update_nested("gradient", "enabled", enabled)

    def _choose_gradient_color(self, field: str, _button: QPushButton) -> None:
        source = self.store.get(self._source_id)
        if source is None:
            return
        korean = self.translator.is_korean
        dialog = ColorEditorDialog(
            QColor(str(getattr(source.gradient, field))),
            source,
            self.translator,
            "그라데이션 색상 편집" if korean else "Edit gradient color",
            self,
            tracks=self._dialog_tracks(),
            show_personal_color=all("fill_color" in entry.personal_color_fields for entry in self._selected_sources()),
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._commit_color_dialog(dialog, "gradient", field)

    def _update_shadow(self, field: str, value: object) -> None:
        if self._updating:
            return
        self._update_nested("shadow", field, value)

    def _update_nested(self, container: str, field: str, value: object) -> None:
        if self._updating or not self._source_ids:
            return
        self._applying_batch = True
        try:
            for source in self._selected_sources():
                setattr(getattr(source, container), field, value)
                self.store.source_changed.emit(source)
        finally:
            self._applying_batch = False
        self._refresh_current_selection()

    def _choose_shadow_color(self) -> None:
        source = self.store.get(self._source_id)
        if source is None:
            return
        korean = self.translator.is_korean
        dialog = ColorEditorDialog(
            QColor(source.shadow.color),
            source.shadow,
            self.translator,
            "그림자 색상 편집" if korean else "Edit shadow color",
            self,
            tracks=self._dialog_tracks(),
            show_personal_color=all(entry.source_type in TEXT_SOURCE_TYPES for entry in self._selected_sources()),
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._commit_color_dialog(dialog, "shadow", "color")

    def _commit_color_dialog(
        self, dialog: ColorEditorDialog, target: str, field: str,
    ) -> None:
        """Apply color and personal-color policy as one Inspector edit."""
        color = self._serialized_color(dialog.selected_color)
        personal = dialog.personal_settings()
        self._applying_batch = True
        try:
            for source in self._selected_sources():
                if target == "source":
                    setattr(source, field, color)
                else:
                    setattr(getattr(source, target), field, color)
                for setting, value in personal.items():
                    setattr(source.shadow if target == "shadow" else source, setting, value)
                self.store.source_changed.emit(source)
        finally:
            self._applying_batch = False
        self._refresh_current_selection()

    def _set_enabled(self, enabled: bool) -> None:
        for editor in self._editors:
            editor.setEnabled(enabled)

    def set_mix_transitions_active(self, active: bool) -> None:
        """Show the mix-handover animation rows while the project crossfades or AutoMixes."""
        if self._mix_transitions_active == bool(active):
            return
        self._mix_transitions_active = bool(active)
        sources = self._selected_sources()
        if len(sources) > 1:
            self._update_common_visibility(sources)
        elif sources:
            self._update_source_specific_fields(sources[0])

    def _update_animation_preview_button(self, _value: object = None) -> None:
        source = self.store.get(self._source_id)
        has_animation = (
            self.animation_in_combo.currentData() != "none"
            or self.animation_out_combo.currentData() != "none"
            or self.motion.widgets["loop_motion"].currentData() not in {None, "none"}
            or (source is not None and source.source_type in {SourceType.TEXT, SourceType.LYRICS} and source.uses_bass_reaction)
        )
        self.animation_preview_button.setEnabled(
            self._source_id is not None and has_animation
        )

    def _request_animation_preview(self) -> None:
        if self._source_id and self.animation_preview_button.isEnabled():
            self.animation_preview_requested.emit(self._source_id)

    def _autoplay_animation_preview(self, _index: object = None) -> None:
        """Play the animation on the Canvas as soon as the user picks a style."""
        if self._updating or not self._source_id:
            return
        if (self.animation_in_combo.currentData() != "none"
                or self.animation_out_combo.currentData() != "none"):
            self.animation_preview_requested.emit(self._source_id)

    def _add_center_cover(self) -> None:
        """Put a circular album cover inside the radial visualizer's ring.

        The cover is sized to the ring, stacked just above the visualizer and
        grouped with it, so the pair moves and resizes as one design.
        """
        visualizer = self.store.get(self._source_id)
        if visualizer is None or visualizer.source_type is not SourceType.AUDIO_VISUALIZER:
            return
        ring = min(visualizer.width, visualizer.height) * visualizer.scale
        size = max(24.0, ring * visualizer.visualizer_inner_radius * 0.92)
        center_x = visualizer.x + visualizer.width / 2.0
        center_y = visualizer.y + visualizer.height / 2.0
        korean = self.translator.is_korean
        cover = Source(
            SourceType.ALBUM_COVER, "앨범 커버" if korean else "Album Cover",
            x=center_x - size / 2.0, y=center_y - size / 2.0, width=size, height=size,
            album_frame_style="circle", fill_color="#1F2937",
            z_index=visualizer.z_index + 1,
            animation_in=visualizer.animation_in, animation_out=visualizer.animation_out,
            animation_in_duration=visualizer.animation_in_duration,
            animation_out_duration=visualizer.animation_out_duration,
        )
        visualizer_id = visualizer.id
        self.store.add(cover)
        if visualizer.group_id is not None:
            self.store.assign_group([cover.id], visualizer.group_id)
        else:
            self.store.add_group(
                "원형 비주얼라이저" if korean else "Radial visualizer",
                [visualizer_id, cover.id],
            )

    def _show_empty_state(self, visible: bool) -> None:
        """Swap the editor for a viewport-centered selection hint."""
        self._content.setVisible(not visible)
        self.empty_state.setVisible(visible)
        if visible:
            self.empty_state.setGeometry(self.viewport().rect())
            self.empty_state.raise_()

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self.empty_state.setGeometry(self.viewport().rect())

    def retranslate(self) -> None:
        self.shape_kind_combo.setPlaceholderText(
            "도형 종류 선택" if self.translator.is_korean else "Choose a shape"
        )
        labels = {
            "name": ("이름", "Name"), "text": ("텍스트", "Text"), "file": ("파일", "File"),
            "video_settings": ("영상 사용 범위", "Video scope"),
            "shape": ("도형", "Shape"), "progress_style": ("진행 바 스타일", "Progress style"),
            "text_alignment": ("텍스트 정렬", "Text alignment"),
            "text_overflow": ("긴 텍스트 처리", "Long text handling"),
            "image_fit": ("이미지 맞춤", "Image fit"),
            "progress_value": ("진행 값", "Progress value"), "progress_track_color": ("트랙 색", "Track color"),
            "x": ("X", "X"), "y": ("Y", "Y"), "width": ("너비", "Width"),
            "height": ("높이", "Height"), "rotation": ("회전", "Rotation"),
            "scale": ("크기", "Scale"), "opacity": ("투명도", "Opacity"),
            "border_radius": ("모서리 반경", "Border radius"), "outline": ("윤곽선", "Outline"),
            "font_size": ("글꼴 크기", "Font size"),
            "font_weight": ("글자 굵기", "Font weight"),
            "font_family": ("글꼴", "Font"),
            "fill_color": ("채우기 색", "Fill color"),
            "text_color": ("텍스트 색상", "Text color"),
            "outline_color": ("윤곽선 색", "Outline color"),
            "text_stroke_color": ("글자 테두리 색", "Text outline color"),
            "text_stroke_width": ("글자 테두리 두께", "Text outline width"),
            "gradient": ("그라데이션", "Gradient"),
            "gradient_start": ("시작 색", "Start color"), "gradient_end": ("끝 색", "End color"),
            "gradient_angle": ("그라데이션 회전", "Gradient rotation"),
            "progress_knob": ("진행 핸들", "Progress handle"),
            "visualizer_center_cover": ("앨범 커버 조합", "Album cover combo"),
            "blur": ("블러", "Blur"), "brightness": ("밝기", "Brightness"), "contrast": ("대비", "Contrast"),
            "shadow": ("그림자", "Shadow"), "shadow_color": ("그림자 색", "Shadow color"),
            "shadow_opacity": ("그림자 투명도", "Shadow opacity"), "shadow_blur": ("그림자 흐림", "Shadow blur"),
            "shadow_x": ("그림자 X", "Shadow X"), "shadow_y": ("그림자 Y", "Shadow Y"),
            "animation_in": ("곡 시작 애니메이션", "Track-start animation"),
            "animation_out": ("곡 종료 애니메이션", "Track-end animation"),
            "animation_in_duration": ("시작 애니메이션 시간", "Entrance duration"),
            "animation_out_duration": ("종료 애니메이션 시간", "Exit duration"),
            "animation_fit_mix": ("믹스 전환 시간 자동", "Fit transition to mix"),
            "layer": ("레이어", "Layer"),
        }
        labels.update({
            "background_mode": ("배경 모드", "Background mode"),
            "background_ambient": ("앨범 커버 앰비언트 블러", "Album art ambient blur"),
            "background_ambient_blur": ("앰비언트 블러", "Ambient blur"),
            "background_ambient_motion": ("앰비언트 흐름", "Ambient flow"),
            "background_bass_reactive": ("베이스 반응 확대", "Bass-reactive zoom"),
            "background_bass_strength": ("베이스 반응 강도", "Bass response strength"),
            "background_track_transition": ("곡 전환 시 배경 크로스페이드", "Cross-fade background on track change"),
            "background_track_transition_seconds": ("전환 길이(초)", "Transition length (s)"),
            "progress_mode": ("진행 기준", "Progress timing"),
            "album_frame": ("앨범 커버 프레임", "Album cover frame"),
            "waveform_style": ("파형 스타일", "Waveform style"),
        })
        labels.update(VisualizerSection.LABELS)
        labels.update(LyricsSection.LABELS)
        labels.update(LevelMeterSection.LABELS)
        labels.update(TrackListSection.LABELS)
        labels.update(NowPlayingSection.LABELS)
        labels.update(ParticleSection.LABELS)
        labels.update(TypographySection.LABELS)
        labels.update(MotionSection.LABELS)
        korean = self.translator.is_korean
        for key, label in self._form_labels.items():
            label.setText(labels[key][0 if korean else 1])
        tab_labels = {
            "layout": ("배치", "Layout"),
            "text": ("텍스트", "Text"),
            "shape": ("모양", "Appearance"),
            "fill": ("채우기", "Fill"),
            "filter": ("필터", "Filters"),
            "animation": ("애니메이션", "Animation"),
            "other": ("기타", "Other"),
        }
        for category, pair in tab_labels.items():
            self.property_tabs.setTabText(
                self._tab_indices[category], pair[0 if korean else 1],
            )
        section_titles = {
            **VisualizerSection.SECTION_TITLES,
            **TrackListSection.SECTION_TITLES,
            **LevelMeterSection.SECTION_TITLES,
            **ParticleSection.SECTION_TITLES,
            **LyricsSection.SECTION_TITLES,
            **NowPlayingSection.SECTION_TITLES,
            **MotionSection.SECTION_TITLES,
        }
        for (_category, section), group in self._sections.items():
            pair = section_titles.get(section, (section, section))
            group.set_title(pair[0 if korean else 1])
        self.visible_check.setText("표시" if korean else "Visible")
        self.locked_check.setText("잠금" if korean else "Locked")
        self.file_button.setText("찾아보기" if korean else "Browse")
        self.clear_file_button.setText("제거" if korean else "Clear")
        self._update_video_settings_button(
            self.store.get(self._source_id) if self._source_id else None
        )
        self.expand_text_button.setText("확장…" if korean else "Expand…")
        self.expand_text_button.setToolTip(
            "긴 텍스트를 별도의 창에서 편집합니다."
            if korean else "Edit long text in a separate window."
        )
        self.font_add_button.setText("글꼴 추가" if korean else "Add font")
        weight_labels = (
            ("얇게 · 300", "보통 · 400", "중간 · 500", "세미 볼드 · 600",
             "굵게 · 700", "매우 굵게 · 800", "블랙 · 900")
            if korean else
            ("Light · 300", "Regular · 400", "Medium · 500", "Semi bold · 600",
             "Bold · 700", "Extra bold · 800", "Black · 900")
        )
        for index, label in enumerate(weight_labels):
            self.font_weight_combo.setItemText(index, label)
        overflow_labels = (
            ("자동 줄바꿈", "말줄임표 (…)", "영역에서 자르기")
            if korean else
            ("Automatic wrap", "Ellipsis (…)", "Clip to box")
        )
        for index, label in enumerate(overflow_labels):
            self.text_overflow_combo.setItemText(index, label)
        for combo in (self.animation_in_combo, self.animation_out_combo):
            for index in range(combo.count()):
                combo.setItemText(
                    index, self._ANIMATION_LABELS[combo.itemData(index)][0 if korean else 1],
                )
        self.motion.retranslate(korean)
        self.visualizer_center_cover_button.setText(
            "가운데에 앨범 커버 배치" if korean else "Place album cover in the centre"
        )
        self.track_list.retranslate(korean)
        self.now_playing.retranslate(korean)
        self.lyrics.retranslate(korean)
        self.lyrics_karaoke_button.setText("가사 시간·적용할 줄 설정…" if korean else "Set lyric timing and lines…")
        self.lyrics_karaoke_button.setToolTip("선택한 곡의 가사 맞춤·색상·노래방 표시를 한 번에 설정합니다." if korean else "Align the selected track's lyrics, choose colors and enable karaoke together.")
        self.lyrics_transition_button.setText(
            "자막 전환 설정…" if korean else "Subtitle transition settings…"
        )
        self.lyrics_transition_button.setToolTip(
            "실시간 미리보기로 이동 방향, 순차 이동과 이전·현재·다음 가사 스타일을 설정합니다."
            if korean else "Customize direction, staggered motion and previous/current/next styles with a live preview."
        )
        self.lyrics_animation_button.setText("자막 전환 고급 설정…" if korean else "Advanced subtitle settings…")
        self.lyrics_animation_button.setToolTip(self.lyrics_transition_button.toolTip())
        source = self.store.get(self._source_id) if self._source_id else None
        line_style_count = (
            sum(bool(style) for style in source.subtitle_line_styles)
            if source is not None and source.source_type is SourceType.LYRICS else 0
        )
        self.lyrics_line_styles_button.setText(
            (f"설정된 줄 {line_style_count}개…" if line_style_count else "설정…")
            if korean else
            (f"{line_style_count} configured…" if line_style_count else "Configure…")
        )
        self.typography.retranslate(korean)
        for index, label in enumerate(
            ("스타일 기본값", "없음", "원", "막대") if korean
            else ("Style default", "None", "Circle", "Bar")
        ):
            self.progress_knob_combo.setItemText(index, label)
        # Field-specific guidance beyond _property_help_text. It is merged into
        # the hover help by _install_property_tooltips; a plain setToolTip here
        # would be overwritten by it.
        self._field_notes = {
            "text": (
                "지원 토큰: " if korean else "Supported tokens: "
            ) + (
                "%title%, %artist%, %album%, %track%, %track_total%, %filename%, "
                "%current_time%, %total_time%, %track_current_time%, %track_total_time%, "
                "%video_current_time%, %video_total_time%"
            ),
            **self.visualizer.notes(korean),
            **self.track_list.notes(korean),
            **self.lyrics.notes(korean),
        }
        self.particle.retranslate(korean)
        self.level_meter.retranslate(korean)
        self.gradient_check.setText("사용" if korean else "Enabled")
        self.animation_preview_button.setText(
            "애니메이션 미리보기" if korean else "Preview animation"
        )
        self.animation_preview_button.setToolTip(
            "등장·종료·반복 모션을 캔버스에서 재생합니다. 음악 반응은 예시 박자로 보여 주며, 실제 음악 반응은 메인 재생 미리보기에서 확인하세요."
            if korean else
            "Play entrance, exit and loop motion. Music reaction uses sample hits here; use Full Preview to see the actual track response."
        )
        self.text_edit.setPlaceholderText(
            "%title% · %artist% · %album%"
        )
        self._install_property_tooltips()
        self._refresh_property_tabs(self._selected_sources())
        self._apply_lyrics_mode()
        self.expand_text_button.setToolTip(
            "긴 텍스트를 별도의 창에서 편집합니다."
            if korean else "Edit long text in a separate window."
        )
        self.expand_text_button.setAccessibleName(
            "텍스트 확장 입력" if korean else "Expanded text editor"
        )
        self.empty_state.setText(self.translator.text("select_object"))
        if self._source_id is None:
            self.title.setText(self.translator.text("inspector"))
            self.subtitle.setText(self.translator.text("select_object"))
        elif len(self._source_ids) > 1:
            count = len(self._source_ids)
            self.title.setText(
                f"요소 {count}개 선택" if korean else f"{count} sources selected"
            )
            self.subtitle.setText(
                "공통 속성을 한 번에 편집합니다. 다른 값은 공백으로 표시됩니다."
                if korean else
                "Edit shared properties together. Mixed values are shown blank."
            )
            self.text_edit.setPlaceholderText("")

    def set_source(self, source: Source | None) -> None:
        """Compatibility entry point for a single Inspector selection."""
        self.set_sources((source.id,) if source is not None else (), source)

    def set_sources(self, source_ids: object, active: Source | None) -> None:
        """Bind the Inspector to one source or a shared multi-selection."""
        identifiers = tuple(
            source_id for source_id in (
                source_ids if isinstance(source_ids, (tuple, list)) else ()
            )
            if isinstance(source_id, str) and self.store.get(source_id) is not None
        )
        sources = [
            source for source_id in identifiers
            if (source := self.store.get(source_id)) is not None
        ]
        self._source_ids = identifiers
        self._source_id = (
            active.id if active is not None and active.id in identifiers
            else identifiers[-1] if identifiers else None
        )
        self._mixed_fields.clear()
        self._dirty_line_fields.clear()
        self._set_enabled(bool(sources))
        self._show_empty_state(not sources)
        if not sources:
            self._update_source_specific_fields(None)
            self.title.setText(self.translator.text("inspector"))
            self.subtitle.setText(self.translator.text("select_object"))
            self._apply_lyrics_mode()
            return
        if len(sources) == 1:
            source = sources[0]
            self._clear_mixed_visuals()
            self.title.setText(source.name)
            try:  # the same localized type name the element palette shows
                kind = self.translator.text(source.source_type.value)
            except KeyError:
                kind = source.source_type.value.replace("_", " ").title()
            self.subtitle.setText(kind)
            self.animation_preview_button.setVisible(True)
            self._fill(source)
            return
        korean = self.translator.is_korean
        self.title.setText(
            f"요소 {len(sources)}개 선택" if korean else f"{len(sources)} sources selected"
        )
        self.subtitle.setText(
            "공통 속성을 한 번에 편집합니다. 다른 값은 공백으로 표시됩니다."
            if korean else
            "Edit shared properties together. Mixed values are shown blank."
        )
        self.animation_preview_button.setVisible(False)
        self._fill_multi(sources)

    def refresh_source(self, source: Source) -> None:
        if self._applying_batch or source.id not in self._source_ids:
            return
        self._refresh_current_selection()

    def _selected_sources(self) -> list[Source]:
        return [
            source for source_id in self._source_ids
            if (source := self.store.get(source_id)) is not None
        ]

    def _refresh_current_selection(self) -> None:
        sources = self._selected_sources()
        if len(sources) > 1:
            self._fill_multi(sources)
        elif sources:
            self.title.setText(sources[0].name)
            self._fill(sources[0])

    def _update_common_visibility(self, sources: list[Source]) -> None:
        """Keep only property rows visible for every selected source."""
        if not sources:
            self._update_source_specific_fields(None)
            return
        visible_sets: list[set[str]] = []
        for source in sources:
            self._update_source_specific_fields(source)
            visible_sets.append({
                key for key, widget in self._field_widgets.items()
                if self._field_visibility.get(key, False)
            })
        common = set.intersection(*visible_sets)
        for key in self._field_widgets:
            self._set_field_visible(key, key in common)
        self._refresh_property_tabs(sources)

    def _fill_multi(self, sources: list[Source]) -> None:
        """Fill from the active source, then blank every non-uniform value."""
        active = self.store.get(self._source_id) or sources[-1]
        self._clear_mixed_visuals()
        self._fill(active)
        self._update_common_visibility(sources)
        self._mixed_fields.clear()
        self._dirty_line_fields.clear()
        self.text_edit.setPlaceholderText("")
        self._updating = True
        try:
            for field, (path, widget, kind) in self._multi_value_bindings().items():
                values = [self._read_path(source, path) for source in sources]
                if any(value != values[0] for value in values[1:]):
                    self._mixed_fields.add(field)
                    self._set_mixed_widget(widget, kind)
                elif isinstance(widget, QCheckBox):
                    widget.setTristate(False)
        finally:
            self._updating = False
        self._update_animation_preview_button()
        self._apply_lyrics_mode()

    def _apply_lyrics_mode(self) -> None:
        sources = self._selected_sources()
        lyrics = [source for source in sources if source.source_type is SourceType.LYRICS]
        active = {key for source in lyrics
                  for key in source.subtitle_advanced_categories}
        self._lyrics_locked_fields = {field for key in active for field in LyricsSection.ADVANCED_BASIC_FIELDS[key]}
        for field in {field for fields in LyricsSection.ADVANCED_BASIC_FIELDS.values() for field in fields}:
            widget = self._field_widgets[field]
            enabled = bool(sources) and field not in self._lyrics_locked_fields
            if field == "subtitle_animation_duration":
                enabled = enabled and all(source.subtitle_animation != "none" for source in lyrics)
            self._field_hosts[field].setEnabled(enabled)
            widget.setEnabled(enabled)
            slider = self._linked_sliders.get(widget)
            if slider is not None:
                slider.setEnabled(enabled and field not in self._mixed_fields)
        self.lyrics_mode_note.setVisible(bool(lyrics))
        korean = self.translator.is_korean
        names = {"motion": ("움직임", "Motion"), "layout": ("줄 배치", "Layout"),
                 "styles": ("줄 스타일", "Role styles"), "intro": ("인트로", "Intro")}
        labels = ", ".join(names[key][0 if korean else 1] for key in names if key in active)
        self.lyrics_mode_note.setText(
            (f"고급 모드: {labels}\n고급 모드가 활성화되어 있으므로 관련 옵션은 속성 탭에서 설정할 수 없습니다."
             if active else "기본 모드 · 속성 패널의 자막 설정을 적용합니다.") if korean else
            (f"Advanced: {labels}\nAdvanced mode is active; related options cannot be edited in Properties."
             if active else "Basic mode · subtitle settings follow Properties.")
        )
        self._install_property_tooltips()

    def _clear_mixed_visuals(self) -> None:
        """Restore normal control presentation before filling concrete values."""
        previous = self._updating
        self._updating = True
        try:
            for _field, (_path, widget, kind) in self._multi_value_bindings().items():
                if kind == "check" and isinstance(widget, QCheckBox):
                    widget.setTristate(False)
                slider = self._linked_sliders.get(widget)
                if slider is not None:
                    slider.setEnabled(widget.isEnabled())
            self.text_edit.setPlaceholderText("%title% · %artist% · %album%")
        finally:
            self._updating = previous

    @staticmethod
    def _read_path(source: Source, path: str) -> object:
        value: object = source
        for part in path.split("."):
            value = getattr(value, part)
        return value

    def _set_mixed_widget(self, widget: QWidget, kind: str) -> None:
        if kind == "line" and isinstance(widget, QLineEdit):
            widget.clear()
        elif kind == "spin" and isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            widget.lineEdit().clear()
            slider = self._linked_sliders.get(widget)
            if slider is not None:
                slider.setEnabled(False)
        elif kind == "combo" and isinstance(widget, QComboBox):
            widget.setCurrentIndex(-1)
            if widget.isEditable() and widget.lineEdit() is not None:
                widget.lineEdit().clear()
        elif kind == "check" and isinstance(widget, QCheckBox):
            widget.setTristate(True)
            widget.setCheckState(Qt.CheckState.PartiallyChecked)
        elif kind == "color" and isinstance(widget, QPushButton):
            widget.setText("")
            widget.setStyleSheet("border: 1px dashed #7B8794;")

    def _multi_value_bindings(self) -> dict[str, tuple[str, QWidget, str]]:
        """Map editable model values to the controls that present them."""
        bindings: dict[str, tuple[str, QWidget, str]] = {}

        def add(kind: str, controls: dict[str, QWidget]) -> None:
            bindings.update({
                path: (path, widget, kind) for path, widget in controls.items()
            })

        add("line", {
            "name": self.name_edit, "text": self.text_edit,
            "content_path": self.file_path_edit,
        })
        add("combo", {
            "shape_kind": self.shape_kind_combo,
            "progress_style": self.progress_style_combo,
            "text_alignment": self.text_alignment_combo,
            "text_overflow": self.text_overflow_combo,
            "image_fit_mode": self.image_fit_combo,
            "background_mode": self.background_mode_combo,
            "progress_mode": self.progress_mode_combo,
            "progress_knob": self.progress_knob_combo,
            "album_frame_style": self.album_frame_combo,
            "waveform_style": self.waveform_style_combo,
            "font_family": self.font_family_combo,
            "font_weight": self.font_weight_combo,
            "animation_in": self.animation_in_combo,
            "animation_out": self.animation_out_combo,
        })
        add("spin", {
            "x": self.x_spin, "y": self.y_spin, "width": self.width_spin,
            "height": self.height_spin, "rotation": self.rotation_spin,
            "scale": self.scale_spin, "opacity": self.opacity_spin,
            "border_radius": self.radius_spin, "outline_width": self.outline_spin,
            "font_size": self.font_size_spin, "z_index": self.z_spin,
            "progress_value": self.progress_value_spin,
            "background_track_transition_seconds":
                self.background_track_transition_seconds_spin,
            "background_ambient_blur": self.background_ambient_blur_spin,
            "background_ambient_motion": self.background_ambient_motion_spin,
            "background_bass_strength": self.background_bass_strength_spin,
            "blur": self.blur_spin, "brightness": self.brightness_spin,
            "contrast": self.contrast_spin,
            "text_stroke_width": self.text_stroke_width_spin,
            "animation_in_duration": self.animation_in_duration_spin,
            "animation_out_duration": self.animation_out_duration_spin,
            "shadow.opacity": self.shadow_opacity_spin,
            "shadow.blur_radius": self.shadow_blur_spin,
            "shadow.offset_x": self.shadow_x_spin,
            "shadow.offset_y": self.shadow_y_spin,
            "gradient.angle": self.gradient_angle_spin,
        })
        add("check", {
            "background_ambient": self.background_ambient_check,
            "background_bass_reactive": self.background_bass_reactive_check,
            "background_track_transition": self.background_track_transition_check,
            "animation_fit_mix": self.animation_fit_mix_check,
            "visible": self.visible_check, "locked": self.locked_check,
            "gradient.enabled": self.gradient_check,
            "shadow.enabled": self.shadow_check,
        })
        add("color", {
            "progress_track_color": self.progress_track_color_button,
            "fill_color": self.fill_color_button,
            "outline_color": self.outline_color_button,
            "text_stroke_color": self.text_stroke_color_button,
            "gradient.start_color": self.gradient_start_button,
            "gradient.end_color": self.gradient_end_button,
            "shadow.color": self.shadow_color_button,
        })
        bindings.update(self.visualizer.bindings())
        bindings.update(self.lyrics.bindings())
        bindings.update(self.level_meter.bindings())
        bindings.update(self.track_list.bindings())
        bindings.update(self.now_playing.bindings())
        bindings.update(self.particle.bindings())
        bindings.update(self.typography.bindings())
        bindings.update(self.motion.bindings())
        # Text sources and drawable outlines share the legacy model field, but
        # expose it in separate, correctly named UI categories.
        bindings["text_color"] = (
            "outline_color", self.text_color_button, "color",
        )
        return bindings

    def _fill(self, source: Source) -> None:
        self._update_source_specific_fields(source)
        self._updating = True
        try:
            self.name_edit.setText(source.name)
            self.text_edit.setText(source.text)
            self.file_path_edit.setText(source.content_path)
            self._update_video_settings_button(source)
            self.x_spin.setValue(source.x)
            self.y_spin.setValue(source.y)
            self.width_spin.setValue(source.width)
            self.height_spin.setValue(source.height)
            self.rotation_spin.setValue(source.rotation)
            self.scale_spin.setValue(source.scale)
            self.opacity_spin.setValue(source.opacity)
            self.radius_spin.setValue(source.border_radius)
            self.outline_spin.setValue(source.outline_width)
            self.font_size_spin.setValue(source.font_size)
            weight_index = self.font_weight_combo.findData(source.font_weight)
            if weight_index < 0:
                weight_index = min(
                    range(self.font_weight_combo.count()),
                    key=lambda index: abs(
                        int(self.font_weight_combo.itemData(index))
                        - int(source.font_weight)
                    ),
                )
            self.font_weight_combo.setCurrentIndex(weight_index)
            if source.font_path:
                load_application_font(source.font_path)
            if self.font_family_combo.findText(source.font_family) < 0:
                self.font_family_combo.addItem(source.font_family)
            self.font_family_combo.setCurrentText(source.font_family)
            self.shape_kind_combo.setCurrentIndex(self.shape_kind_combo.findData(source.shape_kind))
            self.progress_style_combo.setCurrentIndex(max(0, self.progress_style_combo.findData(source.progress_style)))
            self.visualizer.fill(source)
            self.text_alignment_combo.setCurrentIndex(max(0, self.text_alignment_combo.findData(source.text_alignment)))
            self.text_overflow_combo.setCurrentIndex(
                max(0, self.text_overflow_combo.findData(source.text_overflow))
            )
            self.image_fit_combo.setCurrentIndex(max(0, self.image_fit_combo.findData(source.image_fit_mode)))
            self.background_mode_combo.setCurrentIndex(max(0, self.background_mode_combo.findData(source.background_mode)))
            self.background_ambient_check.setChecked(source.background_ambient)
            self.background_ambient_blur_spin.setValue(source.background_ambient_blur)
            self.background_ambient_motion_spin.setValue(source.background_ambient_motion)
            self.background_bass_reactive_check.setChecked(source.background_bass_reactive)
            self.background_bass_strength_spin.setValue(source.background_bass_strength)
            self.background_track_transition_check.setChecked(
                source.background_track_transition
            )
            self.background_track_transition_seconds_spin.setValue(
                source.background_track_transition_seconds
            )
            self.progress_value_spin.setValue(source.progress_value)
            self.progress_mode_combo.setCurrentIndex(max(0, self.progress_mode_combo.findData(source.progress_mode)))
            self.album_frame_combo.setCurrentIndex(max(0, self.album_frame_combo.findData(source.album_frame_style)))
            self.track_list.fill(source, set_color=self._set_color_button)
            self.now_playing.fill(source)
            self.lyrics.fill(source, set_color=self._set_color_button)
            line_style_count = sum(bool(style) for style in source.subtitle_line_styles)
            self.lyrics_line_styles_button.setText(
                (f"설정된 줄 {line_style_count}개…" if line_style_count else "설정…")
                if self.translator.is_korean else
                (f"{line_style_count} configured…" if line_style_count else "Configure…")
            )
            self.typography.fill(source)
            self.motion.fill(source)
            self.progress_knob_combo.setCurrentIndex(
                max(0, self.progress_knob_combo.findData(source.progress_knob))
            )
            self.gradient_angle_spin.setValue(source.gradient.angle)
            self.waveform_style_combo.setCurrentIndex(max(0, self.waveform_style_combo.findData(source.waveform_style)))
            self.level_meter.fill(source, set_color=self._set_color_button)
            self.particle.fill(source, set_color=self._set_color_button)
            self._set_color_button(self.progress_track_color_button, source.progress_track_color)
            self._set_color_button(self.fill_color_button, source.fill_color)
            self._set_color_button(self.outline_color_button, source.outline_color)
            self._set_color_button(self.text_color_button, source.outline_color)
            self._set_color_button(self.text_stroke_color_button, source.text_stroke_color)
            self.text_stroke_width_spin.setValue(source.text_stroke_width)
            self.gradient_check.setChecked(source.gradient.enabled)
            self._set_color_button(self.gradient_start_button, source.gradient.start_color)
            self._set_color_button(self.gradient_end_button, source.gradient.end_color)
            self.blur_spin.setValue(source.blur)
            self.brightness_spin.setValue(source.brightness)
            self.contrast_spin.setValue(source.contrast)
            self.shadow_check.setChecked(source.shadow.enabled)
            self._set_color_button(self.shadow_color_button, source.shadow.color)
            self.shadow_opacity_spin.setValue(source.shadow.opacity)
            self.shadow_blur_spin.setValue(source.shadow.blur_radius)
            self.shadow_x_spin.setValue(source.shadow.offset_x)
            self.shadow_y_spin.setValue(source.shadow.offset_y)
            self.animation_in_combo.setCurrentIndex(max(0, self.animation_in_combo.findData(source.animation_in)))
            self.animation_out_combo.setCurrentIndex(max(0, self.animation_out_combo.findData(source.animation_out)))
            video = source.source_type is SourceType.VIDEO
            for combo in (self.animation_in_combo, self.animation_out_combo, self.motion.widgets["loop_motion"]):
                for index in range(combo.count()):
                    supported = not video or combo.itemData(index) in (
                        {"none"} if combo is self.motion.widgets["loop_motion"] else {"none", "fade"}
                    )
                    combo.model().item(index).setEnabled(supported)
                    combo.setItemData(index, "영상 내보내기에서 지원하지 않는 효과입니다." if video and not supported and self.translator.is_korean else
                                      "This effect is not supported by video export." if video and not supported else None,
                                      Qt.ItemDataRole.ToolTipRole)
            self.animation_in_duration_spin.setValue(source.animation_in_duration)
            self.animation_out_duration_spin.setValue(source.animation_out_duration)
            self.animation_fit_mix_check.setChecked(source.animation_fit_mix)
            self.z_spin.setValue(source.z_index)
            self.visible_check.setChecked(source.visible)
            self.locked_check.setChecked(source.locked)
        finally:
            self._updating = False
        self._update_animation_preview_button()
        self._apply_lyrics_mode()

    @staticmethod
    def _serialized_color(color: QColor) -> str:
        """Preserve alpha only when needed while keeping legacy RGB readable."""
        name_format = (
            QColor.NameFormat.HexRgb
            if color.alpha() == 255 else QColor.NameFormat.HexArgb
        )
        return color.name(name_format).upper()

    def _set_color_button(self, button: QPushButton, value: str) -> None:
        color = QColor(value)
        if not color.isValid():
            color = QColor("#FFFFFF")
        text_color = "#111111" if color.lightness() > 150 else "#FFFFFF"
        serialized = self._serialized_color(color)
        if color.alpha() == 0:
            button.setText(
                "투명" if self.translator.is_korean else "Transparent"
            )
        elif color.alpha() < 255:
            button.setText(f"{serialized} · {round(color.alphaF() * 100)}%")
        else:
            button.setText(serialized)
        button.setStyleSheet(
            f"QPushButton:enabled {{ background: rgba({color.red()}, {color.green()}, {color.blue()}, "
            f"{color.alpha()}); color: {text_color}; border: 1px solid #7B8794; }}"
        )


def _inspect_legacy_source(inspector: SourceInspector, source: Source) -> None:
    inspector._update_legacy_source_specific_fields(source)


# Phase 7 split every SourceType onto its own dedicated editor below;
# _update_legacy_source_specific_fields keeps every type's logic so
# tests/test_source_registry.py can keep comparing registered field
# visibility against the legacy reference, but _inspect_legacy_source is no
# longer bound to any type here.
source_registry.get(SourceType.SHAPE).inspector = shape_editor.edit
source_registry.get(SourceType.PROGRESS_BAR).inspector = progress_editor.edit
source_registry.get(SourceType.BACKGROUND).inspector = background_editor.edit
source_registry.get(SourceType.ALBUM_COVER).inspector = album_cover_editor.edit
source_registry.get(SourceType.IMAGE).inspector = image_editor.edit
source_registry.get(SourceType.LOGO).inspector = logo_editor.edit
source_registry.get(SourceType.WATERMARK).inspector = watermark_editor.edit
source_registry.get(SourceType.VIDEO).inspector = video_editor.edit
source_registry.get(SourceType.TEXT).inspector = text_editor.edit
source_registry.get(SourceType.TIME).inspector = time_editor.edit
source_registry.get(SourceType.AUDIO_VISUALIZER).inspector = audio_visualizer_editor.edit
source_registry.get(SourceType.AUDIO_WAVEFORM).inspector = audio_waveform_editor.edit
source_registry.get(SourceType.AUDIO_LEVEL_METER).inspector = audio_level_meter_editor.edit
source_registry.get(SourceType.PARTICLE_OVERLAY).inspector = particle_overlay_editor.edit
source_registry.get(SourceType.LYRICS).inspector = lyrics_editor.edit
source_registry.get(SourceType.TRACK_LIST).inspector = track_list_editor.edit
source_registry.get(SourceType.NOW_PLAYING).inspector = now_playing_editor.edit
