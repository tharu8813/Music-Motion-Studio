"""Edit the track transitions and effects saved with the project."""

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QFrame, QHBoxLayout, QLabel, QRadioButton, QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)

from app.dialogs.help_dialog import install_help_shortcut
from app.models.audio_effects import AUDIO_EFFECTS
from app.models.project import MAX_CROSSFADE_SECONDS, MIN_CROSSFADE_SECONDS, ProjectSettings
from app.ui.form_layout import style_dialog_actions, style_form
from app.utils.i18n import Translator


class ProjectAudioSettingsDialog(QDialog):
    def __init__(self, settings: ProjectSettings, translator: Translator,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("projectAudioSettingsDialog")
        self.translator = translator
        self.selected_settings = replace(settings)
        install_help_shortcut(self, ("other", "project_settings"), translator)
        self.setMinimumSize(540, 420)
        self.resize(700, 640)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(12)
        self.title_label = QLabel(objectName="dialogTitle")
        outer.addWidget(self.title_label)
        self.tabs = QTabWidget(objectName="settingsTabs", documentMode=True)
        outer.addWidget(self.tabs, 1)
        transition_layout, effects_layout = self._add_page(), self._add_page()

        self.transition_none_radio = QRadioButton()
        self.transition_crossfade_radio = QRadioButton()
        self.transition_automix_radio = QRadioButton()
        transition_buttons = QButtonGroup(self)
        for radio in (
            self.transition_none_radio, self.transition_crossfade_radio, self.transition_automix_radio,
        ):
            transition_buttons.addButton(radio)
        crossfade_row = self.crossfade_row = QWidget()
        crossfade_row_layout = QHBoxLayout(crossfade_row)
        crossfade_row_layout.setContentsMargins(24, 0, 0, 0)
        self.crossfade_seconds_label = QLabel()
        self.crossfade_seconds_spin = QDoubleSpinBox()
        self.crossfade_seconds_spin.setRange(MIN_CROSSFADE_SECONDS, MAX_CROSSFADE_SECONDS)
        self.crossfade_seconds_spin.setDecimals(1)
        self.crossfade_seconds_spin.setSingleStep(0.5)
        self.crossfade_seconds_spin.setValue(settings.crossfade_seconds)
        crossfade_row_layout.addWidget(self.crossfade_seconds_label)
        crossfade_row_layout.addWidget(self.crossfade_seconds_spin)
        crossfade_row_layout.addStretch(1)
        self.automix_help = QLabel()
        self.automix_help.setObjectName("mutedLabel")
        self.automix_help.setWordWrap(True)
        self.automix_help.setContentsMargins(24, 4, 0, 4)
        self.crossfade_seconds_label.setBuddy(self.crossfade_seconds_spin)
        transition_layout.addWidget(self.transition_none_radio)
        transition_layout.addWidget(self.transition_crossfade_radio)
        transition_layout.addWidget(crossfade_row)
        transition_layout.addWidget(self.transition_automix_radio)
        transition_layout.addWidget(self.automix_help)
        {
            "none": self.transition_none_radio,
            "crossfade": self.transition_crossfade_radio,
            "automix": self.transition_automix_radio,
        }[settings.transition_mode].setChecked(True)
        for radio in (self.transition_none_radio, self.transition_crossfade_radio, self.transition_automix_radio):
            radio.toggled.connect(self._update_transition_help)
        transition_layout.addStretch(1)

        self.effects_help = QLabel(objectName="mutedLabel", wordWrap=True)
        effects_layout.addWidget(self.effects_help)
        self.effect_controls = {}
        for effect in AUDIO_EFFECTS:
            values = settings.audio_effects.get(effect.key, {})
            section = QVBoxLayout()
            section.setSpacing(4)
            enabled = QCheckBox(objectName=f"audioEffect_{effect.key}")
            description = QLabel(objectName="mutedLabel", wordWrap=True)
            description.setContentsMargins(24, 0, 0, 0)
            fields = QWidget()
            form = QFormLayout(fields)
            style_form(form)
            form.setContentsMargins(24, 6, 0, 4)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
            spins = {}
            for parameter in effect.parameters:
                label = QLabel()
                spin = QDoubleSpinBox()
                spin.setObjectName(f"{effect.key}_{parameter.key}")
                spin.setDecimals(0 if float(parameter.step).is_integer() else 1)
                spin.setRange(parameter.low, parameter.high)
                spin.setSingleStep(parameter.step)
                spin.setValue(values.get(parameter.key, parameter.default))
                label.setBuddy(spin)
                form.addRow(label, spin)
                spins[parameter.key] = label, spin
            section.addWidget(enabled)
            section.addWidget(description)
            section.addWidget(fields)
            effects_layout.addLayout(section)
            enabled.toggled.connect(fields.setVisible)
            enabled.setChecked(values.get("enabled", False))
            fields.setVisible(enabled.isChecked())
            self.effect_controls[effect.key] = enabled, description, spins
        effects_layout.addStretch(1)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
        )
        style_dialog_actions(self.buttons)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("primaryButton")
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)
        translator.language_changed.connect(self.retranslate)
        self.retranslate()

    def _add_page(self) -> QVBoxLayout:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(14, 16, 14, 14)
        layout.setSpacing(14)
        scroll.setWidget(body)
        self.tabs.addTab(scroll, "")
        return layout

    def retranslate(self) -> None:
        korean = self.translator.is_korean
        language = 0 if korean else 1
        title = "프로젝트 오디오 설정" if korean else "Project audio settings"
        self.setWindowTitle(title)
        self.title_label.setText(title)
        self.tabs.setTabText(0, "곡 전환 방식" if korean else "Track transitions")
        self.tabs.setTabText(1, "오디오 효과" if korean else "Audio effects")
        self.transition_none_radio.setText("없음 (즉시 전환, 기본값)" if korean else "None (instant cut, default)")
        self.transition_crossfade_radio.setText(
            "크로스페이드 (지정한 초만큼 서서히 전환)" if korean else "Crossfade (fade over a set number of seconds)"
        )
        self.crossfade_seconds_label.setText("전환 길이" if korean else "Crossfade length")
        self.transition_automix_radio.setText("AutoMix (베타, 템포 인식 자동 전환)" if korean else "AutoMix (beta, tempo-aware)")
        self._update_transition_help()
        self.crossfade_seconds_spin.setSuffix("초" if korean else " s")
        self.effects_help.setText(
            "켠 효과는 전체 믹스에 적용됩니다. 미리듣기와 내보내기에는 FFmpeg가 필요합니다."
            if korean else "Enabled effects apply to the whole mix. Preview and export require FFmpeg."
        )
        for effect in AUDIO_EFFECTS:
            enabled, description, spins = self.effect_controls[effect.key]
            enabled.setText(effect.label[language])
            description.setText(effect.description[language])
            enabled.setAccessibleDescription(description.text())
            for parameter in effect.parameters:
                label, spin = spins[parameter.key]
                label.setText(parameter.label[language])
                spin.setSuffix("초" if korean and parameter.unit == "s" else f" {parameter.unit}")
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장" if korean else "Save")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소" if korean else "Cancel")

    def _update_transition_help(self) -> None:
        korean = self.translator.is_korean
        crossfade = self.transition_crossfade_radio.isChecked()
        self.crossfade_row.setVisible(crossfade)
        self.crossfade_seconds_spin.setEnabled(crossfade)
        if self.transition_automix_radio.isChecked():
            text = (
                "곡의 템포를 분석해 전환 위치와 방식을 자동으로 정합니다.\n"
                "저장 후 플레이리스트의 AutoMix 편집에서 직접 조정하고, 미리듣기의 전환 상세에서 결과를 확인하세요.\n"
                "준비 중에도 재생할 수 있습니다. 섞기 어려운 곡은 기본 크로스페이드 또는 이어서 재생으로 연결합니다."
                if korean else
                "Analyzes tempo to choose where and how tracks blend.\n"
                "Save, then adjust transitions in the playlist's AutoMix editor and inspect the result in Preview → Transition details.\n"
                "Playback continues during preparation. Tracks that cannot blend use a plain crossfade or play back to back."
            )
        elif crossfade:
            text = (
                "앞 곡의 끝과 다음 곡의 시작을 지정한 길이만큼 겹쳐 재생합니다. 곡 분석은 필요하지 않습니다."
                if korean else
                "Overlaps the end of each track with the next for the selected duration. No analysis needed."
            )
        else:
            text = ("앞 곡이 끝나면 다음 곡을 겹침 없이 재생합니다." if korean
                    else "Plays the next track when the previous one ends, without overlap.")
        if self.transition_automix_radio.isChecked() or crossfade:
            text += ("\n미리듣기와 내보내기에 적용되며, 곡이 겹치는 만큼 전체 길이가 줄어듭니다." if korean
                     else "\nApplies to preview and export. Overlaps shorten the total duration.")
        self.automix_help.setText(text)

    def _accept(self) -> None:
        if self.transition_crossfade_radio.isChecked():
            self.selected_settings.transition_mode = "crossfade"
        elif self.transition_automix_radio.isChecked():
            self.selected_settings.transition_mode = "automix"
        else:
            self.selected_settings.transition_mode = "none"
        self.selected_settings.crossfade_seconds = self.crossfade_seconds_spin.value()
        effects = {}
        for effect in AUDIO_EFFECTS:
            enabled, _description, spins = self.effect_controls[effect.key]
            values = {key: spin.value() for key, (_label, spin) in spins.items()}
            if (enabled.isChecked() or effect.key in self.selected_settings.audio_effects
                    or any(values[p.key] != p.default for p in effect.parameters)):
                effects[effect.key] = {"enabled": enabled.isChecked(), **values}
        self.selected_settings.audio_effects = effects
        self.accept()
