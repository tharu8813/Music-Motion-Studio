"""Preview-only, translucent telemetry with independent appearance controls."""
from collections import deque
import json

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout,
    QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSlider,
    QSpinBox, QTabWidget, QVBoxLayout, QWidget, QLayout)

# key, Korean label, English label, section, enabled by default
FIELDS = [
    ('backend', '렌더러', 'Renderer', 'render', True),
    ('fps', '실제 / 목표 FPS', 'Actual / target FPS', 'render', True),
    ('scale', '미리보기 해상도', 'Preview resolution', 'render', True),
    ('frame_budget', '목표 프레임 간격', 'Target frame interval', 'render', False),
    ('quality', '자동 품질 단계', 'Adaptive quality level', 'render', False),
    ('layers', '화면 레이어', 'Canvas layers', 'render', False),
    ('playback', '재생 상태', 'Playback', 'audio', True),
    ('track', '현재 곡', 'Current track', 'audio', False),
    ('position', '전체 재생 위치', 'Playlist position', 'audio', False),
    ('audio_position', '오디오 위치 / 길이', 'Audio position / duration', 'audio', False),
    ('audio_state', '오디오 상태', 'Audio status', 'audio', False),
    ('volume', '음량 / 음소거', 'Volume / mute', 'audio', False),
    ('audio_buffer', '오디오 버퍼', 'Audio buffer', 'audio', False),
    ('mix', '믹스 방식', 'Mix mode', 'audio', False),
    ('latency', '최근 / 최대 표시 지연', 'Last / peak present latency', 'gpu', True),
    ('gpu', 'GPU 텍스처 메모리', 'GPU texture memory', 'gpu', False),
    ('gpu_peak', '최대 텍스처 메모리', 'Peak texture memory', 'gpu', False),
    ('textures', '캐시 / 할당 / 퇴출', 'Cached / allocated / evicted textures', 'gpu', False),
    ('uploads', '업로드 / 재사용', 'Uploads / reuses', 'gpu', False),
    ('upload_bytes', '누적 업로드 용량', 'Total uploaded bytes', 'gpu', False),
    ('gpu_frames', '제출 / 표시 / 드롭', 'Submitted / presented / dropped', 'gpu', False),
    ('scene_frames', '장면 표시 프레임', 'Presented scene frames', 'gpu', False),
    ('filters', 'GPU 필터 레이어', 'GPU filter layers', 'gpu', False),
    ('stalls', '정지 / 연속 정지', 'Stalls / consecutive stalls', 'gpu', False),
    ('awaiting', '표시 대기', 'Awaiting presentation', 'gpu', False),
    ('video', '활성 디코더', 'Active decoders', 'video', False),
    ('decoded', '수신 / 드롭 프레임', 'Accepted / dropped frames', 'video', False),
    ('pressure', '압력 드롭 / 스로틀', 'Pressure drops / throttled frames', 'video', False),
    ('seeks', '탐색 / 소스 변경', 'Seeks / source switches', 'video', False),
    ('pending', '필터 대기 / GPU 프레임', 'Pending filters / GPU frames', 'video', False),
    ('decoder_limit', '디코더 해상도 / FPS 제한', 'Decoder scale / FPS limit', 'video', False),
    ('proxies', '프록시 상태', 'Proxy status', 'video', False),
    ('image_cache', '합성 이미지 용량', 'Composition image bytes', 'cache', False),
    ('overlay_cache', '효과 이미지 용량', 'Overlay image bytes', 'cache', False),
    ('workers', '분석 / 효과 작업', 'Analysis / overlay workers', 'cache', False),
    ('refresh', '새 프레임 대기 / 지연', 'Queued / deferred refresh', 'cache', False),
    ('fps_graph', 'FPS · 최근 60초', 'FPS · last 60 s', 'graph', True),
    ('latency_graph', '표시 지연 · ms', 'Present latency · ms', 'graph', True),
    ('memory_graph', 'GPU 텍스처 메모리 · MiB', 'GPU textures · MiB', 'graph', False),
]
GROUPS = {'render': ('화면', 'Rendering'), 'audio': ('재생 · 오디오', 'Playback & audio'),
          'gpu': ('GPU', 'GPU'), 'video': ('영상 디코더', 'Video decoders'),
          'cache': ('캐시 · 작업', 'Caches & workers'), 'graph': ('그래프', 'Graphs')}

# Presets only change the metrics; personal readability settings stay intact.
PRESETS = [
    ('default', '기본', 'Essentials', '주요 상태와 FPS·지연 그래프', 'Key status, FPS and latency graphs', None),
    ('minimal', '간단히 보기', 'Minimal', 'FPS와 해상도, 재생 상태만 표시', 'FPS, resolution and playback only', {'fps', 'scale', 'playback'}),
    ('render', '화면 품질', 'Rendering quality', '렌더러·품질·레이어와 FPS 추이', 'Renderer, quality, layers and FPS history', {'backend', 'fps', 'scale', 'frame_budget', 'quality', 'layers', 'fps_graph'}),
    ('audio', '재생 · 오디오', 'Playback & audio', '현재 곡·재생 위치·음량·버퍼 확인', 'Track, position, volume and buffering', {f[0] for f in FIELDS if f[3] == 'audio'}),
    ('gpu', 'GPU 진단', 'GPU diagnostics', '텍스처·프레임·지연·정지 상태 확인', 'Textures, frames, latency and stalls', {f[0] for f in FIELDS if f[3] == 'gpu'} | {'backend', 'fps', 'latency_graph', 'memory_graph'}),
    ('video', '영상 디코더', 'Video decoders', '디코더·드롭·탐색·프록시 확인', 'Decoders, drops, seeking and proxies', {f[0] for f in FIELDS if f[3] == 'video'} | {'fps', 'scale', 'fps_graph'}),
    ('memory', '메모리 · 캐시', 'Memory & caches', 'GPU 메모리와 이미지 캐시·작업 확인', 'GPU memory, image caches and workers', {f[0] for f in FIELDS if f[3] == 'cache'} | {'gpu', 'gpu_peak', 'textures', 'upload_bytes', 'memory_graph'}),
    ('graphs', '그래프 중심', 'Graphs', 'FPS·표시 지연·GPU 메모리 추이', 'FPS, latency and GPU memory history', {'fps', 'latency', 'gpu', 'fps_graph', 'latency_graph', 'memory_graph'}),
    ('all', '전체 표시', 'All metrics', '사용 가능한 모든 측정 항목 표시', 'Show every available metric', {f[0] for f in FIELDS}),
]


class HistoryGraph(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.values = deque(maxlen=120)
        self.opacity = 90
        self.empty_text = '측정 대기 / Waiting for samples'
        self.setMinimumHeight(68)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor('#B4CDC3')
        color.setAlpha(round(255*self.opacity/100))
        valid = [v for v in self.values if v is not None]
        painter.setPen(color)
        if not valid:
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.empty_text)
            return
        peak = max(1.0, max(valid)*1.1)
        chart = QRectF(0, 19, self.width()-1, self.height()-25)
        grid = QColor(color)
        grid.setAlpha(round(color.alpha()*0.18))
        painter.setPen(grid)
        for fraction in (0, 0.5, 1):
            y = round(chart.top()+chart.height()*fraction)
            painter.drawLine(0, y, self.width(), y)
        painter.setPen(color)
        painter.drawText(self.rect().adjusted(0, 0, 0, -self.height()+18),
                         Qt.AlignmentFlag.AlignLeft, f'{valid[-1]:.1f}   ·   max {max(valid):.1f}')
        painter.setPen(QPen(color, 1.5))
        previous = None
        for i, value in enumerate(self.values):
            point = None if value is None else QPoint(round(i*chart.width()/119),
                round(chart.bottom()-value/peak*chart.height()))
            if previous is not None and point is not None:
                painter.drawLine(previous, point)
            previous = point


class PerformanceMonitor(QFrame):
    closed = Signal()

    def __init__(self, preview, parent):
        super().__init__(parent)
        self.preview = preview
        self.korean = preview.translator.is_korean
        self.setObjectName('performanceMonitor')
        self.background_opacity = 55
        self.text_opacity = 90
        self.font_size = 12
        self._drag = None
        self._resize_edges = ()
        self._preferred_size = QSize(400, 510)
        self._preferred_position = QPoint(16, 16)
        self.setMouseTracking(True)
        self.options = None
        self._settings_ready = False
        self.move(16, 16)
        root = QVBoxLayout(self)
        root.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)
        header = QHBoxLayout()
        self.heading = QLabel(self.tr('성능 모니터', 'Performance monitor'))
        self.heading.setCursor(Qt.CursorShape.SizeAllCursor)
        self.heading.setToolTip(self.tr('제목을 드래그하여 이동 · 가장자리를 드래그하여 크기 조절',
                                       'Drag the title to move · drag an edge to resize'))
        self.heading.installEventFilter(self)
        header.addWidget(self.heading, 1)
        configure = QPushButton(self.tr('설정', 'Settings'))
        configure.clicked.connect(self.configure)
        close = QPushButton('×')
        close.setAccessibleName(self.tr('닫기', 'Close'))
        close.clicked.connect(self.close_monitor)
        for button in (configure, close):
            button.setStyleSheet('QPushButton {background:transparent; border:0; border-radius:6px; padding:3px 7px; min-height:20px;} QPushButton:hover {background:rgba(180,205,195,35);}')
            header.addWidget(button)
        root.addLayout(header)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet('QScrollArea {background:transparent;} QScrollBar:vertical {width:7px; background:transparent;}')
        scroll.viewport().setAutoFillBackground(False)
        content = QWidget()
        content.setObjectName('monitorContent')
        content.setStyleSheet('#monitorContent {background:transparent;}')
        self.body = QVBoxLayout(content)
        self.body.setContentsMargins(0, 0, 5, 0)
        self.body.setSpacing(7)
        self.fields, self.enabled, self.rows, self.sections = {}, {}, {}, {}
        for group, titles in GROUPS.items():
            heading = QLabel(titles[0 if self.korean else 1])
            self.body.addWidget(heading)
            self.sections[group] = heading
            for key, ko, en, section, default in FIELDS:
                if section != group:
                    continue
                title = self.tr(ko, en)
                row = QWidget()
                layout = QVBoxLayout(row) if group == 'graph' else QHBoxLayout(row)
                layout.setContentsMargins(0, 0, 0, 0)
                layout.setSpacing(5)
                label = QLabel(title)
                value = HistoryGraph() if group == 'graph' else QLabel('—')
                label.setWordWrap(True)
                if isinstance(value, HistoryGraph):
                    value.empty_text = self.tr('재생하면 그래프가 표시됩니다', 'Play to collect samples')
                if isinstance(value, QLabel):
                    value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    value.setWordWrap(True)
                    value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                layout.addWidget(label, 1)
                layout.addWidget(value, 1)
                self.body.addWidget(row)
                self.fields[key] = (title, label, value)
                self.rows[key] = row
                self.enabled[key] = default
        self.body.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll)
        self.footer = QLabel(self.tr('0.5초마다 갱신 · GPU 값은 GPU 모드에서만 수집',
                                    'Updates every 0.5 s · GPU metrics require GPU mode'))
        self.footer.setWordWrap(True)
        root.addWidget(self.footer)
        parent.installEventFilter(self)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.refresh)
        self.load_settings()
        for key, enabled in self.enabled.copy().items():
            self.set_field(key, enabled)
        self.apply_appearance()
        for child in self.findChildren(QWidget):
            child.installEventFilter(self)
        self.hide()
        self._settings_ready = True

    def load_settings(self):
        try:
            data = json.loads(str(QSettings().value('performance_monitor/settings', '{}')))
            if not isinstance(data, dict):
                return
        except (ValueError, TypeError):
            return
        fields = data.get('fields', {})
        if isinstance(fields, dict):
            for key in self.enabled:
                if isinstance(fields.get(key), bool):
                    self.enabled[key] = fields[key]
        for key, low, high in (('font_size', 9, 24), ('background_opacity', 0, 100), ('text_opacity', 0, 100)):
            value = data.get(key)
            if type(value) is int:
                setattr(self, key, max(low, min(high, value)))
        size = data.get('size')
        if (isinstance(size, list) and len(size) == 2
                and all(type(value) is int for value in size)):
            self._preferred_size = QSize(max(280, min(10000, size[0])),
                                         max(180, min(10000, size[1])))
        position = data.get('position')
        if (isinstance(position, list) and len(position) == 2
                and all(type(value) is int for value in position)):
            self._preferred_position = QPoint(max(0, position[0]), max(0, position[1]))
            self.move(self._preferred_position)

    def save_settings(self):
        if self._settings_ready:
            QSettings().setValue('performance_monitor/settings', json.dumps({
                'fields': self.enabled, 'font_size': self.font_size,
                'background_opacity': self.background_opacity,
                'text_opacity': self.text_opacity,
                'size': [self._preferred_size.width(), self._preferred_size.height()],
                'position': [self._preferred_position.x(), self._preferred_position.y()],
            }))

    def tr(self, ko, en):
        return ko if self.korean else en

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(22, 27, 29, round(255*self.background_opacity/100)))
        painter.setPen(QColor(180, 205, 195, round(65*self.background_opacity/100)))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 14, 14)

    def close_monitor(self):
        self.hide()
        if self.options is not None:
            self.options.hide()
        self.closed.emit()

    def apply_appearance(self):
        # Apply only to leaf widgets. The separate settings dialog never inherits
        # the overlay's font size or foreground transparency.
        alpha = round(255*self.text_opacity/100)
        for title, label, value in self.fields.values():
            label.setStyleSheet(f'color:rgba(192,205,204,{alpha}); font-size:{self.font_size}px; background:transparent;')
            if isinstance(value, QLabel):
                value.setStyleSheet(f'color:rgba(243,247,245,{alpha}); font-size:{self.font_size}px; font-weight:600; background:transparent;')
            else:
                value.opacity = self.text_opacity
                value.setStyleSheet(f'font-size:{self.font_size}px; background:transparent;')
                value.update()
        self.heading.setStyleSheet(f'color:rgba(243,247,245,{alpha}); font-size:{self.font_size+1}px; font-weight:600; background:transparent;')
        for heading in self.sections.values():
            heading.setStyleSheet(f'color:rgba(180,205,195,{alpha}); font-size:{self.font_size}px; font-weight:600; padding-top:7px; background:transparent;')
        self.footer.setStyleSheet(f'color:rgba(192,205,204,{alpha}); font-size:10px; background:transparent;')
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self.constrain()
        self.raise_()
        self.refresh()
        self.timer.start()

    def hideEvent(self, event):
        self.timer.stop()
        self._resize_edges = ()
        self._drag = None
        self.unsetCursor()
        super().hideEvent(event)

    def refresh(self):
        p = self.preview
        unavailable = self.tr('— GPU 미사용', '— GPU inactive')
        values = {}
        values['backend'] = p.preview_mode_label.text()
        values['fps'] = f'{p._actual_preview_fps:.1f} / {p.preview_fps}'
        values['scale'] = p.performance_scale_label.text()
        values['frame_budget'] = f'{1000/max(1,p.preview_fps):.2f} ms'
        quality = p._adaptive_quality
        values['quality'] = f'{quality.level} · {quality.factor:.0%}'
        values['layers'] = str(len(p.source_store.sources())) if p.source_store is not None else '—'
        values['playback'] = self.tr('재생 중', 'Playing') if p._playing else self.tr('일시정지', 'Paused')
        index = p._highlighted_track_index
        values['track'] = f'{index+1}/{len(p.tracks)} · {p.tracks[index].title}' if 0 <= index < len(p.tracks) else '—'
        values['position'] = f'{p._playhead_seconds:.2f} s'
        values['audio_position'] = f'{p.media_player.position()/1000:.2f} / {p.media_player.duration()/1000:.2f} s'
        values['audio_state'] = p.media_player.mediaStatus().name
        values['volume'] = f'{p.audio_output.volume():.0%} · ' + (self.tr('음소거', 'Muted') if p.audio_output.isMuted() else self.tr('켜짐', 'On'))
        values['audio_buffer'] = f'{p.media_player.bufferProgress():.0%}'
        values['mix'] = p._transition_mode
        gpu = getattr(p.gpu_surface, 'upload_stats', None) if p.gpu_preview_enabled else None
        health = p._gpu_health.stats
        def mb(n):
            return f'{n/1048576:.1f} MiB'
        for key in ('latency','gpu','gpu_peak','textures','uploads','upload_bytes','gpu_frames','scene_frames','filters','stalls','awaiting'):
            values[key] = unavailable
        if p.gpu_preview_enabled:
            values['latency'] = f'{health.last_latency_ms:.1f} / {health.maximum_latency_ms:.1f} ms'
            values['stalls'] = f'{health.total_stalls} / {health.consecutive_stalls}'
            values['awaiting'] = self.tr('대기 중', 'Waiting') if health.awaiting_frame else self.tr('없음', 'No')
        if gpu is not None:
            values.update(gpu=f'{mb(gpu.allocated_bytes)} / {mb(gpu.texture_budget_bytes)}',
                gpu_peak=mb(gpu.peak_allocated_bytes), textures=f'{gpu.cached_textures} / {gpu.texture_allocations} / {gpu.texture_evictions}',
                uploads=f'{gpu.texture_uploads} / {gpu.texture_reuses}', upload_bytes=mb(gpu.uploaded_bytes),
                gpu_frames=f'{gpu.submitted_frames} / {gpu.presented_frames} / {gpu.dropped_pending_frames}',
                scene_frames=str(gpu.presented_scene_frames), filters=str(gpu.filtered_layers))
        decoders = [item.video_decoder_stats() for item in p._cached_video_items if item._video_timeline_preview_active]
        total = lambda name: sum(getattr(d, name) for d in decoders)
        values.update(video=str(len(decoders)), decoded=f'{total("accepted_frames")} / {total("dropped_frames")}',
            pressure=f'{total("pressure_drops")} / {total("throttled_frames")}',
            seeks=f'{total("seek_count")} / {total("source_switches")}',
            pending=f'{total("filter_pending")} / {total("gpu_backed_frame")}',
            decoder_limit=f'{p._decoder_backpressure.resolution_factor:.0%} / {p._decoder_backpressure.fps_limit} FPS',
            proxies=p._video_proxy_status_text(self.korean).strip(' ·') or self.tr('없음', 'None'))
        values['image_cache'] = mb(p._image.sizeInBytes()+p._base_image.sizeInBytes())
        # Memory enumeration is only needed when this optional diagnostic is shown.
        if self.enabled['overlay_cache']:
            values['overlay_cache'] = mb(sum(image.sizeInBytes() for image in p._overlay_cache.images()))
        values['workers'] = f'{int(p._analysis_worker is not None and p._analysis_worker.isRunning())} / {int(p._overlay_worker is not None and p._overlay_worker.isRunning())}'
        values['refresh'] = f'{p._refresh_queued} / {p._gpu_refresh_deferred}'
        for key, value in values.items():
            self.fields[key][2].setText(value)
        for key, value in (('fps_graph', p._actual_preview_fps if p._playing else None),
            ('latency_graph', health.last_latency_ms if p.gpu_preview_enabled and p._playing else None),
            ('memory_graph', gpu.allocated_bytes/1048576 if gpu is not None else None)):
            graph = self.fields[key][2]
            graph.empty_text = (self.tr('GPU 모드에서 측정됩니다', 'Requires GPU mode')
                if key != 'fps_graph' and not p.gpu_preview_enabled else
                self.tr('재생하면 그래프가 표시됩니다', 'Play to collect samples'))
            graph.values.append(value)
            graph.update()

    def configure(self):
        if self.options is not None:
            self.options.show()
            self.options.raise_()
            return
        # A sibling window, deliberately outside the overlay's style hierarchy.
        self.options = QDialog(self.preview)
        self.options.setWindowTitle(self.tr('성능 모니터 설정', 'Performance monitor settings'))
        self.options.resize(570, 640)
        layout = QVBoxLayout(self.options)
        layout.setContentsMargins(24, 20, 24, 18)
        title = QLabel(self.tr('성능 모니터', 'Performance monitor'))
        title.setObjectName('dialogTitle')
        layout.addWidget(title)
        hint = QLabel(self.tr('프리셋이나 표시 항목을 고르고 모양을 조절하세요. 변경은 바로 적용되고 자동 저장됩니다.',
            'Choose a preset or metrics and adjust appearance. Changes apply immediately and save automatically.'))
        hint.setObjectName('mutedLabel')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        tabs = QTabWidget()
        self.settings_tabs = tabs
        layout.addWidget(tabs, 1)
        preset_scroll = QScrollArea()
        preset_scroll.setWidgetResizable(True)
        preset_scroll.setFrameShape(QFrame.Shape.NoFrame)
        preset_page = QWidget()
        preset_layout = QVBoxLayout(preset_page)
        preset_hint = QLabel(self.tr('프리셋은 표시 항목만 변경합니다. 글꼴과 투명도는 유지됩니다.',
            'Presets change metrics only. Font size and transparency are kept.'))
        preset_hint.setWordWrap(True)
        preset_hint.setObjectName('mutedLabel')
        preset_layout.addWidget(preset_hint)
        self.preset_buttons = {}
        for mode, ko, en, description_ko, description_en, fields in PRESETS:
            row = QHBoxLayout()
            button = QPushButton(self.tr(ko, en))
            button.setMinimumWidth(130)
            button.clicked.connect(lambda checked=False, mode=mode: self.select_preset(mode))
            description = QLabel(self.tr(description_ko, description_en))
            description.setWordWrap(True)
            row.addWidget(button)
            row.addWidget(description, 1)
            preset_layout.addLayout(row)
            self.preset_buttons[mode] = button
        preset_layout.addStretch()
        preset_scroll.setWidget(preset_page)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        choices = QVBoxLayout(content)
        actions = QHBoxLayout()
        self.checks = {}
        for text, mode in ((self.tr('전체 표시', 'All'), 'all'),):
            button = QPushButton(text)
            button.clicked.connect(lambda checked=False, mode=mode: self.select_preset(mode))
            actions.addWidget(button)
        choices.addLayout(actions)
        for group, titles in GROUPS.items():
            label = QLabel(titles[0 if self.korean else 1])
            label.setObjectName('panelTitle')
            choices.addSpacing(12)
            choices.addWidget(label)
            grid = QGridLayout()
            for i, (key, ko, en, section, default) in enumerate(f for f in FIELDS if f[3] == group):
                check = QCheckBox(self.tr(ko, en))
                check.setChecked(self.enabled[key])
                check.toggled.connect(lambda enabled, key=key: self.set_field(key, enabled))
                self.checks[key] = check
                grid.addWidget(check, i//2, i%2)
            choices.addLayout(grid)
        choices.addStretch()
        scroll.setWidget(content)
        tabs.addTab(scroll, self.tr('표시 항목', 'Metrics'))
        appearance = QWidget()
        form = QFormLayout(appearance)
        form.setContentsMargins(18, 24, 18, 18)
        form.setVerticalSpacing(22)
        self.font_spin = QSpinBox()
        self.font_spin.setRange(9, 24)
        self.font_spin.setSuffix(' px')
        self.font_spin.setValue(self.font_size)
        self.font_spin.valueChanged.connect(lambda value: self.change_appearance('font_size', value))
        form.addRow(self.tr('모니터 글꼴 크기', 'Monitor font size'), self.font_spin)
        self.opacity_sliders = {}
        for key, title in (('background_opacity', self.tr('배경 투명도', 'Background transparency')),
                           ('text_opacity', self.tr('텍스트 투명도', 'Text transparency'))):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(0, 100)
            slider.setValue(100-getattr(self, key))
            readout = QLabel(f'{slider.value()}%')
            readout.setMinimumWidth(42)
            slider.valueChanged.connect(lambda value, key=key: self.change_appearance(key, 100-value))
            slider.valueChanged.connect(lambda value, readout=readout: readout.setText(f'{value}%'))
            row_layout.addWidget(slider, 1)
            row_layout.addWidget(readout)
            self.opacity_sliders[key] = slider
            form.addRow(title, row)
        note = QLabel(self.tr('0%는 불투명, 100%는 완전히 투명합니다.\n설정 창의 글꼴과 투명도는 바뀌지 않습니다.',
            '0% is opaque; 100% is fully transparent.\nThe settings window keeps its own font and opacity.'))
        note.setWordWrap(True)
        note.setObjectName('mutedLabel')
        form.addRow(note)
        tabs.addTab(appearance, self.tr('모양', 'Appearance'))
        tabs.addTab(preset_scroll, self.tr('프리셋', 'Presets'))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.options.hide)
        layout.addWidget(buttons)
        self.options.show()

    def select_preset(self, mode):
        preset = next((p for p in PRESETS if p[0] == mode), None)
        if preset is None:
            return
        selected = preset[-1]
        for key, ko, en, group, default in FIELDS:
            enabled = default if selected is None else key in selected
            if hasattr(self, 'checks'):
                self.checks[key].setChecked(enabled)
            else:
                self.set_field(key, enabled)

    def change_appearance(self, key, value):
        setattr(self, key, value)
        self.apply_appearance()
        self.save_settings()

    def set_field(self, key, enabled):
        self.enabled[key] = enabled
        self.rows[key].setVisible(enabled)
        # Keep explicit visibility on leaves for callers and accessibility tools.
        for widget in self.fields[key][1:]:
            widget.setVisible(enabled)
        for group in GROUPS:
            self.sections[group].setVisible(any(self.enabled[f[0]] for f in FIELDS if f[3] == group))
        self.save_settings()

    def constrain(self):
        parent = self.parentWidget()
        self.resize(min(self._preferred_size.width(), parent.width()),
                    min(self._preferred_size.height(), parent.height()))
        # Layout can briefly make the preview narrow during startup. Clamp the
        # visible position without discarding the user's saved coordinates.
        self.move(max(0, min(self._preferred_position.x(), parent.width()-self.width())),
                  max(0, min(self._preferred_position.y(), parent.height()-self.height())))

    def _edges_at(self, point):
        """The clear outer margin is the resize hit target; content keeps its input."""
        edges = []
        if point.x() < 8:
            edges.append('left')
        elif point.x() >= self.width()-8:
            edges.append('right')
        if point.y() < 8:
            edges.append('top')
        elif point.y() >= self.height()-8:
            edges.append('bottom')
        return tuple(edges)

    def _update_resize_cursor(self, edges):
        if len(edges) == 2:
            cursor = (Qt.CursorShape.SizeFDiagCursor if edges in
                      (('left', 'top'), ('right', 'bottom')) else Qt.CursorShape.SizeBDiagCursor)
        elif edges:
            cursor = (Qt.CursorShape.SizeHorCursor if edges[0] in ('left', 'right')
                      else Qt.CursorShape.SizeVerCursor)
        else:
            self.unsetCursor()
            return
        self.setCursor(cursor)

    def mousePressEvent(self, event):
        edges = self._edges_at(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and edges:
            self._resize_edges = edges
            self._resize_anchor = event.globalPosition().toPoint()
            self._resize_origin = self.geometry()
            self._update_resize_cursor(edges)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resize_edges:
            delta = event.globalPosition().toPoint()-self._resize_anchor
            rect = self._resize_origin
            left, top = rect.x(), rect.y()
            right, bottom = left+rect.width(), top+rect.height()
            bounds = self.parentWidget().size()
            minimum_width, minimum_height = min(280, bounds.width()), min(180, bounds.height())
            if 'left' in self._resize_edges:
                left = max(0, min(left+delta.x(), right-minimum_width))
            if 'right' in self._resize_edges:
                right = min(bounds.width(), max(right+delta.x(), left+minimum_width))
            if 'top' in self._resize_edges:
                top = max(0, min(top+delta.y(), bottom-minimum_height))
            if 'bottom' in self._resize_edges:
                bottom = min(bounds.height(), max(bottom+delta.y(), top+minimum_height))
            self.setGeometry(QRect(left, top, right-left, bottom-top))
            self._preferred_size = self.size()
            event.accept()
            return
        self._update_resize_cursor(self._edges_at(event.position().toPoint()))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._resize_edges:
            self._resize_edges = ()
            self._update_resize_cursor(self._edges_at(event.position().toPoint()))
            self._preferred_position = self.pos()
            self.save_settings()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):
        if not self._resize_edges:
            self.unsetCursor()
        super().leaveEvent(event)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Enter and watched is not self.parentWidget() and not self._resize_edges:
            self.unsetCursor()
        if watched is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self.constrain()
        if watched is self.heading:
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._drag = event.globalPosition().toPoint()-self.pos()
                return True
            if event.type() == QEvent.Type.MouseMove and self._drag is not None:
                self._preferred_position = event.globalPosition().toPoint()-self._drag
                self.constrain()
                self._preferred_position = self.pos()
                return True
            if event.type() == QEvent.Type.MouseButtonRelease:
                was_dragging = self._drag is not None
                self._drag = None
                if was_dragging:
                    self.save_settings()
                return True
        return super().eventFilter(watched, event)
