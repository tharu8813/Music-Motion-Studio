"""Localized descriptions for source palette cards.

These are display-only texts. Keep them independent of MainWindow so the
palette can be rendered and tested without creating a QApplication.
"""

from __future__ import annotations

from app.models.source import SourceType


def source_hover_help(source_type: SourceType, korean: bool) -> tuple[str, str]:
    """Return a concise purpose and Inspector-setting summary for a source."""
    korean_help = {
        SourceType.IMAGE: (
            "사진이나 그래픽 파일을 캔버스에 표시합니다.",
            "이미지 파일 · 맞춤 방식 · 밝기 · 대비 · 흐림 · 그림자",
        ),
        SourceType.VIDEO: (
            "곡별 또는 전체 타임라인에 맞춰 영상 파일을 재생합니다.",
            "실행 타이밍 · 영상 목록 · 반복 방식 · 사이클 · 속도 · 필터 · 흐림",
        ),
        SourceType.TEXT: (
            "제목과 설명 또는 동적 트랙 정보를 표시합니다.",
            "텍스트 · 글꼴 · 크기 · 정렬 · 줄바꿈 · 색상 · 외곽선",
        ),
        SourceType.SHAPE: (
            "디자인을 구성하는 도형과 색상 면을 추가합니다.",
            "도형 종류 · 채우기 · 그라데이션 · 외곽선 · 모서리 둥글기",
        ),
        SourceType.PROGRESS_BAR: (
            "현재 곡 또는 전체 재생 진행률을 표시합니다.",
            "진행 방식 · 스타일 · 진행 값 · 트랙 색상 · 채우기 색상",
        ),
        SourceType.ALBUM_COVER: (
            "재생 중인 곡의 앨범 이미지를 자동으로 표시합니다.",
            "이미지 맞춤 · 프레임 스타일 · 밝기 · 대비 · 그림자",
        ),
        SourceType.TIME: (
            "현재 시간이나 재생 시간을 동적으로 표시합니다.",
            "시간 형식 · 글꼴 · 크기 · 정렬 · 색상 · 외곽선",
        ),
        SourceType.LOGO: (
            "브랜드 또는 채널 로고 이미지를 배치합니다.",
            "로고 파일 · 이미지 맞춤 · 투명도 · 밝기 · 대비 · 그림자",
        ),
        SourceType.WATERMARK: (
            "영상 위에 워터마크 이미지를 표시합니다.",
            "이미지 파일 · 맞춤 방식 · 투명도 · 위치 · 크기 · 그림자",
        ),
        SourceType.BACKGROUND: (
            "캔버스 전체의 색상, 이미지 또는 앨범 아트 배경을 만듭니다.",
            "배경 방식 · 이미지 맞춤 · 앰비언트 효과 · 밝기 · 대비 · 흐림",
        ),
        SourceType.AUDIO_VISUALIZER: (
            "음악의 주파수 변화에 반응하는 시각 효과를 표시합니다.",
            "스타일 · 막대 수 · 선 굵기 · 감도 · 반응성 · 어택 · 릴리즈 · 스무딩",
        ),
        SourceType.LYRICS: (
            "재생 위치에 맞춰 가사 또는 자막을 표시합니다.",
            "자막 스타일 · 전환 · 문맥 줄 · 줄 간격 · 이전 줄 효과 · 타이밍 보정",
        ),
        SourceType.TRACK_LIST: (
            "현재 곡 주변의 플레이리스트 항목을 표시합니다.",
            "표시 곡 수 · 목록 스타일 · 표시 범위 · 곡 정보 · 간격 · 강조 색상",
        ),
        SourceType.NOW_PLAYING: (
            "현재 재생 중인 곡 정보를 카드 형태로 표시합니다.",
            "카드 스타일 · 표시 시간 · 퇴장 효과 · 글꼴 · 정렬 · 색상",
        ),
        SourceType.AUDIO_WAVEFORM: (
            "음원의 파형을 재생 진행과 함께 표시합니다.",
            "파형 스타일 · 채우기 색상 · 크기 · 투명도 · 애니메이션",
        ),
        SourceType.AUDIO_LEVEL_METER: (
            "음량 레벨과 피크를 실시간 미터로 표시합니다.",
            "모드 · 스타일 · 방향 · 감도 · 어택 · 릴리즈 · 구간 · 피크 · 색상",
        ),
        SourceType.PARTICLE_OVERLAY: (
            "캔버스 위에 움직이는 파티클 또는 노이즈 효과를 추가합니다.",
            "스타일 · 밀도 · 속도 · 크기 · 방향 · 반짝임 · 광택 · 색상 · 시드",
        ),
    }
    english_help = {
        SourceType.IMAGE: ("Display a photo or graphic file on the Canvas.", "Image file · fit mode · brightness · contrast · blur · shadow"),
        SourceType.VIDEO: ("Play video files per track or across the whole timeline.", "Timing · media list · repeat mode · cycles · speed · filters · blur"),
        SourceType.TEXT: ("Display a title, description, or dynamic track information.", "Text · font · size · alignment · wrapping · color · outline"),
        SourceType.SHAPE: ("Add a shape or color surface to the design.", "Shape · fill · gradient · outline · corner radius"),
        SourceType.PROGRESS_BAR: ("Show current-track or playlist progress.", "Progress mode · style · value · track color · fill color"),
        SourceType.ALBUM_COVER: ("Automatically show the current track's album artwork.", "Image fit · frame style · brightness · contrast · shadow"),
        SourceType.TIME: ("Dynamically show clock or playback time.", "Time format · font · size · alignment · color · outline"),
        SourceType.LOGO: ("Place a brand or channel logo image.", "Logo file · image fit · opacity · brightness · contrast · shadow"),
        SourceType.WATERMARK: ("Display a watermark image over the video.", "Image file · fit mode · opacity · position · size · shadow"),
        SourceType.BACKGROUND: ("Create a full-Canvas color, image, or album-art background.", "Background mode · image fit · ambient effect · brightness · contrast · blur"),
        SourceType.AUDIO_VISUALIZER: ("Show a visual effect that reacts to music frequencies.", "Style · bars · line width · sensitivity · reactivity · attack · release · smoothing"),
        SourceType.LYRICS: ("Show lyrics or subtitles synchronized to playback.", "Text color · transition · context lines · spacing · previous-line effect · timing offset"),
        SourceType.TRACK_LIST: ("Show playlist entries around the current track.", "Track count · list style · window · metadata · spacing · highlight colors"),
        SourceType.NOW_PLAYING: ("Show current-track information as a card.", "Card style · duration · exit effect · font · alignment · colors"),
        SourceType.AUDIO_WAVEFORM: ("Show the audio waveform together with playback progress.", "Waveform style · fill color · size · opacity · animation"),
        SourceType.AUDIO_LEVEL_METER: ("Show audio levels and peaks as a live meter.", "Mode · style · direction · sensitivity · attack · release · segments · peak · colors"),
        SourceType.PARTICLE_OVERLAY: ("Add moving particles or noise over the Canvas.", "Style · density · speed · size · direction · twinkle · glow · colors · seed"),
    }
    return (korean_help if korean else english_help)[source_type]


def source_palette_summary(source_type: SourceType, korean: bool) -> str:
    """Return a short secondary line sized for the narrow source palette."""
    summaries = {
        SourceType.IMAGE: ("사진과 그래픽", "Photos and graphics"),
        SourceType.VIDEO: ("영상 클립 재생", "Play video clips"),
        SourceType.TEXT: ("제목과 동적 정보", "Titles and dynamic info"),
        SourceType.SHAPE: ("색상 면과 도형", "Color surfaces and shapes"),
        SourceType.PROGRESS_BAR: ("곡·전체 진행률", "Track or playlist progress"),
        SourceType.ALBUM_COVER: ("현재 곡의 앨범 커버", "Current album artwork"),
        SourceType.TIME: ("시간과 재생 위치", "Clock and playback time"),
        SourceType.LOGO: ("브랜드 로고 이미지", "Brand logo image"),
        SourceType.WATERMARK: ("반투명 워터마크", "Transparent watermark"),
        SourceType.BACKGROUND: ("캔버스 전체 배경", "Full Canvas background"),
        SourceType.AUDIO_VISUALIZER: ("주파수 반응 효과", "Frequency-reactive effect"),
        SourceType.LYRICS: ("실시간 가사와 자막", "Timed lyrics and subtitles"),
        SourceType.TRACK_LIST: ("플레이리스트 목록", "Playlist track list"),
        SourceType.NOW_PLAYING: ("현재 곡 정보 카드", "Current-track info card"),
        SourceType.AUDIO_WAVEFORM: ("재생 반응 오디오 파형", "Playback-reactive waveform"),
        SourceType.AUDIO_LEVEL_METER: ("실시간 음량 미터", "Live audio level meter"),
        SourceType.PARTICLE_OVERLAY: ("파티클과 노이즈 효과", "Particles and noise effects"),
    }
    localized = summaries.get(source_type, ("캔버스 요소", "Canvas source"))
    return localized[0 if korean else 1]
