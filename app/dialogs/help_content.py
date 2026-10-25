"""The User Guide's text: six tabs (one per window) of tagged topics, in Korean and English.

A topic body is rich text for QTextBrowser. ``[[img:name]]`` places the screenshot
``app/resources/help/<language>/name.png`` (see tools/capture_help_images.py); the
①②③ lists under a screenshot follow the numbers drawn on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

HELP_TAB_IDS = ("canvas", "preview", "lyrics_editor", "automix_editor", "track", "other")

_IMAGE_MARKER = re.compile(r"\[\[img:([a-z0-9_]+)\]\]")


@dataclass(frozen=True, slots=True)
class HelpTab:
    identifier: str
    title: str
    summary: str


@dataclass(frozen=True, slots=True)
class HelpTopic:
    identifier: str
    tab: str
    title: str
    tags: tuple[str, ...]
    body: str
    keywords: str = ""
    related: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Entry:
    tab: str
    identifier: str
    title: tuple[str, str]
    tags: tuple[str, str]
    """Comma-separated tags: (Korean, English)."""
    body: tuple[str, str]
    related: tuple[str, ...] = ()
    keywords: tuple[str, str] = ("", "")


def topic_images(topic: HelpTopic) -> tuple[str, ...]:
    """The screenshot names a topic shows, in order."""
    return tuple(_IMAGE_MARKER.findall(topic.body))


def help_tabs(korean: bool) -> list[HelpTab]:
    language = 0 if korean else 1
    return [HelpTab(identifier, title[language], summary[language]) for identifier, title, summary in _TABS]


def help_topics(korean: bool) -> list[HelpTopic]:
    language = 0 if korean else 1
    return [
        HelpTopic(
            entry.identifier, entry.tab, entry.title[language],
            tuple(tag.strip() for tag in entry.tags[language].split(",") if tag.strip()),
            entry.body[language], entry.keywords[language], entry.related,
        )
        for entry in _ENTRIES
    ]


_TABS = (
    ("canvas", ("캔버스", "Canvas"),
     ("메인 편집 화면: 요소·레이어·속성·플레이리스트·타임라인",
      "The main editor: sources, layers, properties, playlist and timeline")),
    ("preview", ("미리보기", "Preview"),
     ("음악과 함께 전체 영상을 재생해 확인하는 화면", "Play the whole video with its music")),
    ("lyrics_editor", ("가사 편집기", "Lyrics editor"),
     ("LRC 파일 생성기: 가사 입력과 타이밍 기록", "The LRC File Generator: type lyrics and record their timing")),
    ("automix_editor", ("AutoMix 편집기", "AutoMix editor"),
     ("곡 사이 전환을 두 곡 타임라인에서 다듬는 창", "Fine-tune transitions on a two-track timeline")),
    ("track", ("곡 정보/설정", "Track info/settings"),
     ("곡마다 정보·분석·가사·영상·음량을 정하는 창", "Per-track details, analysis, lyrics, videos and sound")),
    ("other", ("기타", "More"),
     ("프로젝트·내보내기·설정·FFmpeg·업데이트·문제 해결", "Projects, export, settings, FFmpeg, updates and troubleshooting")),
)



def _e(tab: str, identifier: str, title: tuple[str, str], tags: tuple[str, str],
       korean: str, english: str, related: tuple[str, ...] = (),
       keywords: tuple[str, str] = ("", "")) -> _Entry:
    return _Entry(tab, identifier, title, tags, (korean, english), related, keywords)


# ---------------------------------------------------------------------------------------------
# Canvas: the main editor window
# ---------------------------------------------------------------------------------------------

_CANVAS = (
    _e("canvas", "workspace", ("화면 구성 한눈에 보기", "The workspace at a glance"),
       ("시작하기,화면,패널", "getting started,workspace,panels"),
       """<p>Playlist Canvas는 음악·가사·앨범 커버·비주얼라이저를 하나의 캔버스에 배치해 <b>플레이리스트 영상(MP4)</b>을 만드는 편집기입니다. 메인 창은 다음처럼 나뉩니다.</p>
[[img:canvas_workspace]]
<ol>
<li><b>메뉴와 도구 모음</b> — 파일·프로젝트·편집·추가·보기·도구·도움말 메뉴와 새로 만들기·열기·저장, 실행 취소·다시 실행, 가로·세로 중앙 정렬 버튼</li>
<li><b>왼쪽 패널</b> — <a href='topic:sources'>요소</a>, <a href='topic:project_content'>프로젝트 콘텐츠</a>, <a href='topic:layers'>레이어</a> 탭</li>
<li><b>캔버스</b> — 영상 화면(아트보드)입니다. 요소를 끌어 배치하고 크기를 바꿉니다.</li>
<li><b>속성 패널</b> — 선택한 요소의 <a href='topic:properties'>세부 설정</a></li>
<li><b>하단 작업 패널</b> — <a href='topic:playlist'>플레이리스트</a>, <a href='topic:timeline'>타임라인</a>, <a href='topic:full_preview'>미리보기</a> 탭</li>
<li><b>프로젝트 상태와 내보내기</b> — 프로젝트 이름과 저장 상태, <a href='topic:export'>내보내기</a> 버튼</li>
<li><b>상태 표시줄</b> — 알림 메시지, 백그라운드 작업 진행률(마우스를 올리면 작업별 상세), 캔버스 확대/축소</li>
</ol>
<h2>패널 다루기</h2>
<ul>
<li>패널 경계를 끌어 크기를 바꿉니다. <b>보기 → 패널 크기 초기화</b>는 캔버스가 넓어지도록 기본 크기로 되돌립니다.</li>
<li><code>Ctrl+Alt+L</code> 왼쪽 패널, <code>Ctrl+Alt+R</code> 속성 패널, <code>Ctrl+Alt+B</code> 하단 패널을 숨기거나 다시 보여 줍니다.</li>
<li><code>Ctrl+Alt+1</code> / <code>2</code> / <code>3</code>으로 플레이리스트 / 타임라인 / 미리보기 탭을 엽니다.</li>
</ul>
<div class='note'>캔버스 둘레의 회색 작업 영역과 그리드는 편집 보조 화면입니다. 최종 영상에는 아트보드 안쪽만 들어갑니다.</div>
<div class='tip'>어느 창에서든 <b>F1</b>을 누르면 그 창에 맞는 도움말 탭이 열립니다. 메인 창에서는 포커스가 있는 패널(플레이리스트, 레이어, 속성 등)의 주제가 바로 선택됩니다.</div>""",
       """<p>Playlist Canvas arranges music, lyrics, album art and visualizers on one canvas and turns them into a <b>playlist video (MP4)</b>. The main window is laid out like this:</p>
[[img:canvas_workspace]]
<ol>
<li><b>Menus and toolbar</b> — File, Project, Edit, Add, View, Tools and Help, plus New, Open, Save, Undo, Redo and horizontal/vertical centering</li>
<li><b>Left panel</b> — the <a href='topic:sources'>Sources</a>, <a href='topic:project_content'>Project Content</a> and <a href='topic:layers'>Layers</a> tabs</li>
<li><b>Canvas</b> — the video frame (artboard): drag sources to place and resize them</li>
<li><b>Inspector</b> — <a href='topic:properties'>settings</a> of the selected source</li>
<li><b>Bottom panel</b> — the <a href='topic:playlist'>Playlist</a>, <a href='topic:timeline'>Timeline</a> and <a href='topic:full_preview'>Preview</a> tabs</li>
<li><b>Project status and Export</b> — the project name, whether it is saved, and the <a href='topic:export'>Export</a> button</li>
<li><b>Status bar</b> — messages, background work progress (hover for each task's details) and canvas zoom</li>
</ol>
<h2>Working with panels</h2>
<ul>
<li>Drag a panel border to resize it. <b>View → Reset panel sizes</b> brings back the defaults so the canvas gets more room.</li>
<li><code>Ctrl+Alt+L</code> toggles the left panel, <code>Ctrl+Alt+R</code> the inspector and <code>Ctrl+Alt+B</code> the bottom panel.</li>
<li><code>Ctrl+Alt+1</code> / <code>2</code> / <code>3</code> open the Playlist / Timeline / Preview tabs.</li>
</ul>
<div class='note'>The gray area and grid around the canvas are editing aids. Only the artboard ends up in the video.</div>
<div class='tip'>Press <b>F1</b> in any window to open its help tab. In the main window the topic of the focused panel (playlist, layers, inspector, …) opens directly.</div>""",
       ("start", "sources", "canvas_editing", "canvas_shortcuts"),
       ("메인 창 레이아웃 메뉴 도구 모음 상태 표시줄", "main window layout menu toolbar status bar")),

    _e("canvas", "start", ("새 프로젝트와 첫 영상 만들기", "New project and your first video"),
       ("시작하기,프로젝트", "getting started,project"),
       """<p>프로그램을 시작하면 <b>프로젝트 시작</b> 창에서 새 프로젝트를 만들거나, 파일을 열거나, 최근 프로젝트를 고릅니다. 프로젝트 파일을 이 창으로 끌어와도 열립니다.</p>
[[img:new_project]]
<h2>새 프로젝트 창</h2>
<ul>
<li><b>프로젝트 정보</b>: 이름과 작성자(설명은 프로젝트 설정에서)</li>
<li><b>화면 비율 및 캔버스</b>: 16:9, 9:16, 1:1, 4:3, 3:4, 8:19, 21:9 또는 사용자 지정 크기</li>
<li><b>시작 디자인</b>: 기본 프로젝트나 <a href='topic:presets'>디자인 프리셋</a>을 미리보기와 함께 선택</li>
<li><b>곡 전환</b>: 없음(즉시 전환) · 크로스페이드 · <a href='topic:automix'>AutoMix</a></li>
<li><b>콘텐츠 저장</b>: 프로젝트에 포함(다른 PC에서도 열림) 또는 원본 위치 참조(파일이 작음)</li>
<li><b>시작 음악(선택)</b>: 음악 파일이나 M3U8 플레이리스트를 고르거나 창으로 끌어 놓기</li>
</ul>
<p>여기서 고른 내용은 모두 나중에 <a href='topic:project_settings'>프로젝트 설정</a>에서 바꿀 수 있습니다.</p>
<h2>기본 작업 순서</h2>
<ol>
<li><b>음악 추가</b> — 하단 플레이리스트의 <b>+ 음악 추가</b> 또는 파일 끌어 놓기. 이름이 같은 .lrc/.srt/.vtt가 있으면 가사로 연결됩니다.</li>
<li><b>화면 꾸미기</b> — 왼쪽 <b>요소</b> 탭에서 배경·텍스트·앨범 커버·가사·진행 바·비주얼라이저를 추가하고 속성 패널에서 다듬습니다.</li>
<li><b>확인</b> — <b>미리보기</b>(<code>Ctrl+Alt+3</code>)로 음악과 함께 재생해 봅니다.</li>
<li><b>내보내기</b> — <b>내보내기</b>(<code>Ctrl+E</code>)에서 영상·오디오·자막을 선택합니다. 영상·오디오와 믹싱 시간 확인에는 <a href='topic:ffmpeg'>FFmpeg 설치</a>가 필요합니다.</li>
</ol>
<div class='note'>처음 실행하면 화면 구성을 소개하는 짧은 안내가 나옵니다. <b>도구 → 설정 → 유지 관리 → 프로그램 초기화</b> 후 다시 볼 수 있습니다.</div>""",
       """<p>At startup the <b>Start a project</b> window lets you create a project, open a file or pick a recent project. Dropping a project file on it opens it too.</p>
[[img:new_project]]
<h2>The New project window</h2>
<ul>
<li><b>Project information</b>: name and author (the description lives in Project settings)</li>
<li><b>Aspect ratio and canvas</b>: 16:9, 9:16, 1:1, 4:3, 3:4, 8:19, 21:9 or a custom size</li>
<li><b>Starting design</b>: the default project or a <a href='topic:presets'>design preset</a>, with a preview</li>
<li><b>Transitions</b>: none (cut) · crossfade · <a href='topic:automix'>AutoMix</a></li>
<li><b>Content storage</b>: include in the project (opens on any PC) or reference the originals (smaller file)</li>
<li><b>Starting music (optional)</b>: pick music files or an M3U8 playlist, or drop them on the window</li>
</ul>
<p>Everything chosen here can be changed later in <a href='topic:project_settings'>Project settings</a>.</p>
<h2>Basic workflow</h2>
<ol>
<li><b>Add music</b> — <b>+ Add music</b> in the Playlist, or drop files. A .lrc/.srt/.vtt with the same name is attached as lyrics.</li>
<li><b>Design the screen</b> — add a background, text, album art, lyrics, a progress bar or visualizers from <b>Sources</b> and fine-tune them in the inspector.</li>
<li><b>Check</b> — play it with the music in <b>Preview</b> (<code>Ctrl+Alt+3</code>).</li>
<li><b>Export</b> — choose video, audio, or subtitles under <b>Export</b> (<code>Ctrl+E</code>). Video/audio and mix timing need <a href='topic:ffmpeg'>FFmpeg</a>.</li>
</ol>
<div class='note'>A short tour of the workspace appears on first launch. <b>Tools → Settings → Maintenance → Reset program</b> shows it again.</div>""",
       ("workspace", "project_settings", "playlist", "export"),
       ("새로 만들기 시작 화면 화면 비율 최근 프로젝트", "new startup aspect ratio recent projects welcome tour")),

    _e("canvas", "sources", ("요소 추가하기", "Adding sources"),
       ("요소,캔버스", "sources,canvas"),
       """<p>왼쪽 <b>요소</b> 탭이나 <b>추가</b> 메뉴에서 캔버스에 넣을 요소를 고릅니다. 버튼을 <b>클릭</b>하면 기본 위치에, 캔버스로 <b>끌어 놓으면</b> 놓은 자리에 추가됩니다. 버튼에 마우스를 올리면 용도와 추가 후 설정할 수 있는 항목이 보입니다.</p>
[[img:canvas_sources]]
<ul><li>위쪽 <b>전체 · 기본 · 재생 · 오디오 · 장면</b> 탭으로 분류를 고르고, <b>요소 검색</b> 칸에 이름을 입력해 찾습니다.</li>
<li>▸ 표시가 있는 버튼(텍스트, 이미지)은 펼치면 <b>시간 표시</b>, <b>로고</b>, <b>워터마크</b> 같은 설정 템플릿이 나옵니다. 템플릿은 부모 요소와 같은 종류로 추가됩니다.</li></ul>
<table cellspacing='4'>
<tr><th>분류</th><th>요소</th><th>하는 일</th></tr>
<tr><td>기본</td><td>텍스트 · 도형 · 이미지 · 영상</td><td>제목과 <a href='topic:text_tokens'>동적 곡 정보</a>, 색 면과 도형, 사진, <a href='topic:video_source'>영상 클립</a></td></tr>
<tr><td>재생 정보</td><td>가사/자막 · 앨범 커버 · 진행 바 · 트랙 목록 · 현재 재생 카드 · 시간</td><td>재생 중인 곡에 맞춰 자동으로 바뀝니다</td></tr>
<tr><td>오디오</td><td>비주얼라이저 · 오디오 파형 · 레벨 미터 · 파티클/노이즈</td><td>음악에 반응하는 <a href='topic:audio_visuals'>시각 효과</a></td></tr>
<tr><td>장면</td><td>배경 · 로고 · 워터마크</td><td>색·이미지·앨범 아트 배경과 브랜딩</td></tr>
</table>
<div class='note'>이미지·영상·음원은 <a href='topic:project_content'>프로젝트 콘텐츠</a>에서 캔버스로 바로 끌어 놓아도 됩니다.</div>""",
       """<p>Pick what to add from the <b>Sources</b> tab on the left or the <b>Add</b> menu. <b>Click</b> a button to add it at the default spot, or <b>drag</b> it onto the canvas to drop it where you want. Hover a button to see what it does and what you can set after adding it.</p>
[[img:canvas_sources]]
<ul><li>Choose a category with <b>All · Basic · Playback · Audio · Scene</b> at the top, or type in <b>Search sources</b>.</li>
<li>Buttons with ▸ (Text, Image) expand into templates such as <b>Time</b>, <b>Logo</b> and <b>Watermark</b>; a template is added as its parent source type.</li></ul>
<table cellspacing='4'>
<tr><th>Category</th><th>Sources</th><th>What they do</th></tr>
<tr><td>Basic</td><td>Text · Shape · Image · Video</td><td>Titles and <a href='topic:text_tokens'>live track info</a>, color surfaces and shapes, photos, <a href='topic:video_source'>video clips</a></td></tr>
<tr><td>Playback</td><td>Lyrics/subtitles · Album cover · Progress bar · Track list · Now playing card · Time</td><td>Follow the track that is playing</td></tr>
<tr><td>Audio</td><td>Visualizer · Audio waveform · Level meter · Particles/noise</td><td><a href='topic:audio_visuals'>Visuals</a> that react to the music</td></tr>
<tr><td>Scene</td><td>Background · Logo · Watermark</td><td>Color, image or album-art backgrounds and branding</td></tr>
</table>
<div class='note'>You can also drag images, videos and audio straight from <a href='topic:project_content'>Project Content</a> onto the canvas.</div>""",
       ("canvas_editing", "properties", "text_tokens", "audio_visuals"),
       ("배경 이미지 텍스트 도형 앨범 커버 로고 워터마크 진행 바 시간 트랙 목록 현재 재생 카드 추가 메뉴 템플릿",
        "background image text shape album cover logo watermark progress bar time track list now playing add menu template")),

    _e("canvas", "canvas_editing", ("캔버스에서 편집하기", "Editing on the canvas"),
       ("캔버스,선택,정렬", "canvas,selection,alignment"),
       """<h2>선택과 변형</h2>
<ul>
<li>요소를 클릭해 선택하고 끌어서 옮깁니다. <code>Ctrl</code>+클릭이나 빈 곳에서 끌어 여러 요소를 선택합니다.</li>
<li>테두리 핸들로 크기를, 위쪽 원형 핸들로 회전을 바꿉니다.</li>
<li><code>Alt</code>+크기 조절은 현재 비율을 유지하고, <code>Alt+Shift</code>+크기 조절은 1:1 비율로 바꿔 조절합니다. 앨범 커버는 항상 1:1입니다.</li>
<li>끄는 동안 가까운 캔버스와 다른 요소의 가장자리·중앙에 <b>스냅</b>되며 안내선이 표시됩니다. <code>Alt</code>를 누르는 동안 스냅이 해제되어 정밀하게 이동·크기 조절할 수 있습니다.</li>
<li><code>Ctrl</code>을 누른 채 끌면 놓은 자리에 복제됩니다. 텍스트·가사 요소는 더블클릭해 바로 편집합니다.</li>
</ul>
<h2>정밀 이동과 정렬</h2>
<ul>
<li>방향키: 1px, <code>Shift</code>+방향키: 10px 이동</li>
<li><code>Ctrl+Shift+H</code> / <code>Ctrl+Shift+V</code>: 캔버스 가로 / 세로 중앙 정렬 (도구 모음 버튼과 같음)</li>
<li><code>Ctrl+]</code> / <code>Ctrl+[</code>: 맨 앞으로 / 맨 뒤로</li>
<li><code>Ctrl+G</code> / <code>Ctrl+Shift+G</code>: 그룹 / 그룹 해제, <code>Ctrl+L</code>: 잠금 토글</li>
</ul>
<h2>우클릭 메뉴</h2>
<p>요소를 우클릭하면 잘라내기·복사·복제·삭제, 레이어 순서, 캔버스 중앙 정렬, 여러 요소의 가장자리 맞춤, 그룹, 표시·잠금 명령이 나옵니다. 빈 캔버스를 우클릭하면 붙여넣기·모두 선택·캔버스에 맞추기가 나옵니다.</p>
<h2>보기 이동과 확대</h2>
<ul>
<li><code>Space</code>+끌기 또는 가운데 버튼 끌기: 캔버스 시점 이동</li>
<li><code>Ctrl</code>+휠: 커서 기준 확대/축소, <code>Ctrl++</code> / <code>Ctrl+-</code>, 상태 표시줄의 − / + 버튼</li>
<li><code>F</code>, <code>Home</code>, <code>Ctrl+0</code>: 캔버스 전체 맞춤 · 상태 표시줄의 배율을 누르면 100%와 화면 맞춤을 오갑니다</li>
<li><b>보기 → 그리드</b>로 격자를 켜고 끕니다.</li>
</ul>
<div class='note'>모든 편집은 <code>Ctrl+Z</code> / <code>Ctrl+Shift+Z</code>로 실행 취소·다시 실행할 수 있으며, 되돌린 뒤에도 선택 상태가 유지됩니다.</div>""",
       """<h2>Select and transform</h2>
<ul>
<li>Click a source to select it and drag to move it. <code>Ctrl</code>+click, or drag on an empty spot, to select several.</li>
<li>Resize with the border handles; rotate with the round handle on top.</li>
<li><code>Alt</code>+resize keeps the current ratio; <code>Alt+Shift</code>+resize switches to 1:1. Album covers are always 1:1.</li>
<li>While dragging, sources <b>snap</b> to nearby canvas and source edges and centers, with guides. Hold <code>Alt</code> to disable snapping for precise movement and resizing.</li>
<li><code>Ctrl</code>+drag drops a duplicate. Double-click a text or lyrics source to edit it in place.</li>
</ul>
<h2>Nudge and align</h2>
<ul>
<li>Arrow keys: 1 px, <code>Shift</code>+arrow: 10 px</li>
<li><code>Ctrl+Shift+H</code> / <code>Ctrl+Shift+V</code>: center horizontally / vertically on the canvas (same as the toolbar buttons)</li>
<li><code>Ctrl+]</code> / <code>Ctrl+[</code>: bring to front / send to back</li>
<li><code>Ctrl+G</code> / <code>Ctrl+Shift+G</code>: group / ungroup, <code>Ctrl+L</code>: toggle lock</li>
</ul>
<h2>Context menu</h2>
<p>Right-click a source for cut, copy, duplicate and delete, layer order, centering, aligning the edges of several sources, grouping, visibility and lock. Right-click an empty canvas for paste, select all and fit.</p>
<h2>Pan and zoom</h2>
<ul>
<li><code>Space</code>+drag or middle-button drag: pan</li>
<li><code>Ctrl</code>+wheel: zoom around the cursor; also <code>Ctrl++</code> / <code>Ctrl+-</code> and the − / + buttons in the status bar</li>
<li><code>F</code>, <code>Home</code>, <code>Ctrl+0</code>: fit the whole canvas · click the zoom percentage to switch between 100% and fit</li>
<li><b>View → Grid</b> toggles the grid.</li>
</ul>
<div class='note'>Every edit can be undone and redone with <code>Ctrl+Z</code> / <code>Ctrl+Shift+Z</code>; the selection comes back too.</div>""",
       ("layers", "properties", "canvas_shortcuts"),
       ("이동 크기 회전 복제 그룹 잠금 스냅 안내선 우클릭 확대 축소 그리드 실행 취소",
        "move resize rotate duplicate group lock snap guides right click zoom pan grid undo")),

    _e("canvas", "layers", ("레이어와 그룹", "Layers and groups"),
       ("레이어,캔버스,정렬", "layers,canvas,alignment"),
       """<p>왼쪽 패널의 <b>레이어</b> 탭은 캔버스 요소의 쌓임 순서와 그룹을 보여 줍니다. 위에 있는 레이어가 화면에서도 앞에 그려집니다.</p>
[[img:canvas_layers]]
<ul>
<li><b>표시</b>·<b>잠금</b> 칸으로 요소를 숨기거나 실수로 움직이지 않게 잠급니다. 잠긴 레이어는 이름이 강조색으로 표시되고, <b>잠금 해제</b>는 잠긴 모든 레이어를 한 번에 풉니다.</li>
<li>레이어를 끌어 순서를 바꾸고, 다른 그룹 위에 놓으면 그 그룹으로 옮겨집니다.</li>
<li>아래 <b>⇈ ↑ ↓ ⇊</b> 버튼으로 맨 앞 / 한 단계 앞 / 한 단계 뒤 / 맨 뒤로 보냅니다(여러 개 선택 가능).</li>
<li><b>그룹</b>은 선택한 요소를 새 그룹으로 묶고, <b>해제</b>는 그룹을 풉니다.</li>
<li>이름은 더블클릭하거나 <code>F2</code>로 바꿉니다. 이름은 구분용이며 영상에는 나오지 않습니다.</li>
</ul>
<div class='note'>레이어에서 선택하면 캔버스와 속성 패널의 선택도 함께 바뀝니다.</div>""",
       """<p>The <b>Layers</b> tab on the left shows the stacking order and groups of the canvas sources. A layer higher in the list is drawn in front.</p>
[[img:canvas_layers]]
<ul>
<li>Use the <b>Visible</b> and <b>Lock</b> columns to hide a source or keep it from moving. Locked layers show their name in the accent color; <b>Unlock</b> unlocks every locked layer at once.</li>
<li>Drag layers to reorder them; drop one on another group to move it into that group.</li>
<li>The <b>⇈ ↑ ↓ ⇊</b> buttons bring to front / forward / backward / to back (works on several layers).</li>
<li><b>Group</b> puts the selection into a new group; <b>Ungroup</b> breaks it up.</li>
<li>Rename with a double-click or <code>F2</code>. Names are for you only and never appear in the video.</li>
</ul>
<div class='note'>Selecting in Layers also selects on the canvas and in the inspector.</div>""",
       ("canvas_editing", "properties"),
       ("레이어 순서 표시 숨기기 잠금 그룹 이름 바꾸기 z", "layer order visibility hide lock group rename z order")),

    _e("canvas", "properties", ("속성 패널", "The inspector"),
       ("속성,색상,캔버스", "properties,color,canvas"),
       """<p>캔버스나 레이어에서 요소를 선택하면 오른쪽 <b>속성 패널</b>에 그 요소의 설정이 나타납니다. 값을 바꾸면 캔버스와 미리보기에 바로 반영됩니다.</p>
[[img:canvas_inspector]]
<h2>속성 탭</h2>
<ul>
<li><b>요소 이름 탭</b>(예: 진행 바, 가사): 그 요소에만 있는 전용 설정</li>
<li><b>배치</b>: 이름, X·Y 위치, 너비·높이, 회전, 크기(배율)</li>
<li><b>텍스트</b>: 내용, 정렬, 줄바꿈 방식, 글꼴·굵기·크기, 글자색과 글자 테두리. <b>글꼴 추가</b>로 TTF/OTF를 등록합니다.</li>
<li><b>모양</b>: 투명도와 그림자(색·투명도·흐림·X/Y 거리)</li>
<li><b>채우기</b>: 채움 색, 모서리 반경, 윤곽선, 그라데이션(시작·끝 색, 각도)</li>
<li><b>필터</b>: 이미지·영상의 흐림, 밝기, 대비</li>
<li><b>애니메이션</b>: 곡 시작·종료 효과, 자막 전환, 텍스트·현재 자막의 <a href='topic:music_reaction'>음악 반응</a> — <a href='topic:animation'>애니메이션</a> 참고</li>
<li><b>기타</b>: 레이어 순서(Z), 표시·잠금</li>
</ul>
<p>항목에 마우스를 올리면 설명과 값의 범위·조절 단위가 나옵니다. 머리글(▸)을 눌러 하위 묶음을 접고 펼 수 있습니다.</p>
<p>회색으로 표시된 설정은 현재 변경할 수 없습니다. 자막의 특정 카테고리에 고급 모드를 켰다면 해당 기본 항목이 잠깁니다. <a href='topic:lyrics_transition'>자막 전환 설정</a>에서 수정하거나 그 카테고리의 고급 모드를 끄세요.</p>
<h2>색상과 퍼스널 컬러</h2>
<p>색상 칸을 누르면 색상 편집기가 열립니다. 빠른 색상, 색조·채도·밝기·투명도, <code>#RRGGBB</code>/<code>#AARRGGBB</code> 입력을 지원합니다. <b>현재 트랙에 퍼스널 컬러 사용하기</b>를 켜면 재생 중인 곡의 앨범 커버 대표색을 씁니다. 밝기·채도 보정, 색조 이동, 적용 강도를 정하고 <b>곡별 미리보기</b>로 곡마다 결과를 확인할 수 있습니다. 커버가 없는 곡은 기준 색을 씁니다.</p>
<h2>여러 요소 한 번에</h2>
<p>여러 요소를 선택하면 모두에 공통인 속성만 보입니다. 값이 서로 다른 칸은 비어 있거나 혼합 상태로 표시되고, 새 값을 넣으면 선택한 모든 요소에 적용됩니다.</p>""",
       """<p>Select a source on the canvas or in Layers and its settings appear in the <b>inspector</b> on the right. Changes show up on the canvas and in Preview at once.</p>
[[img:canvas_inspector]]
<h2>Inspector tabs</h2>
<ul>
<li><b>Source tab</b> (for example Progress bar, Lyrics): settings only that source type has</li>
<li><b>Layout</b>: name, X/Y, width/height, rotation, scale</li>
<li><b>Text</b>: content, alignment, overflow, font, weight and size, text color and outline. <b>Add font</b> registers a TTF/OTF.</li>
<li><b>Appearance</b>: opacity and shadow (color, opacity, blur, X/Y offset)</li>
<li><b>Fill</b>: fill color, corner radius, outline, gradient (start/end color, angle)</li>
<li><b>Filters</b>: blur, brightness and contrast for images and video</li>
<li><b>Animation</b>: track start/end effects, subtitle transitions and <a href='topic:music_reaction'>music reaction</a> for text and current subtitles — see <a href='topic:animation'>Animation</a></li>
<li><b>More</b>: stacking order (Z), visibility and lock</li>
</ul>
<p>Hover a field for its explanation, range and step. Click a heading (▸) to fold a group.</p>
<p>Grayed-out settings cannot currently be edited. Enabling advanced mode for a subtitle category locks its matching basic controls. Edit them in <a href='topic:lyrics_transition'>Subtitle transition settings</a> or turn off advanced mode for that category.</p>
<h2>Colors and personal color</h2>
<p>Click a color field for the color editor: quick colors, hue/saturation/brightness/opacity and <code>#RRGGBB</code>/<code>#AARRGGBB</code> input. <b>Use personal color for the current track</b> takes the dominant color of the playing track's cover; adjust brightness, saturation, hue shift and strength, and check each track under <b>Preview per track</b>. Tracks without a cover keep the base color.</p>
<h2>Several sources at once</h2>
<p>With several sources selected, only the settings they share are shown. Fields that differ are blank or mixed, and a new value applies to every selected source.</p>""",
       ("text_tokens", "animation", "canvas_editing"),
       ("배치 텍스트 모양 채우기 필터 애니메이션 기타 그림자 그라데이션 글꼴 폰트 투명도 퍼스널 컬러 색상 편집기 다중 선택",
        "layout text appearance fill filter animation shadow gradient font opacity personal color color editor multiple selection")),

    _e("canvas", "text_tokens", ("텍스트와 동적 토큰", "Text and live tokens"),
       ("텍스트,요소", "text,sources"),
       """<p>텍스트 요소의 내용에 <code>%토큰%</code>을 넣으면 재생 중인 곡의 정보로 자동으로 바뀝니다. 편집 화면에서는 <code>(제목)</code>처럼 이름으로 표시되고, 미리보기와 내보내기에서 실제 값이 들어갑니다.</p>
<table cellspacing='4'>
<tr><th>토큰</th><th>표시되는 값</th></tr>
<tr><td><code>%title%</code> · <code>%artist%</code> · <code>%album%</code></td><td>곡 제목 · 아티스트 · 앨범</td></tr>
<tr><td><code>%track%</code> · <code>%track_total%</code></td><td>현재 곡 번호 · 전체 곡 수</td></tr>
<tr><td><code>%filename%</code></td><td>음원 파일 이름</td></tr>
<tr><td><code>%current_time%</code> · <code>%total_time%</code></td><td>곡의 현재 재생 시간 · 곡 길이 (<code>%track_current_time%</code> · <code>%track_total_time%</code>과 같음)</td></tr>
<tr><td><code>%video_current_time%</code> · <code>%video_total_time%</code></td><td>영상 전체 기준 현재 시간 · 전체 길이</td></tr>
</table>
<p>토큰 앞뒤의 글자는 그대로 유지됩니다. 예: <code>NOW PLAYING · %title%</code>. <b>시간 표시</b> 템플릿은 <code>%current_time% / %total_time%</code>이 들어간 텍스트 요소입니다.</p>
<div class='note'>곡 제목·아티스트·앨범은 <a href='topic:track_info'>곡 정보/설정</a>에서 프로젝트 전용으로 고칠 수 있습니다. 원본 음원의 태그는 바뀌지 않습니다.</div>""",
       """<p>Put a <code>%token%</code> in a text source and it becomes information about the track that is playing. The editor shows it by name, like <code>(Title)</code>; Preview and Export show the real value.</p>
<table cellspacing='4'>
<tr><th>Token</th><th>Shows</th></tr>
<tr><td><code>%title%</code> · <code>%artist%</code> · <code>%album%</code></td><td>Track title · artist · album</td></tr>
<tr><td><code>%track%</code> · <code>%track_total%</code></td><td>Current track number · number of tracks</td></tr>
<tr><td><code>%filename%</code></td><td>Audio file name</td></tr>
<tr><td><code>%current_time%</code> · <code>%total_time%</code></td><td>Elapsed time in the track · track length (same as <code>%track_current_time%</code> · <code>%track_total_time%</code>)</td></tr>
<tr><td><code>%video_current_time%</code> · <code>%video_total_time%</code></td><td>Elapsed time in the whole video · video length</td></tr>
</table>
<p>Text around a token is kept, for example <code>NOW PLAYING · %title%</code>. The <b>Time</b> template is a text source containing <code>%current_time% / %total_time%</code>.</p>
<div class='note'>Titles, artists and albums can be edited per project in <a href='topic:track_info'>Track information/settings</a>; the audio file's own tags are left alone.</div>""",
       ("properties", "sources", "track_info"),
       ("토큰 제목 아티스트 앨범 시간 표시 현재 시간 전체 시간 트랙 번호 파일명",
        "token title artist album time elapsed duration track number file name placeholder")),

    _e("canvas", "video_source", ("영상 요소", "Video sources"),
       ("영상,요소", "video,sources"),
       """<p><b>영상</b> 요소는 영상 클립을 캔버스 안에서 재생합니다. 요소를 추가하거나 속성의 설정 버튼을 누르면 <b>영상 재생 설정</b> 창이 열립니다.</p>
<ol>
<li><b>어디에서 영상을 가져올까요?</b>
<ul><li><b>곡마다 다른 영상 사용</b>: 곡이 바뀔 때마다 그 곡에 등록된 영상으로 바뀝니다. 영상은 <a href='topic:track_videos'>곡 정보/설정 → 이 곡의 영상</a>에서 추가합니다.</li>
<li><b>전체 재생목록에서 같은 영상 사용</b>: 여기서 추가한 영상이 곡과 관계없이 전체 영상 시간에 맞춰 이어서 재생됩니다.</li></ul></li>
<li><b>어떻게 재생할까요?</b> 한 번만 재생 · 첫 번째 영상 계속 반복 · 여러 영상을 순서대로 반복 · 여러 영상을 무작위로 반복, 그리고 <b>끝날 때까지 반복</b> 또는 <b>반복 횟수</b></li>
<li><b>속도와 화면 효과</b>: 재생 속도, 채도, 흑백으로 표시</li>
</ol>
<div class='note'>영상의 소리는 플레이리스트 음악과 섞이지 않도록 항상 사용하지 않습니다. 미리보기에서는 부드러운 재생을 위해 저화질 프록시를 자동으로 만들 수 있으며, 내보내기는 원본으로 렌더링합니다.</div>""",
       """<p>A <b>Video</b> source plays clips inside the canvas. Adding one, or its settings button in the inspector, opens <b>Video playback settings</b>.</p>
<ol>
<li><b>Where do the videos come from?</b>
<ul><li><b>Use different videos for each track</b>: switches to the videos of whichever track is playing. Add them in <a href='topic:track_videos'>Track information/settings → Videos for this track</a>.</li>
<li><b>Use the same videos across the whole playlist</b>: the videos added here run on the whole-video timeline, independent of tracks.</li></ul></li>
<li><b>How should they play?</b> Once · loop the first video · loop all in order · loop all shuffled, then <b>until the end</b> or a <b>number of cycles</b></li>
<li><b>Speed and look</b>: playback speed, saturation, black and white</li>
</ol>
<div class='note'>Video sound is never used, so it cannot mix into the playlist music. Preview may build low-resolution proxies for smooth playback; Export always renders the originals.</div>""",
       ("track_videos", "sources"),
       ("영상 클립 반복 무작위 재생 속도 흑백 채도 프록시 동영상", "video clip loop shuffle playback speed black and white saturation proxy movie")),

    _e("canvas", "audio_visuals", ("오디오 시각 효과", "Audio-reactive visuals"),
       ("오디오,요소,성능", "audio,sources,performance"),
       """<p>오디오 요소는 실제 재생되는 음악을 분석해 움직입니다.</p>
<ul>
<li><b>비주얼라이저</b>: 주파수 대역을 막대·웨이브·점·선·미러·스펙트럼·LED·센터·캡슐·원호 스타일로 표시. 원형 스타일은 가운데에 원형 앨범 커버를 함께 넣을 수 있습니다.</li>
<li><b>오디오 파형</b>: 곡의 파형을 선·채운 면·위아래 대칭으로 표시하고 재생 진행을 함께 보여 줍니다.</li>
<li><b>레벨 미터</b>: 모노/스테레오 음량과 피크(그라데이션·단색·LED·구간 스타일, 세로·가로)</li>
<li><b>파티클/노이즈</b>: 먼지·네온·노이즈·눈·별·보케·색종이 효과. 밀도·속도·크기·방향·반짝임·광택·색·시드</li>
</ul>
<h2>움직임 다듬기</h2>
<ul>
<li><b>감도</b>: 클수록 작은 소리에도 크게 반응</li>
<li><b>어택 / 릴리즈</b>: 소리가 커질 때 올라가는 속도 / 작아질 때 내려오는 속도</li>
<li><b>스무딩·노이즈 게이트·최소/최대 높이</b>: 떨림을 줄이고 무음일 때의 모양을 정합니다.</li>
</ul>
<div class='note'>오디오 요소가 많거나 크고, 해상도·FPS·파티클 밀도가 높을수록 미리보기와 내보내기가 무거워집니다. <a href='topic:performance'>성능</a>을 참고하세요.</div>""",
       """<p>Audio sources move with the music that is actually playing.</p>
<ul>
<li><b>Visualizer</b>: frequency bands as bars, wave, dots, line, mirror, spectrum, LED, center, capsule or arc. Circular styles can hold a round album cover in the middle.</li>
<li><b>Audio waveform</b>: the track's waveform as a line, filled or mirrored shape, together with playback progress</li>
<li><b>Level meter</b>: mono/stereo level and peak (gradient, solid, LED or segment style; vertical or horizontal)</li>
<li><b>Particles/noise</b>: dust, neon, noise, snow, stars, bokeh or confetti, with density, speed, size, direction, twinkle, glow, colors and seed</li>
</ul>
<h2>Tuning the motion</h2>
<ul>
<li><b>Sensitivity</b>: higher reacts strongly even to quiet sound</li>
<li><b>Attack / release</b>: how fast it rises when the sound gets louder / falls when it gets quieter</li>
<li><b>Smoothing, noise gate, min/max level</b>: calm jitter and set the shape in silence</li>
</ul>
<div class='note'>Many or large audio sources, higher resolution and FPS, and dense particles all make Preview and Export heavier. See <a href='topic:performance'>Performance</a>.</div>""",
       ("sources", "performance"),
       ("비주얼라이저 파형 웨이브폼 레벨 미터 파티클 노이즈 감도 어택 릴리즈 스무딩 반응",
        "visualizer waveform level meter particles noise sensitivity attack release smoothing reactive")),

    _e("canvas", "animation", ("애니메이션", "Animation"),
       ("애니메이션,속성", "animation,properties"),
       """<p>속성 패널의 <b>애니메이션</b> 탭에서 요소가 곡마다 나타나고 사라지는 효과를 정합니다.</p>
<ul>
<li><b>시작 효과 / 종료 효과</b>: 없음, 페이드, 왼쪽·오른쪽·위쪽·아래쪽 슬라이드, 줌, 줌 아웃, 팝, 통통 튀기, 떠오르기, 떨어지기, 회전, 스핀, 흔들며 등장, 뒤집기</li>
<li><b>시작 / 종료 시간</b>: 각 효과가 재생되는 길이(초)</li>
<li>크로스페이드나 AutoMix에서는 두 곡이 겹치는 구간의 가운데에서 화면이 다음 곡으로 넘어갑니다. 관련 옵션으로 종료 효과가 겹침 구간에 맞춰 재생되도록 할 수 있습니다.</li>
</ul>
<p><b>애니메이션 미리보기</b>를 누르면 선택한 요소의 효과가 캔버스에서 바로 재생됩니다. 재생 중에는 편집이 잠기고, 다른 동작을 하면 멈추며, 끝나면 위치와 선택이 원래대로 돌아옵니다.</p>
<p>텍스트·가사 요소에는 <a href='topic:music_reaction'>음악에 맞춰 반응</a>도 있습니다. 가사가 바뀔 때의 효과는 <a href='topic:lyrics_transition'>자막 전환 설정</a>에서 따로 조절합니다.</p>
<div class='note'>배경 요소는 곡이 바뀔 때 이전 배경에서 새 배경으로 부드럽게 넘어가는 <b>곡 전환 크로스페이드</b>를 따로 켤 수 있습니다. 현재 재생 카드는 표시 시간과 퇴장 효과를 따로 가집니다.</div>""",
       """<p>The inspector's <b>Animation</b> tab sets how a source appears and leaves with each track.</p>
<ul>
<li><b>Entrance / exit effect</b>: none, fade, slide left/right/up/down, zoom, zoom out, pop, bounce, float up, drop, rotate, spin, wobble in, flip</li>
<li><b>Entrance / exit duration</b>: how long each effect plays (seconds)</li>
<li>With crossfade or AutoMix the screen switches to the next track in the middle of the overlap; an option lets the exit effect line up with that overlap.</li>
</ul>
<p><b>Preview animation</b> plays the selected source's effects right on the canvas. Editing is locked while it plays, any other action stops it, and position and selection are restored afterwards.</p>
<p>Text and lyric sources also support <a href='topic:music_reaction'>React to music</a>. Effects between lyric cues are configured separately in <a href='topic:lyrics_transition'>Subtitle transition settings</a>.</p>
<div class='note'>A Background source can separately <b>crossfade between tracks</b>, easing from the previous track's background to the next. The Now playing card has its own display time and exit effect.</div>""",
       ("properties", "timeline", "preview_mix"),
       ("등장 퇴장 시작 종료 효과 페이드 슬라이드 줌 팝 애니메이션 미리보기 배경 크로스페이드",
        "entrance exit effect fade slide zoom pop bounce preview animation background crossfade")),

    _e("canvas", "music_reaction", ("텍스트·현재 자막의 음악 반응", "Music reaction for text and current subtitles"),
       ("음악 반응,애니메이션,텍스트,자막", "music reaction,animation,text,subtitles"),
       """<p>텍스트 또는 가사 / 자막 요소를 선택한 뒤 <b>속성 → 애니메이션 → 음악에 맞춰 반응</b>을 켜세요. 토글을 켜면 효과와 세부 조절 항목이 나타납니다.</p>
[[img:canvas_music_reaction]]
<h2>효과와 반응 음역</h2>
<ul>
<li><b>반응 애니메이션</b>: 확대·복귀, 위로 튀기, 가로 늘리기, 기울이기</li>
<li><b>반응 음역</b>: 저음·중음·고음·전체. 킥이나 베이스에 맞추려면 저음을 선택합니다.</li>
<li><b>반응 강도</b>: 최대 움직임. 확대·복귀에서 0.25는 최대 25% 확대를 뜻합니다. 0이면 반응하지 않습니다.</li>
</ul>
<h2>속도와 감도</h2>
<ul>
<li><b>커지는 시간</b>: 작을수록 빠르게 반응합니다. <b>돌아오는 시간</b>: 클수록 천천히 원래 모습으로 돌아옵니다. 둘 다 0이면 즉시 바뀝니다.</li>
<li><b>반응 감도</b>: 작은 소리에도 반응하도록 신호를 키웁니다. 최대 크기는 반응 강도로 정합니다.</li>
<li><b>최소 반응 기준</b>: 약한 신호를 무시합니다. 높이면 강한 비트 위주로 반응합니다.</li>
<li><b>반응 곡선</b>: 부드럽게는 약한 소리를 살리고, 선형은 신호에 비례하며, 강한 비트 중심은 작은 반응을 줄입니다.</li>
<li><b>반응 시간 보정</b>: 양수는 늦추고 음수는 앞당깁니다. 가사 시간 보정과는 별개입니다.</li>
</ul>
<p>예를 들어 저음 + 확대·복귀, 강도 0.25, 커지는 시간 0.06초, 돌아오는 시간 0.4초로 시작해 보세요. 떨림이 많으면 최소 반응 기준을 높이거나 돌아오는 시간을 늘립니다.</p>
<div class='note'>자막은 <b>현재 가사 묶음에만</b> 적용됩니다. 같은 묶음의 여러 텍스트 줄은 함께 움직이고 이전·다음 가사와 인트로 표시는 영향을 받지 않습니다. 고급 자막 모드를 켜지 않아도 사용할 수 있습니다.</div>
<h2>확인하고 끄기</h2>
<p><b>애니메이션 미리보기</b>는 예시 박자로 움직임을 확인하는 기능입니다. 실제 곡의 반응은 메인 <b>미리보기</b> 재생에서 확인하세요. 내보내기에도 적용되며, 무음에서는 원래 모습으로 돌아옵니다. 토글을 끄면 반응은 적용되지 않고 조절 값은 유지됩니다.</p>""",
       """<p>Select a Text or Lyrics / subtitles source, then enable <b>Inspector → Animation → React to music</b>. The effect and tuning controls appear when enabled.</p>
[[img:canvas_music_reaction]]
<h2>Effect and frequency range</h2>
<ul>
<li><b>Reaction effect</b>: scale pulse, upward bounce, horizontal stretch or tilt</li>
<li><b>Frequency range</b>: bass, midrange, treble or full spectrum. Choose bass for kicks and bass instruments.</li>
<li><b>Reaction strength</b>: maximum movement. For scale pulse, 0.25 allows up to 25% enlargement. Zero produces no reaction.</li>
</ul>
<h2>Speed and sensitivity</h2>
<ul>
<li><b>Attack time</b>: smaller reacts faster. <b>Release time</b>: larger returns to the original appearance more slowly. Zero changes instantly.</li>
<li><b>Sensitivity</b>: amplifies quiet signals; strength sets maximum movement.</li>
<li><b>Threshold</b>: ignores weak signals. Raise it to focus on stronger beats.</li>
<li><b>Response curve</b>: soft boosts subtle sounds, linear follows signal strength, punchy suppresses smaller reactions.</li>
<li><b>Reaction offset</b>: positive delays and negative advances the reaction, independently of lyric timing.</li>
</ul>
<p>Try bass + scale pulse, strength 0.25, attack 0.06 s and release 0.4 s. For less jitter, raise the threshold or increase release time.</p>
<div class='note'>Subtitles react only on the <b>current cue</b>. Its text lines move together; previous/next cues and intro indicators are unchanged. Advanced subtitle mode is not required.</div>
<h2>Checking and disabling</h2>
<p><b>Preview animation</b> uses a sample beat. Check the actual song in the main <b>Preview</b> player. Export uses the reaction too; silence returns the source to its original appearance. Turning the toggle off stops the effect while keeping its tuning values.</p>""",
       ("animation", "lyrics_transition", "full_preview"),
       ("베이스 저음 반응 커지는 속도 감소 속도 어택 릴리즈 감도 기준 시간 보정",
        "bass reactive attack release decay speed sensitivity threshold offset")),

    _e("canvas", "lyrics_transition", ("자막 전환과 카테고리별 고급 모드", "Subtitle transitions and advanced categories"),
       ("자막,고급 모드,애니메이션,블러", "subtitles,advanced mode,animation,blur"),
       """<p>가사 / 자막 요소를 선택하고 <b>속성 → 애니메이션 → 자막 전환 고급 설정…</b>을 여세요. 왼쪽 탭은 움직임·줄 배치·줄 스타일·인트로로 나뉘며, 각 탭 안에서 관련 옵션을 묶어 보여 줍니다. 옵션에 마우스를 올리면 툴팁을 볼 수 있습니다.</p>
[[img:lyrics_transition]]
<h2>기본 설정과 고급 설정의 우선순위</h2>
<ol>
<li>간단히 조절하려면 속성 패널의 기본 설정을 사용합니다.</li>
<li>더 세밀하게 조절할 탭에서 <b>고급 모드</b>를 켭니다. 켠 카테고리의 값만 이 창에서 우선 적용됩니다.</li>
<li>같은 카테고리의 기본 항목은 속성 패널에서 회색으로 잠기고 이유가 표시됩니다. 다른 카테고리의 기본 항목은 계속 편집할 수 있습니다.</li>
<li><b>적용</b>으로 저장합니다. 고급 모드가 꺼진 카테고리의 고급 값은 저장해 두되 영상에는 적용하지 않습니다. 다시 끄면 기본 설정으로 돌아갑니다.</li>
</ol>
<h2>움직임</h2>
<p>글로우·라이즈·페이드·슬라이드·줌·순차 이동·바운스·없음 중 선택하고 전환 시간, 이동 방향, 속도 곡선을 조절합니다. <b>아래 → 위</b>는 다음 가사가 아래에서 현재 위치로 올라오고 현재 가사는 위로 이동하는 방향입니다. 나머지 방향도 선택할 수 있습니다.</p>
<p><b>순차 이동</b>의 시차는 다음 가사에만 적용됩니다. 가까운 가사부터 또는 먼 가사부터 움직이도록 선택하세요. 추가 이동 거리, 크기 변화와 글로우 항목은 해당 효과에 맞춰 조절합니다.</p>
<h2>줄 배치</h2>
<p>현재 가사의 기준 위치, 글자 정렬, 이전·다음 가사 묶음 수와 간격을 정합니다. 묶음 하나에 원문·번역 등 여러 텍스트 줄이 있을 수 있습니다. 줄 수 <b>자동</b>은 요소 크기에 맞추고, 0은 해당 쪽 가사를 숨깁니다.</p>
<h2>이전·현재·다음 줄 스타일</h2>
[[img:lyrics_role_styles]]
<p><b>편집할 위치</b>에서 이전·현재·다음을 각각 선택하고 <b>크기 배율·불투명도·블러</b>만 조절합니다. 블러 0은 선명하고 불투명도 1은 불투명합니다. 색상과 글꼴은 속성 패널 및 줄별 스타일에서 설정합니다.</p>
<p><b>거리별 흐림 강도</b>는 이전·다음에 각각 적용합니다. 0은 같은 흐림, 1은 현재 가사에 가까울수록 선명하고 불투명한 모습입니다. 해당 쪽의 블러와 불투명도는 가장 먼 가사의 기준값이 됩니다.</p>
<div class='note'>고급 줄 스타일을 켜도 <a href='topic:lyrics_line_styles'>줄별 스타일</a>의 색상·굵기·기울임·크기는 계속 편집할 수 있습니다. 설정한 기본·줄별 크기에 고급 크기 배율을 곱합니다. 이전 버전의 고급 색상·글꼴 값은 사용하지 않습니다. 현재 자막의 음악 반응은 <a href='topic:music_reaction'>별도 옵션</a>입니다.</div>
<h2>예시 미리보기</h2>
<p>오른쪽에서 실제 렌더러의 전환을 확인하고 <b>다시 재생</b>할 수 있습니다. <b>미리보기 줄 수</b>는 이 창의 예시만 바꿉니다. 영상에 표시할 묶음 수는 줄 배치 탭에서 정하세요. 첫 가사 전·곡 중간 예시는 인트로 탭에서 선택합니다.</p>""",
       """<p>Select a Lyrics / subtitles source and open <b>Inspector → Animation → Advanced subtitle settings…</b>. The left tabs group options into Motion, Layout, Styles and Intro. Hover any option for its tooltip.</p>
[[img:lyrics_transition]]
<h2>Basic and advanced priorities</h2>
<ol>
<li>Use basic inspector settings for simple adjustments.</li>
<li>Enable <b>Advanced mode</b> on the tab you want to customize. Only enabled categories use this dialog's values.</li>
<li>The matching basic controls become gray and show why they are locked. Other categories remain editable in the inspector.</li>
<li>Click <b>Apply</b>. Advanced drafts in disabled categories are retained but do not affect the video. Disable a category to return to its basic settings.</li>
</ol>
<h2>Motion</h2>
<p>Choose Glow, Rise, Fade, Slide, Zoom, Staggered flow, Bounce or None, then tune duration, flow direction and easing. <b>Bottom → top</b> brings upcoming lyrics up into the current position as the current cue moves upward. Other directions are available.</p>
<p><b>Staggered flow</b> delays only upcoming cues. Choose nearest or farthest first. Extra travel, scale and glow controls apply to the corresponding effects.</p>
<h2>Layout</h2>
<p>Set the current cue anchor, text alignment, previous/upcoming cue counts and spacing. A cue can contain several text lines, such as original text and a translation. <b>Auto</b> fits the source size; zero hides that side.</p>
<h2>Previous, current and next styles</h2>
[[img:lyrics_role_styles]]
<p>Select a <b>Role</b> and adjust only <b>size scale, opacity and blur</b> independently. Blur zero is sharp; opacity one is opaque. Set colors and typography in Properties and Per-line styles.</p>
<p><b>Distance falloff</b> is independent for previous and next cues. Zero keeps them uniform; one makes nearby cues clearer and more opaque. That side's blur and opacity define the farthest cue.</p>
<div class='note'><a href='topic:lyrics_line_styles'>Per-line</a> color, weight, italic and size remain editable with advanced roles enabled. The advanced scale multiplies the base/per-line size. Older advanced color and typography values are ignored. Current subtitles have a <a href='topic:music_reaction'>separate music reaction option</a>.</div>
<h2>Sample preview</h2>
<p>The right side uses the real renderer; click <b>Replay</b> to restart. <b>Preview cues</b> changes only the sample in this dialog. Set actual video cue counts under Layout. Choose before-first-lyric or mid-track examples under Intro.</p>""",
       ("lyrics", "lyrics_intro", "lyrics_line_styles", "music_reaction"),
       ("고급 설정 잠금 우선순위 기본 모드 순차 이동 정렬 이전 현재 다음 거리 흐림 블러 투명도",
        "advanced settings priority locked basic mode stagger flow alignment previous current next distance blur opacity")),

    _e("canvas", "lyrics_intro", ("인트로·간주 임시 줄", "Temporary intro and interlude rows"),
       ("자막,인트로,고급 모드", "subtitles,intro,advanced mode"),
       """<p><b>자막 전환 설정 → 인트로</b>에서 이 카테고리의 <b>고급 모드</b>를 켜고 표시할 구간을 선택하세요.</p>
[[img:lyrics_intro]]
<ul>
<li><b>첫 가사 전 대기 표시</b>: 첫 가사 시간이 오기 전 임시 줄을 현재 기준 위치에 표시합니다. 첫 가사는 다음 줄에 보이고, 시작 시 임시 줄이 같은 전환 효과로 빠져나간 뒤 사라집니다.</li>
<li><b>곡 중간의 간주에도 표시</b>: 긴 자막 공백을 보컬 분석하여 음악이 있고 보컬이 없는 구간에만 표시합니다. 이전 가사와 다음 가사 사이에 임시 줄이 추가되고 구간이 끝나면 사라집니다. 긴 자막 공백만으로는 표시되지 않습니다.</li>
<li><b>표시 스타일</b>: 세 점이 차례로 밝아짐, 숨쉬기, 웨이브, 리듬 막대, 회전 링</li>
<li><b>최소 간주 길이</b>: 중간 표시를 위한 최소 무보컬 구간 길이. 첫 가사 전 표시에는 적용하지 않습니다.</li>
<li><b>한 사이클 시간 / 표시 크기</b>: 표시 내부의 반복 속도와 크기입니다. 줄 전체의 이동은 움직임 탭의 전환 설정을 따릅니다.</li>
</ul>
<p>인트로 색상·불투명도는 현재 가사 스타일을 따릅니다. 이 표시는 가사 파일을 수정하지 않습니다. <b>미리보기 구간</b>으로 첫 가사 전 또는 곡 중간 예시를 확인하고, 실제 곡에서는 분석 결과가 준비된 후 메인 미리보기로 확인하세요.</p>""",
       """<p>Open <b>Subtitle transition settings → Intro</b>, enable <b>Advanced mode</b> for this category and choose where to show the indicator.</p>
[[img:lyrics_intro]]
<ul>
<li><b>Show before the first lyric</b>: a temporary row occupies the current cue anchor while the first lyric appears next. When the lyric starts, the row exits with the same transition and disappears.</li>
<li><b>Show during instrumental interludes</b>: long lyric gaps are analyzed for vocals. A row is inserted between previous and next lyrics only while music plays without vocals, then disappears. A long lyric gap alone is insufficient.</li>
<li><b>Indicator style</b>: sequential-light dots, breathing dots, wave dots, rhythm bars or spinning ring</li>
<li><b>Minimum interlude</b>: minimum vocal-free interval for mid-track rows; does not affect the opening wait.</li>
<li><b>Cycle duration / indicator scale</b>: the inner animation speed and size. Motion settings control movement of the whole row.</li>
</ul>
<p>Indicators follow current-lyric color and opacity and do not change the lyric file. <b>Preview segment</b> switches between opening and mid-track examples. Check the actual song in the main Preview after its analysis is ready.</p>""",
       ("lyrics_transition", "track_analysis", "full_preview"),
       ("세 점 대기 간주 보컬 없는 무보컬 차례로 밝아짐 임시 줄", "dots waiting instrumental vocal-free interlude temporary row")),

    _e("canvas", "lyrics_line_styles", ("가사 줄별 스타일", "Per-line lyric styles"),
       ("가사,텍스트,스타일", "lyrics,text,styles"),
       """<p>가사 / 자막 요소의 <b>속성 → 가사 → 줄별 스타일</b>에서 한 가사 묶음 안의 원문·번역 등 텍스트 줄을 다르게 꾸밉니다. 시간순으로 2번째 가사를 고르는 기능이 아니라 <b>각 묶음의 2번째 텍스트 줄</b>에 반복 적용하는 기능입니다.</p>
[[img:lyrics_line_styles]]
<ol>
<li>왼쪽에서 줄을 선택합니다. 1번 줄은 전체 텍스트 스타일을 따르며 목록에서 제외됩니다. 기본 목록은 2·3·4번 줄이고 <b>목록 펼치기</b>로 최대 12번 줄까지 볼 수 있습니다.</li>
<li><b>개별 스타일 사용</b>을 켜고 색상·굵기·기울임·크기를 조절합니다. 크기는 기본 글꼴 크기에 더하거나 빼는 상대 크기 또는 고정 크기로 선택합니다.</li>
<li>2번 줄 편집 시에는 미리보기에 1번 줄도 함께 표시되어 크기와 색상을 비교할 수 있습니다.</li>
<li>개별 설정을 끄면 전체 스타일을 따릅니다. <b>적용</b>으로 저장합니다.</li>
</ol>
<div class='note'>자막 전환의 <b>줄 스타일 고급 모드</b>를 켜도 이 창의 색상·굵기·기울임·크기는 잠기지 않습니다. 기본·줄별 크기에 고급 크기 배율을 곱하고 불투명도·블러를 함께 적용합니다.</div>""",
       """<p>Use <b>Inspector → Lyrics → Per-line styles</b> to style original text, translations and other text lines inside each cue. This targets the <b>second text line of every cue</b>, not the second lyric in time.</p>
[[img:lyrics_line_styles]]
<ol>
<li>Select a line on the left. Line 1 uses the overall text style and is omitted. Lines 2, 3 and 4 appear initially; <b>Expand list</b> reveals lines through 12.</li>
<li>Enable <b>Custom style</b> and adjust color, weight, italic and size. Choose a relative offset added to the base font size or a fixed font size.</li>
<li>Editing line 2 also shows line 1 in the preview so you can compare size and color.</li>
<li>Disable custom styling to inherit the overall style, then click <b>Apply</b> to save.</li>
</ol>
<div class='note'>Advanced <b>Role styles</b> do not lock this dialog's color, weight, italic or size controls. Advanced size scale multiplies the base/per-line size; opacity and blur also apply.</div>""",
       ("lyrics", "lyrics_transition", "properties"),
       ("번역 원문 두번째 줄 2번 줄 목록 펼치기 개별 스타일 글꼴 크기 상대 크기", "translation original second line expand list custom style font size relative offset")),

    _e("canvas", "playlist", ("플레이리스트", "The playlist"),
       ("플레이리스트,음악", "playlist,music"),
       """<p>하단 <b>플레이리스트</b> 탭에서 영상에 들어갈 곡과 순서를 정합니다.</p>
[[img:canvas_playlist]]
<h2>곡 추가</h2>
<ul>
<li><b>+ 음악 추가</b>를 누르거나 음악 파일·M3U8/M3U 플레이리스트를 목록으로 끌어 놓습니다. 지원 형식: MP3, WAV, FLAC, AAC, M4A, OGG.</li>
<li>같은 폴더에 이름이 같은 <code>.lrc</code>/<code>.srt</code>/<code>.vtt</code>가 있으면 가사로 연결합니다(<a href='topic:settings'>설정 → 콘텐츠</a>에서 동작 변경). 이름이 비슷한 파일은 추가할지 물어봅니다.</li>
<li>M3U8을 추가하면 담긴 곡의 아트·제목·아티스트·앨범을 확인하는 창이 먼저 열립니다. 찾을 수 없는 파일과 웹 스트림은 건너뜁니다. <b>파일</b> 메뉴에서 M3U8 가져오기·내보내기를 선택할 수 있습니다. <b>타임라인 내보내기</b>는 믹싱 후 곡 시작 시간과 곡 정보를 설명문·CSV로 저장합니다.</li>
</ul>
<h2>목록 다루기</h2>
<ul>
<li><b>검색</b> 칸으로 제목·아티스트·앨범을 찾습니다.</li>
<li><b>↑ ↓</b>로 순서를 바꾸고, <b>순서 편집</b>은 커버·정보와 미리듣기가 있는 큰 창에서 끌어서 정렬합니다(더블클릭으로 미리듣기, 저장 전에 바뀐 순서를 확인).</li>
<li><b>복제</b>, <b>삭제</b>(<code>Delete</code>, <code>Ctrl+Z</code>로 복구), <code>Space</code> 또는 우클릭으로 <b>내보내기에 포함/제외</b></li>
<li><b>곡 정보/설정</b>(<code>Enter</code> 또는 더블클릭): <a href='topic:track_details'>곡별 정보·분석·가사·영상·음량</a></li>
<li>가사 파일을 곡 위에 끌어 놓으면 그 곡의 가사로 연결됩니다.</li>
</ul>
<p>각 행 오른쪽에는 가사 보정값, 분석된 BPM, 길이가 보입니다. AutoMix 프로젝트에서는 곡 사이에 <b>⟷ 자동 전환</b> / <b>✎ 수동</b> 표시가 나타나며, 누르면 <a href='topic:automix_editor'>AutoMix 편집기</a>가 열립니다.</p>""",
       """<p>The <b>Playlist</b> tab at the bottom holds the songs of the video and their order.</p>
[[img:canvas_playlist]]
<h2>Adding songs</h2>
<ul>
<li>Choose <b>+ Add music</b>, or drop music files or an M3U8/M3U playlist on the list. Supported: MP3, WAV, FLAC, AAC, M4A, OGG.</li>
<li>A <code>.lrc</code>/<code>.srt</code>/<code>.vtt</code> with the same name in the same folder is attached as lyrics (change this in <a href='topic:settings'>Settings → Content</a>); for similarly named files you are asked.</li>
<li>Adding an M3U8 first shows its tracks with art, title, artist and album to confirm. Missing files and web streams are skipped. The <b>File</b> menu imports and exports M3U8. <b>Export timeline</b> saves mixed track start times and track details as a description and CSV.</li>
</ul>
<h2>Working with the list</h2>
<ul>
<li><b>Search</b> finds titles, artists and albums.</li>
<li><b>↑ ↓</b> reorder; <b>Reorder</b> opens a larger window with covers, details and listening, where you drag to sort (double-click to listen; changes are confirmed before saving).</li>
<li><b>Duplicate</b>, <b>Delete</b> (<code>Delete</code>, restore with <code>Ctrl+Z</code>), and <b>include/exclude from export</b> with <code>Space</code> or the context menu</li>
<li><b>Track information/settings</b> (<code>Enter</code> or double-click): <a href='topic:track_details'>per-track info, analysis, lyrics, videos and sound</a></li>
<li>Drop a lyrics file on a track to attach it to that track.</li>
</ul>
<p>Each row shows its lyric offset, analyzed BPM and length. In an AutoMix project, <b>⟷ Auto transition</b> / <b>✎ Manual</b> chips appear between tracks; click one to open the <a href='topic:automix_editor'>AutoMix editor</a>.</p>""",
       ("track_details", "lyrics_files", "automix_playlist", "timeline"),
       ("음악 추가 곡 순서 m3u8 m3u 가져오기 내보내기 검색 복제 삭제 포함 제외 순서 편집 드래그",
        "add music track order m3u8 m3u import export search duplicate delete include exclude reorder drag")),

    _e("canvas", "timeline", ("시각 타임라인과 숫자 편집", "Visual timeline and numeric editing"),
       ("타임라인,플레이리스트,애니메이션", "timeline,playlist,animation"),
       """<p>하단 <b>타임라인</b> 탭은 곡이 전체 영상의 어디에 놓이는지와 요소가 보이는 시간을 보여 줍니다.</p>
[[img:canvas_timeline]]
<h2>막대로 직접 편집하기</h2>
<ol>
<li>상단에서 <b>시각 편집</b>을 선택합니다. 음악 행에는 곡들이, 그 아래에는 요소마다 한 행씩 표시됩니다.</li>
<li>음악 막대의 몸통을 끌어 시작 위치를 바꿉니다. 곡 길이는 유지되고 뒤 곡은 새 위치에 맞춰 배치됩니다. 앞 곡 종료 전으로는 옮길 수 없습니다.</li>
<li>요소 막대를 선택하면 캔버스와 속성 패널도 같은 요소를 선택합니다. 몸통을 끌면 시작 위치, 양끝 손잡이를 끌면 시작·종료를 조절합니다. 잠긴 요소는 이동할 수 없습니다.</li>
<li>마우스를 놓으면 변경이 반영됩니다. 드래그 중 <code>Esc</code>는 취소하고, 반영 후 <code>Ctrl+Z</code>로 실행 취소합니다.</li>
</ol>
<h2>정확하게 맞추고 미리보기</h2>
<ul>
<li><b>스냅</b>: 다른 막대의 시작·끝, 재생 커서와 시간 눈금에 붙입니다. <code>Shift</code>를 누르는 동안 스냅 상태가 반대로 바뀝니다.</li>
<li><b>전체 보기</b>, <b>− / +</b>, <code>Ctrl+휠</code>: 전체 길이에 맞추거나 확대합니다. <code>Shift+휠</code> 또는 마우스 가운데 버튼으로 가로 이동하고, 일반 휠로 요소 목록을 세로 이동합니다.</li>
<li>눈금이나 빈 곳을 누르거나 시간 칸에 값을 넣어 재생 커서를 옮긴 뒤 <b>이 위치 미리보기</b>로 메인 재생 화면에서 확인합니다. 시간은 <code>분:초.소수</code>로 표시됩니다.</li>
<li>선택한 막대는 <code>← / →</code>로 1초씩, <code>Shift+← / →</code>로 0.1초씩 이동합니다. <code>↑ / ↓</code>로 막대를 선택하고 <code>Home</code>으로 커서를 처음으로 옮깁니다.</li>
</ul>
<h2>숫자 편집과 자동 길이</h2>
<p><b>숫자 편집</b>으로 바꾸면 기존 음악 타임라인·소스 타이밍 표에서 시작·지속 시간을 직접 입력할 수 있습니다. 요소의 지속 시간 0은 <b>곡 끝까지</b>, 즉 전체 플레이리스트가 끝날 때까지 표시한다는 뜻입니다. 막대 우클릭에서도 자동 길이로 복원하거나 숫자 편집으로 전환할 수 있습니다.</p>
<div class='note'>크로스페이드·AutoMix를 쓰면 곡이 겹치는 만큼 전체 길이가 줄어듭니다. 실제 전환 위치는 <a href='topic:preview_mix'>미리보기</a>의 타임라인에서 확인하세요.</div>""",
       """<p>The <b>Timeline</b> tab shows where each track sits in the video and when sources are visible.</p>
[[img:canvas_timeline]]
<h2>Edit directly with blocks</h2>
<ol>
<li>Select <b>Visual edit</b>. Music occupies one row, followed by one row per source.</li>
<li>Drag a music block to change its start. Its length stays fixed and following tracks adjust to the new position. It cannot start before the previous track ends.</li>
<li>Select a source block to select the same source on the canvas and in the inspector. Drag its body to move it or its edge handles to change its start/end. Locked sources cannot move.</li>
<li>Release the mouse to commit. Press <code>Esc</code> during a drag to cancel, or <code>Ctrl+Z</code> after committing to undo.</li>
</ol>
<h2>Precise placement and preview</h2>
<ul>
<li><b>Snap</b>: aligns to block edges, the playhead and time ticks. Hold <code>Shift</code> to temporarily invert snapping.</li>
<li><b>Fit all</b>, <b>− / +</b>, <code>Ctrl+wheel</code>: fit the whole duration or zoom. <code>Shift+wheel</code> or middle-button dragging pans horizontally; ordinary wheel scrolling moves through source rows.</li>
<li>Click the ruler or blank space, or enter a time, to position the playhead; use <b>Preview here</b> to open the main player at that time. Times display as <code>minutes:seconds.fraction</code>.</li>
<li><code>Left / Right</code> nudges the selected block by one second; hold <code>Shift</code> for 0.1 s. <code>Up / Down</code> selects blocks; <code>Home</code> resets the playhead.</li>
</ul>
<h2>Numeric editing and automatic duration</h2>
<p>Choose <b>Numeric edit</b> for the existing Music timeline and Source timing tables. Source duration zero means <b>Playlist end</b>. Right-click a block to restore automatic timing or switch to numeric editing.</p>
<div class='note'>Crossfade and AutoMix shorten the video by the overlaps. See the real transition points on the <a href='topic:preview_mix'>Preview</a> timeline.</div>""",
       ("playlist", "animation"),
       ("시각 편집 숫자 편집 드래그 막대 손잡이 스냅 확대 여기서 미리보기 시작 시간 지속 시간 소스 타이밍",
        "visual edit numeric edit drag block handles snap zoom preview here start time duration source timing")),

    _e("canvas", "project_content", ("프로젝트 콘텐츠", "Project content"),
       ("콘텐츠,미디어", "content,media"),
       """<p>왼쪽 <b>프로젝트 콘텐츠</b> 탭은 프로젝트에서 쓰는 이미지·영상·음원·폰트·가사 파일을 모아 두고 다시 쓰는 곳입니다.</p>
[[img:canvas_content]]
<ul>
<li><b>필터</b>로 전체·이미지·영상·오디오·가사/자막·폰트만 보고, <b>보기</b>에서 목록·격자·간단을 고릅니다(다음 실행에도 유지).</li>
<li>이미지·영상은 캔버스로, 음원은 플레이리스트로 끌어 놓습니다. 가사는 적용할 곡 위에 놓습니다.</li>
<li>더블클릭하면 미리봅니다. 오디오·영상 미리보기는 재생·정지, 5초 이동, 위치 탐색, 볼륨·음소거를 지원합니다.</li>
<li>✓ <b>추가됨</b>은 이미 프로젝트에서 쓰는 항목입니다. 파일이 없어진 항목은 <b>파일 없음</b>으로 표시됩니다.</li>
<li>우클릭: 프로젝트에 추가, 미리보기, 정보, 목록에서 제거 · 빈 곳 우클릭 또는 <b>콘텐츠 가져오기</b>로 새 파일 추가</li>
</ul>
<div class='note'>다른 PC로 옮길 프로젝트는 콘텐츠를 <b>프로젝트에 포함</b>해 <code>.pvsproj</code>로 저장하세요.</div>""",
       """<p>The <b>Project Content</b> tab on the left collects the images, videos, audio, fonts and lyrics a project uses, ready to reuse.</p>
[[img:canvas_content]]
<ul>
<li><b>Filter</b> to All, Images, Video, Audio, Lyrics/subtitles or Fonts, and choose List, Grid or Compact under <b>View</b> (remembered).</li>
<li>Drag images and videos onto the canvas and audio onto the playlist; drop lyrics on the track they belong to.</li>
<li>Double-click to preview. Audio and video previews have play/stop, 5-second skips, seeking, volume and mute.</li>
<li>✓ <b>Added</b> marks items the project already uses; <b>File missing</b> marks files that moved or were deleted.</li>
<li>Right-click for add to project, preview, info and remove from list · right-click empty space or <b>Import content</b> to add new files</li>
</ul>
<div class='note'>For a project that moves to another PC, <b>include content in the project</b> and save as <code>.pvsproj</code>.</div>""",
       ("projects", "sources", "playlist"),
       ("라이브러리 미디어 재사용 이미지 오디오 영상 폰트 가사 미리보기 파일 없음 가져오기",
        "library media reuse image audio video font lyrics preview missing import")),

    _e("canvas", "presets", ("디자인 프리셋", "Design presets"),
       ("디자인,프로젝트", "design,project"),
       """<h2>디자인 프리셋</h2>
<p><b>프로젝트 → 디자인 프리셋</b>에서 오로라·카세트·미드나잇·느와르 등 완성된 레이아웃을 미리보고 적용합니다. 캔버스의 모든 요소가 프리셋 요소로 바뀌고 플레이리스트는 유지되며, <code>Ctrl+Z</code>로 되돌릴 수 있습니다. 프리셋은 현재 캔버스 비율에 맞게 배치됩니다.</p>
<p><b>프로젝트 → 현재 캔버스를 프리셋으로 저장</b>하면 나만의 프리셋이 생깁니다. 사용자 프리셋은 선택 창에서 삭제하거나 내보낼 수 있습니다.</p>""",
       """<h2>Design presets</h2>
<p><b>Project → Design Presets</b> previews and applies finished layouts such as Aurora, Cassette, Midnight and Noir. Every canvas source is replaced by the preset's, the playlist stays, and <code>Ctrl+Z</code> undoes it. Presets adapt to the current canvas ratio.</p>
<p><b>Project → Save current canvas as preset</b> makes your own preset; user presets can be deleted or exported from the picker.</p>""",
       ("start", "projects"),
       ("프리셋 템플릿 레이아웃 사용자 프리셋 저장",
        "preset template layout user preset save")),

    _e("canvas", "canvas_shortcuts", ("메인 창 단축키", "Main window shortcuts"),
       ("단축키,캔버스", "shortcuts,canvas"),
       """<table cellspacing='4'>
<tr><th>키</th><th>동작</th></tr>
<tr><td><code>Ctrl+N</code> / <code>Ctrl+O</code></td><td>새 프로젝트 / 열기</td></tr>
<tr><td><code>Ctrl+S</code> / <code>Ctrl+Shift+S</code></td><td>저장 / 다른 이름으로 저장</td></tr>
<tr><td><code>Ctrl+E</code></td><td>영상 내보내기</td></tr>
<tr><td><code>F1</code></td><td>도움말(현재 창·패널에 맞는 주제)</td></tr>
<tr><td><code>Ctrl+Alt+L</code> / <code>R</code> / <code>B</code></td><td>왼쪽 / 속성 / 하단 패널 표시 전환</td></tr>
<tr><td><code>Ctrl+Alt+1</code> / <code>2</code> / <code>3</code></td><td>플레이리스트 / 타임라인 / 미리보기</td></tr>
<tr><td><code>Enter</code> · <code>Space</code> · <code>Delete</code></td><td>(플레이리스트) 곡 정보/설정 · 포함/제외 · 삭제</td></tr>
<tr><td><code>Ctrl+X</code> / <code>C</code> / <code>V</code> · <code>Ctrl+D</code></td><td>잘라내기 / 복사 / 붙여넣기 · 복제</td></tr>
<tr><td><code>Ctrl+A</code> · <code>Esc</code></td><td>전체 선택 · 선택 해제</td></tr>
<tr><td><code>Delete</code> / <code>Backspace</code> · <code>F2</code></td><td>선택 요소 삭제 · 이름 바꾸기</td></tr>
<tr><td><code>Ctrl+Z</code> / <code>Ctrl+Shift+Z</code></td><td>실행 취소 / 다시 실행</td></tr>
<tr><td>방향키 · <code>Shift</code>+방향키</td><td>1px · 10px 이동</td></tr>
<tr><td><code>Ctrl+Shift+H</code> / <code>V</code></td><td>가로 / 세로 중앙</td></tr>
<tr><td><code>Ctrl+]</code> / <code>Ctrl+[</code></td><td>맨 앞으로 / 맨 뒤로</td></tr>
<tr><td><code>Ctrl+G</code> / <code>Ctrl+Shift+G</code> · <code>Ctrl+L</code></td><td>그룹 / 그룹 해제 · 잠금</td></tr>
<tr><td><code>Space</code>+끌기 · <code>Ctrl</code>+휠</td><td>시점 이동 · 확대/축소</td></tr>
<tr><td><code>Home</code> / <code>F</code> / <code>Ctrl+0</code> · <code>Ctrl++</code> / <code>Ctrl+-</code></td><td>캔버스 맞춤 · 확대 / 축소</td></tr>
</table>
<p>전체 목록은 <b>도움말 → 단축키 안내</b>에도 있습니다. 텍스트 입력 칸에 커서가 있으면 일반 입력이 우선합니다.</p>""",
       """<table cellspacing='4'>
<tr><th>Key</th><th>Action</th></tr>
<tr><td><code>Ctrl+N</code> / <code>Ctrl+O</code></td><td>New project / open</td></tr>
<tr><td><code>Ctrl+S</code> / <code>Ctrl+Shift+S</code></td><td>Save / save as</td></tr>
<tr><td><code>Ctrl+E</code></td><td>Export video</td></tr>
<tr><td><code>F1</code></td><td>Help (the topic for the current window or panel)</td></tr>
<tr><td><code>Ctrl+Alt+L</code> / <code>R</code> / <code>B</code></td><td>Toggle left / inspector / bottom panel</td></tr>
<tr><td><code>Ctrl+Alt+1</code> / <code>2</code> / <code>3</code></td><td>Playlist / Timeline / Preview</td></tr>
<tr><td><code>Enter</code> · <code>Space</code> · <code>Delete</code></td><td>(Playlist) track info/settings · include/exclude · remove</td></tr>
<tr><td><code>Ctrl+X</code> / <code>C</code> / <code>V</code> · <code>Ctrl+D</code></td><td>Cut / copy / paste · duplicate</td></tr>
<tr><td><code>Ctrl+A</code> · <code>Esc</code></td><td>Select all · clear selection</td></tr>
<tr><td><code>Delete</code> / <code>Backspace</code> · <code>F2</code></td><td>Delete sources · rename</td></tr>
<tr><td><code>Ctrl+Z</code> / <code>Ctrl+Shift+Z</code></td><td>Undo / redo</td></tr>
<tr><td>Arrows · <code>Shift</code>+arrows</td><td>Nudge 1 px · 10 px</td></tr>
<tr><td><code>Ctrl+Shift+H</code> / <code>V</code></td><td>Center horizontally / vertically</td></tr>
<tr><td><code>Ctrl+]</code> / <code>Ctrl+[</code></td><td>Bring to front / send to back</td></tr>
<tr><td><code>Ctrl+G</code> / <code>Ctrl+Shift+G</code> · <code>Ctrl+L</code></td><td>Group / ungroup · lock</td></tr>
<tr><td><code>Space</code>+drag · <code>Ctrl</code>+wheel</td><td>Pan · zoom</td></tr>
<tr><td><code>Home</code> / <code>F</code> / <code>Ctrl+0</code> · <code>Ctrl++</code> / <code>Ctrl+-</code></td><td>Fit canvas · zoom in / out</td></tr>
</table>
<p>The full list is also under <b>Help → Keyboard shortcuts</b>. While a text field has the cursor, typing wins.</p>""",
       ("shortcuts", "canvas_editing"),
       ("키보드 단축키 핫키", "keyboard shortcuts hotkeys")),
)


# ---------------------------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------------------------

_PREVIEW = (
    _e("preview", "full_preview", ("미리보기 화면", "The Preview screen"),
       ("미리보기,재생", "preview,playback"),
       """<p><b>미리보기</b>는 플레이리스트 전체를 실제 음악과 함께 캔버스 자리에서 재생해, 내보내기 전에 결과를 확인하는 화면입니다. 하단의 <b>미리보기</b> 탭, <b>보기 → 미리보기</b> 또는 <code>Ctrl+Alt+3</code>으로 엽니다.</p>
[[img:preview_screen]]
<ol>
<li><b>미리보기 화면</b> — 가사·곡 정보·진행 바·오디오 반응 효과·애니메이션이 실제 재생 시간에 맞춰 움직입니다.</li>
<li><b>곡 목록 · 성능 정보</b> — 오른쪽 곡 목록 패널과 렌더링 상태 표시줄을 켜고 끕니다.</li>
<li><b>곡 목록</b> — 곡을 누르면 그 곡의 처음으로 이동합니다. 재생 중인 곡이 강조됩니다.</li>
<li><b>현재 곡</b> — 곡 번호·제목·아티스트와 곡 안의 재생 위치</li>
<li><b>재생 타임라인</b> — 전체 영상 기준 위치. 숫자는 곡 경계, 색 막대는 곡이 겹치는 <a href='topic:preview_mix'>전환 구간</a>입니다.</li>
<li><b>볼륨 · 재생 조작 · 편집으로 돌아가기</b></li>
<li><b>믹스 상태</b>(크로스페이드·AutoMix 프로젝트) — 믹스 준비 상태와 <a href='topic:transition_details'>전환 자세히 보기</a></li>
</ol>
<h2>여는 동안과 여는 중에</h2>
<ul>
<li>여는 동안 <b>미리보기 준비</b> 창이 단계별 진행을 보여 줍니다(오디오 믹스, 화면 구성, 저장된 곡 분석 불러오기).</li>
<li>미리보기 중에는 프로젝트가 바뀌지 않도록 편집이 잠깁니다. 도움말(<code>F1</code>)과 단축키 안내는 계속 쓸 수 있습니다.</li>
<li><b>편집으로 돌아가기</b>, 또는 하단의 플레이리스트·타임라인 탭을 누르면 편집 화면으로 돌아옵니다.</li>
</ul>
<div class='note'>미리보기는 부드러운 재생을 위해 최종 해상도보다 낮게 렌더링될 수 있습니다. 내보낸 영상은 선택한 출력 해상도로 선명하게 만들어집니다.</div>""",
       """<p><b>Preview</b> plays the whole playlist with its music where the canvas was, so you can check the result before exporting. Open it with the <b>Preview</b> tab at the bottom, <b>View → Preview</b> or <code>Ctrl+Alt+3</code>.</p>
[[img:preview_screen]]
<ol>
<li><b>Preview screen</b> — lyrics, track info, progress, audio-reactive visuals and animations move with the real playback time.</li>
<li><b>Track list · Performance</b> — show or hide the track list panel and the rendering status bar.</li>
<li><b>Track list</b> — click a track to jump to its start; the playing track is highlighted.</li>
<li><b>Now playing</b> — track number, title, artist and position within the track</li>
<li><b>Playback timeline</b> — position in the whole video. Numbers mark track boundaries; colored bars are the <a href='topic:preview_mix'>transitions</a> where tracks overlap.</li>
<li><b>Volume · transport · Back to editing</b></li>
<li><b>Mix status</b> (crossfade and AutoMix projects) — how far the mix is and <a href='topic:transition_details'>transition details</a></li>
</ol>
<h2>Opening and while it runs</h2>
<ul>
<li>While it opens, a <b>Preparing preview</b> window lists each step (audio mix, screen, loading saved track analysis).</li>
<li>Editing is locked during Preview so the project cannot change. Help (<code>F1</code>) and the shortcut list still work.</li>
<li><b>Back to editing</b>, or the Playlist or Timeline tab, returns to the editor.</li>
</ul>
<div class='note'>Preview may render below the final resolution to play smoothly. The exported video is rendered sharp at the output resolution you choose.</div>""",
       ("preview_controls", "preview_mix", "preview_quality"),
       ("전체 미리보기 재생 편집 잠금 편집으로 돌아가기 곡 목록", "full preview playback editing locked back to editing track list")),

    _e("preview", "preview_controls", ("재생 조작과 단축키", "Playback controls and shortcuts"),
       ("미리보기,재생,단축키", "preview,playback,shortcuts"),
       """<table cellspacing='4'>
<tr><th>조작</th><th>동작</th></tr>
<tr><td><code>Space</code> · ▶ 재생</td><td>재생 / 일시정지</td></tr>
<tr><td><code>←</code> / <code>→</code> · −5s / +5s</td><td>5초 뒤로 / 앞으로</td></tr>
<tr><td><code>Shift+←</code> / <code>Shift+→</code> · |◀ / ▶|</td><td>이전 곡 / 다음 곡</td></tr>
<tr><td><code>↑</code> / <code>↓</code> · 볼륨 슬라이더</td><td>볼륨 올리기 / 내리기</td></tr>
<tr><td>재생 타임라인 클릭·끌기</td><td>원하는 위치로 이동</td></tr>
<tr><td>곡 목록의 곡 클릭</td><td>그 곡의 처음으로 이동</td></tr>
</table>
<ul>
<li>볼륨은 곡 정보/설정의 미리듣기, 가사 편집기와 같은 값을 공유합니다.</li>
<li>재생 중에도 곡 목록 패널을 접으면 화면이 넓어집니다.</li>
</ul>""",
       """<table cellspacing='4'>
<tr><th>Control</th><th>Action</th></tr>
<tr><td><code>Space</code> · ▶ Play</td><td>Play / pause</td></tr>
<tr><td><code>←</code> / <code>→</code> · −5s / +5s</td><td>Back / forward 5 seconds</td></tr>
<tr><td><code>Shift+←</code> / <code>Shift+→</code> · |◀ / ▶|</td><td>Previous / next track</td></tr>
<tr><td><code>↑</code> / <code>↓</code> · volume slider</td><td>Volume up / down</td></tr>
<tr><td>Click or drag the playback timeline</td><td>Seek</td></tr>
<tr><td>Click a track in the track list</td><td>Jump to its start</td></tr>
</table>
<ul>
<li>The volume is shared with listening in Track information/settings and the lyrics editor.</li>
<li>Fold the track list panel for a larger picture, even while playing.</li>
</ul>""",
       ("full_preview", "shortcuts"),
       ("스페이스 일시정지 탐색 이동 볼륨 이전 곡 다음 곡 5초", "space pause seek volume previous next track 5 seconds")),

    _e("preview", "preview_mix", ("곡 전환과 믹스 상태", "Transitions and mix status"),
       ("미리보기,AutoMix,곡 전환", "preview,AutoMix,transitions"),
       """<p>프로젝트의 곡 전환이 <b>크로스페이드</b>나 <b>AutoMix</b>이면(또는 곡별 음량·EQ를 바꿨으면) 미리보기는 내보내기와 같은 방식으로 섞은 <b>믹스 오디오</b>를 들려줍니다. 믹스를 만들려면 <a href='topic:ffmpeg'>FFmpeg</a>가 필요합니다.</p>
<h2>믹스 상태 표시</h2>
<ul>
<li><b>● 준비 중</b>: 믹스를 만드는 동안 곡을 차례로 재생합니다. 준비되면 재생 중인 위치에서 믹스로 자동 전환됩니다.</li>
<li><b>◐ 임시 계획</b>(AutoMix): 분석이 끝난 곡까지 먼저 섞어 들려주고, 나머지는 분석이 끝나는 대로 넓혀 갑니다.</li>
<li><b>✓ 최종 계획</b>: 내보내기와 같은 최종 믹스입니다.</li>
<li><b>! 대체</b>: 믹스를 만들지 못했거나 일부 곡만 섞였을 때. 섞기 어려운 곡은 기본 크로스페이드나 이어서 재생으로 연결됩니다. 원인은 로그에 남습니다.</li>
</ul>
<p>AutoMix 분석 진행(리듬·보컬, 곡 구조, 미리보기 믹스, 최종 믹스·음량 보정)은 상태 표시줄의 작업 진행에도 나옵니다. 분석 결과는 캐시에 저장되어 다음부터는 빠르게 열립니다.</p>
<h2>화면 전환 시점</h2>
<p>곡이 겹치는 구간에서 캔버스(가사·커버·곡 정보)는 겹침의 가운데에서 다음 곡으로 넘어갑니다. 타임라인의 색 막대로 전환 위치를 확인하세요.</p>
<div class='note'>AutoMix 편집기에서 전환을 고치고 닫으면, 열려 있는 미리보기가 새 설정으로 한 번 다시 섞입니다.</div>""",
       """<p>When the project's transitions are <b>crossfade</b> or <b>AutoMix</b> (or a track's volume/EQ was changed), Preview plays a <b>mixed audio</b> made the same way as Export. Mixing needs <a href='topic:ffmpeg'>FFmpeg</a>.</p>
<h2>Mix status</h2>
<ul>
<li><b>● Preparing</b>: tracks play one after another while the mix is made; once ready it switches over at the current position.</li>
<li><b>◐ Provisional</b> (AutoMix): tracks analyzed so far are mixed first, and the mix grows as analysis finishes.</li>
<li><b>✓ Final plan</b>: the same final mix as Export.</li>
<li><b>! Fallback</b>: the mix failed or covers only some tracks. Hard-to-mix tracks join with a plain crossfade or back to back; the reason is logged.</li>
</ul>
<p>AutoMix analysis (beats and vocals, song structure, preview mix, final mix and loudness) also shows in the status bar's progress. Results are cached, so the next opening is quick.</p>
<h2>When the picture switches</h2>
<p>Where tracks overlap, the canvas (lyrics, cover, track info) switches to the next track in the middle of the overlap. The colored bars on the timeline show where.</p>
<div class='note'>After you change transitions in the AutoMix editor and close it, an open Preview re-mixes once with the new settings.</div>""",
       ("transition_details", "automix", "project_settings"),
       ("크로스페이드 믹스 준비 임시 계획 최종 계획 대체 분석 캐시 화면 전환", "crossfade mix preparing provisional final fallback analysis cache canvas switch")),

    _e("preview", "transition_details", ("전환 살펴보기 창", "The transition details window"),
       ("미리보기,AutoMix,곡 전환", "preview,AutoMix,transitions"),
       """<p>미리보기 오른쪽의 <b>AutoMix 전환 자세히 보기</b>를 누르면 모든 전환을 그림으로 보여 주는 창이 열립니다. 미리보기와 재생 위치가 연결되어 있습니다.</p>
[[img:preview_transition_details]]
<ol>
<li><b>전체 믹스</b> — 곡이 어디서 겹치는지 한 줄로 보여 줍니다. 색칠된 구간이 전환이며, 누르면 그 위치로 이동합니다.</li>
<li><b>전환 목록</b> — 곡 쌍, 시작 시각, 방식(스타일)과 길이. <b>재생 중인 전환 따라가기</b>를 켜면 재생 위치에 맞춰 선택이 바뀝니다. ✎는 직접 설정한 전환입니다.</li>
<li><b>선택한 전환</b> — 방식 설명, 시작, 믹스 길이, 화면 전환 시각. <b>4초 전부터 듣기</b> / <b>시작점으로 이동</b></li>
<li><b>전환 흐름 / 분석 정보</b> — 저음·중음·고음 대역별 두 곡의 음량 곡선(실선: 나가는 곡, 점선: 들어오는 곡)과 아웃트로·마지막 보컬·인트로 끝 같은 표시, 그리고 선택 근거와 BPM·키·신뢰도 같은 수치</li>
<li><b>재생 조작</b> — 이전/다음 전환, ±5초, 재생, <b>전환 반복</b>, 음량</li>
<li><b>정보 복사</b> — 전환 정보를 텍스트로 복사(문의·기록용)</li>
</ol>""",
       """<p><b>AutoMix transition details</b> on the right of Preview opens a window that draws every transition. It shares the playback position with Preview.</p>
[[img:preview_transition_details]]
<ol>
<li><b>Whole mix</b> — where the tracks overlap, in one strip. Colored spans are transitions; click to jump there.</li>
<li><b>Transition list</b> — track pair, start time, style and length. <b>Follow the playing transition</b> keeps the selection with playback. ✎ marks one set by hand.</li>
<li><b>Selected transition</b> — what the style does, start, mix length and the canvas switch time. <b>Listen from 4 s before</b> / <b>Go to start</b></li>
<li><b>Transition flow / Analysis</b> — each song's level per low/mid/high band (solid: outgoing, dashed: incoming) with markers such as outro, last vocal and intro end; plus why it was chosen and values like BPM, key and confidence</li>
<li><b>Transport</b> — previous/next transition, ±5 s, play, <b>loop transition</b>, volume</li>
<li><b>Copy info</b> — copy the transition data as text (for reports or notes)</li>
</ol>""",
       ("preview_mix", "automix_styles", "automix_editor"),
       ("전환 상세 그래프 대역 곡선 선택 근거 전체 믹스 정보 복사 듣기", "transition details graph band curves reasons whole mix copy info listen")),

    _e("preview", "preview_quality", ("미리보기 성능과 렌더러", "Preview performance and renderer"),
       ("미리보기,성능", "preview,performance"),
       """<p><b>성능 정보</b> 버튼을 켜면 미리보기 위에 렌더링 상태가 표시됩니다: 렌더 배율, GPU 메모리·텍스처, 화면 표시 지연, 영상 디코더 부하 등.</p>
<h2>GPU 레이어와 CPU 모드</h2>
<ul>
<li><b>GPU 레이어(권장)</b>: 정적 캔버스와 움직이는 요소를 GPU 텍스처로 유지하고 레이어 순서대로 합성합니다. 부하에 따라 렌더 해상도와 영상 디코더 해상도·FPS를 자동으로 조절합니다.</li>
<li><b>CPU 호환 모드</b>: 그래픽 드라이버와 충돌할 때만 선택하세요. <b>도구 → 설정 → 일반 → 미리보기 렌더러</b>에서 바꾸며, 프로그램을 다시 시작해야 적용됩니다.</li>
<li>GPU 미리보기를 시작하지 못하면 알림과 함께 자동으로 CPU 모드로 전환됩니다. <b>자세히</b>로 원인을 볼 수 있습니다.</li>
</ul>
<h2>미리보기가 끊길 때</h2>
<ul><li>동시에 보이는 비주얼라이저·파형·파티클 수와 파티클 밀도를 줄입니다.</li>
<li>큰 영상 요소가 많으면 프록시 준비가 끝날 때까지 기다립니다.</li></ul>
<div class='note'>미리보기의 화질·해상도는 최종 내보내기에 영향을 주지 않습니다.</div>""",
       """<p>The <b>Performance</b> button shows a rendering status bar above Preview: render scale, GPU memory and textures, present latency, video decoder load and so on.</p>
<h2>GPU layers and CPU mode</h2>
<ul>
<li><b>GPU layers (recommended)</b>: keeps the static canvas and moving sources as GPU textures and composes them in layer order, adjusting render resolution and video decoder resolution/FPS to the load.</li>
<li><b>CPU compatibility mode</b>: choose it only if the graphics driver misbehaves. Switch it in <b>Tools → Settings → General → Preview renderer</b>; it applies after a restart.</li>
<li>If GPU preview cannot start, it switches to CPU mode by itself and says so; <b>Details</b> shows why.</li>
</ul>
<h2>If Preview stutters</h2>
<ul><li>Show fewer visualizers, waveforms and particles at once, and lower particle density.</li>
<li>With many large videos, wait for their proxies to finish.</li></ul>
<div class='note'>Preview quality and resolution never change the exported video.</div>""",
       ("performance", "settings"),
       ("성능 정보 GPU CPU 렌더러 끊김 느림 프록시 FPS", "performance GPU CPU renderer stutter slow proxy FPS")),
)


# ---------------------------------------------------------------------------------------------
# Lyrics editor: the LRC File Generator
# ---------------------------------------------------------------------------------------------

_LYRICS = (
    _e("lyrics_editor", "lrc_generator", ("가사 편집기 개요", "Lyrics editor overview"),
       ("가사,LRC", "lyrics,LRC"),
       """<p><b>가사 편집기</b>(LRC 파일 생성기)는 노래를 들으면서 가사 줄마다 시간을 기록해 <b>동기화 가사(LRC)</b>를 만드는 창입니다.</p>
<h2>여는 방법</h2>
<ul>
<li><b>도구 → LRC 파일 생성기</b>: 새 LRC 파일을 만듭니다. 4단계: 오디오 선택 → 가사 입력 → 타이밍 기록 → 확인 및 저장</li>
<li><b>곡 정보/설정 → 가사 설정 → LRC 생성기로 편집</b>: 그 곡의 오디오와 현재 가사·타이밍을 불러와 고칩니다. 3단계: 가사 입력 → 타이밍 기록 → 확인 및 적용. <b>완료</b>를 누르면 곡에 바로 적용됩니다.</li>
</ul>
[[img:lyrics_timing]]
<p>창 위쪽에 현재 단계와 설명, 단계 점(○●)이 보이고, 아래쪽 <b>&lt; 이전</b> / <b>다음 &gt;</b>로 단계를 오갑니다. 타이밍 기록 단계의 번호는 <a href='topic:lrc_timing'>타이밍 기록</a>에서 설명합니다.</p>
<div class='note'>가사 입력과 타이밍은 오디오별 복구 초안으로 자동 저장됩니다. <a href='topic:lrc_recovery'>자동 저장과 복구</a>를 참고하세요.</div>""",
       """<p>The <b>lyrics editor</b> (LRC File Generator) makes <b>synchronized lyrics (LRC)</b> by recording a time for each line while you listen.</p>
<h2>Opening it</h2>
<ul>
<li><b>Tools → LRC File Generator</b>: make a new LRC file in four steps: choose audio → enter lyrics → record timing → review and save</li>
<li><b>Track information/settings → Lyrics settings → Edit in LRC Generator</b>: load that track's audio and current lyrics and timing to fix them, in three steps: enter lyrics → record timing → review and apply. <b>Done</b> applies the result to the track.</li>
</ul>
[[img:lyrics_timing]]
<p>The top shows the current step, its description and step dots (○●); <b>&lt; Back</b> / <b>Next &gt;</b> at the bottom move between steps. The numbers on the timing step are explained under <a href='topic:lrc_timing'>Recording timing</a>.</p>
<div class='note'>Lyrics and timing are autosaved as a recovery draft per audio file. See <a href='topic:lrc_recovery'>Autosave and recovery</a>.</div>""",
       ("lrc_audio", "lrc_input", "lrc_timing", "lrc_review"),
       ("LRC 파일 생성기 가사 만들기 싱크 동기화 단계", "LRC file generator make lyrics sync synchronize steps")),

    _e("lyrics_editor", "lrc_audio", ("1단계 · 오디오 선택", "Step 1 · Choose the audio"),
       ("가사,LRC", "lyrics,LRC"),
       """[[img:lyrics_audio]]
<ul>
<li>목록에서 <b>프로젝트 콘텐츠</b>의 음원을 고르거나, <b>로컬에서 선택하기…</b> / <b>찾아보기…</b>로 컴퓨터의 오디오 파일을 고릅니다.</li>
<li>곡 제목과 아티스트는 오디오 메타데이터에서 자동으로 가져옵니다(다음 단계에서 수정 가능).</li>
<li>고른 곡에 이미 가사가 적용되어 있으면 <b>새로 작성</b>할지, <b>적용된 가사 불러오기</b>로 이어서 고칠지 묻습니다.</li>
</ul>""",
       """[[img:lyrics_audio]]
<ul>
<li>Pick a song from <b>Project content</b> in the list, or choose a file on your computer with <b>Choose from this computer…</b> / <b>Browse…</b>.</li>
<li>Title and artist come from the audio's metadata (editable in the next step).</li>
<li>If the song already has lyrics applied, you are asked whether to <b>write new</b> lyrics or <b>load the applied lyrics</b> and continue from them.</li>
</ul>""",
       ("lrc_generator", "lrc_input"),
       ("오디오 파일 선택 음원 프로젝트 콘텐츠 기존 가사", "audio file choose project content existing lyrics")),

    _e("lyrics_editor", "lrc_input", ("2단계 · 가사 입력", "Step 2 · Enter the lyrics"),
       ("가사,LRC", "lyrics,LRC"),
       """[[img:lyrics_input]]
<ul>
<li><b>가사 단위</b>: <b>한 줄마다 한 가사</b> 또는 <b>빈 줄마다 한 가사(여러 줄 지원)</b>. 여러 줄 모드에서는 한 가사 안에서 줄을 바꾸고, 다음 가사와는 빈 줄로 구분합니다.</li>
<li><b>곡 제목·아티스트</b>: LRC의 <code>[ti:]</code>·<code>[ar:]</code> 태그로 저장됩니다.</li>
<li><b>입력 전처리 옵션</b>
<ul><li><b>빈 줄 구분 무시</b>: 모든 줄을 개별 가사로 처리</li>
<li><b>[ ]로 묶인 줄 무시</b>: <code>[Chorus]</code> 같은 표시 줄을 빼기</li>
<li><b>고급: 정규식으로 각 줄의 내용 제거</b>: 예 <code>^\\d+\\.\\s*</code>(줄 번호), <code>\\(.*?\\)</code>(괄호 속 내용)</li></ul></li>
</ul>
<p><b>다음</b>을 누르면 가사 줄이 준비됩니다. 이미 기록한 타이밍이 있을 때 문구만 고쳤다면 기존 타이밍이 유지되고, 줄 구성이 바뀌면 타이밍을 다시 준비할지 묻습니다.</p>""",
       """[[img:lyrics_input]]
<ul>
<li><b>Lyric unit</b>: <b>one lyric per line</b> or <b>one lyric per blank-line block (multi-line)</b>. In multi-line mode, break lines inside a lyric and separate lyrics with a blank line.</li>
<li><b>Title and artist</b>: saved as the LRC <code>[ti:]</code> and <code>[ar:]</code> tags.</li>
<li><b>Input clean-up</b>
<ul><li><b>Ignore blank-line blocks</b>: treat every line as its own lyric</li>
<li><b>Ignore lines in [ ]</b>: drop marker lines such as <code>[Chorus]</code></li>
<li><b>Advanced: remove text from each line with a regular expression</b>: for example <code>^\\d+\\.\\s*</code> (line numbers) or <code>\\(.*?\\)</code> (text in parentheses)</li></ul></li>
</ul>
<p><b>Next</b> prepares the lines. If you only changed wording, recorded timing is kept; if the lines themselves changed, you are asked before timing is reset.</p>""",
       ("lrc_timing", "lrc_generator"),
       ("가사 입력 여러 줄 빈 줄 정규식 대괄호 전처리 제목 아티스트", "enter lyrics multi-line blank line regular expression regex brackets clean-up title artist")),

    _e("lyrics_editor", "lrc_timing", ("3단계 · 타이밍 기록", "Step 3 · Record the timing"),
       ("가사,LRC,타이밍", "lyrics,LRC,timing"),
       """<p>음악을 재생하면서 각 가사가 시작되는 순간 <code>Space</code>를 누르면 그 줄에 재생 시간이 기록되고 다음 줄로 넘어갑니다.</p>
[[img:lyrics_timing]]
<ol>
<li><b>단계 표시</b> — 현재 단계와 할 일</li>
<li><b>가사 표</b> — 번호·시간·가사. <b>▶ 희미한 행</b>: 다음에 기록될 줄, <b>♪ 노란 행</b>: 지금 재생 위치의 가사, <b>● 원</b>: 선택한 줄</li>
<li><b>현재 줄 기록 [Space]</b> — 다음 기록 위치에 지금 시간을 기록. <b>선택 줄을 기록 위치로</b>는 선택한 줄부터 다시 기록합니다.</li>
<li><b>도구</b>
<ul><li>실행 이력: 기록 취소(<code>Ctrl+Z</code>) / 다시 실행(<code>Ctrl+Y</code>)</li>
<li>선택 시간 조정: −0.1초 / +0.1초, 시간 삭제, 전체 초기화</li>
<li>가사 편집: 선택한 줄 다음에 가사 추가, 가사 편집(<code>F2</code>), 가사 삭제(<code>Delete</code>)</li></ul></li>
<li><b>입력 지연 보정</b> — 버튼을 늦게 누르는 편이면 음수 값(ms)을 넣어 기록 시간을 앞당깁니다.</li>
<li><b>음악 재생</b> — 위치 슬라이더, −3초 / 재생(<code>Ctrl+Space</code>) / +3초 / 정지, <code>←</code>/<code>→</code>는 1초 이동. <b>미리보기 모드</b>는 편집을 잠그고 현재 가사를 표 가운데에서 따라가며 결과를 확인합니다. 볼륨은 미리보기와 공유됩니다.</li>
<li><b>이전 · 다음 · 취소</b></li>
</ol>
<div class='note'>가사 입력칸 등에 커서가 있을 때는 <code>Space</code>와 실행 취소가 일반 텍스트 편집에 쓰입니다.</div>""",
       """<p>Play the music and press <code>Space</code> the moment each lyric starts: the playback time is written to that line and the next line comes up.</p>
[[img:lyrics_timing]]
<ol>
<li><b>Step header</b> — the current step and what to do</li>
<li><b>Lyrics table</b> — number, time and lyric. <b>▶ faint row</b>: the next line to record, <b>♪ yellow row</b>: the lyric at the playback position, <b>● circle</b>: the selected line</li>
<li><b>Record current line [Space]</b> — writes the current time to the next line. <b>Use selected as cursor</b> re-records from the selected line.</li>
<li><b>Tools</b>
<ul><li>History: undo (<code>Ctrl+Z</code>) / redo (<code>Ctrl+Y</code>)</li>
<li>Adjust selected time: −0.1 s / +0.1 s, delete time, reset all</li>
<li>Edit lyrics: add a lyric after the selected line, edit (<code>F2</code>), delete (<code>Delete</code>)</li></ul></li>
<li><b>Input latency offset</b> — if you tend to press late, enter a negative value (ms) to move recorded times earlier.</li>
<li><b>Music playback</b> — position slider, −3 s / Play (<code>Ctrl+Space</code>) / +3 s / Stop; <code>←</code>/<code>→</code> seek one second. <b>Preview mode</b> locks editing and follows the current lyric in the middle of the table. The volume is shared with Preview.</li>
<li><b>Back · Next · Cancel</b></li>
</ol>
<div class='note'>While a text field has the cursor, <code>Space</code> and undo work as normal text editing.</div>""",
       ("lrc_review", "lrc_shortcuts", "lrc_input"),
       ("스페이스 기록 타임스탬프 시간 보정 입력 지연 미리보기 모드 가사 추가 삭제 실행 취소",
        "space record timestamp adjust time input latency preview mode add delete lyric undo")),

    _e("lyrics_editor", "lrc_review", ("4단계 · 확인 및 저장", "Step 4 · Review and save"),
       ("가사,LRC", "lyrics,LRC"),
       """[[img:lyrics_review]]
<ul>
<li>기록된 가사 수, 시간이 없는 가사 수, 마지막 타이밍과 함께 만들어질 LRC 내용을 보여 줍니다. 시간이 없는 줄은 결과에서 빠집니다(저장 전에 확인).</li>
<li><b>LRC 저장…</b>(<code>Ctrl+S</code>): 표준 UTF-8 LRC 파일을 만듭니다. <b>저장한 LRC를 프로젝트 콘텐츠에 추가</b>를 켜 두면 바로 쓸 수 있습니다.</li>
<li>곡 정보에서 열었다면 <b>완료</b>를 눌러 편집한 가사와 타이밍을 그 곡에 적용합니다(곡 정보 창의 <b>적용</b>으로 확정).</li>
</ul>""",
       """[[img:lyrics_review]]
<ul>
<li>Shows how many lyrics are timed, how many are not, the last timing, and the LRC that will be written. Lines without a time are left out (you are asked before saving).</li>
<li><b>Save LRC…</b> (<code>Ctrl+S</code>) writes a standard UTF-8 LRC file. Keep <b>Add the saved LRC to project content</b> on to use it right away.</li>
<li>Opened from Track information? <b>Done</b> hands the edited lyrics and timing to that track (confirm with <b>Apply</b> in the track window).</li>
</ul>""",
       ("lrc_timing", "track_lyrics"),
       ("LRC 저장 완료 적용 프로젝트 콘텐츠 추가 미기록", "save LRC done apply add to project content untimed")),

    _e("lyrics_editor", "lrc_recovery", ("자동 저장과 복구", "Autosave and recovery"),
       ("가사,복구", "lyrics,recovery"),
       """<ul>
<li>가사 입력과 타이밍 변경은 <b>오디오별 복구 초안</b>으로 자동 저장됩니다. 창 아래에 “자동 저장됨 (시각)” 상태가 표시됩니다.</li>
<li>프로그램이 비정상 종료된 뒤 같은 오디오로 다시 열면 저장 시각과 함께 <b>가사 자동 저장 복구</b> 여부를 묻습니다.</li>
<li>창을 정상적으로 닫거나 LRC를 저장하면 복구 초안은 정리됩니다. 닫을 때 확인 창이 나옵니다.</li>
<li>타이밍 단계에서 이전 단계로 돌아갈 때 바뀐 내용이 있으면 <b>변경 유지</b> 또는 <b>변경 무시</b>를 고릅니다.</li>
</ul>""",
       """<ul>
<li>Lyrics and timing changes are autosaved as a <b>recovery draft per audio file</b>; the bottom of the window shows “Autosaved (time)”.</li>
<li>After a crash, opening the same audio again offers to <b>recover the autosaved lyrics</b>, showing when they were saved.</li>
<li>Closing the window normally, or saving the LRC, clears the draft; you confirm before closing.</li>
<li>Going back from the timing step with changes asks whether to <b>keep</b> or <b>discard</b> them.</li>
</ul>""",
       ("lrc_generator",),
       ("자동 저장 복구 초안 비정상 종료 변경 유지 무시", "autosave recovery draft crash keep discard changes")),

    _e("lyrics_editor", "lrc_shortcuts", ("가사 편집기 단축키", "Lyrics editor shortcuts"),
       ("가사,단축키", "lyrics,shortcuts"),
       """<table cellspacing='4'>
<tr><th>키</th><th>동작</th></tr>
<tr><td><code>Space</code></td><td>현재 가사 줄에 재생 시간 기록</td></tr>
<tr><td><code>Ctrl+Z</code></td><td>마지막 타이밍 기록 취소</td></tr>
<tr><td><code>Ctrl+Y</code> / <code>Ctrl+Shift+Z</code></td><td>취소한 타이밍 다시 실행</td></tr>
<tr><td><code>Ctrl+S</code></td><td>현재 기록을 LRC 파일로 저장</td></tr>
<tr><td><code>Ctrl+Space</code></td><td>오디오 재생 / 일시정지</td></tr>
<tr><td><code>←</code> / <code>→</code></td><td>재생 위치를 1초 뒤로 / 앞으로</td></tr>
<tr><td><code>F2</code> · <code>Delete</code></td><td>선택한 가사 편집 · 삭제(확인 후)</td></tr>
<tr><td><code>F1</code> · <code>Shift+F1</code></td><td>이 도움말 · 단축키 안내 창</td></tr>
</table>
<p>창 아래 <b>단축키 안내</b> 버튼으로도 목록을 볼 수 있습니다.</p>""",
       """<table cellspacing='4'>
<tr><th>Key</th><th>Action</th></tr>
<tr><td><code>Space</code></td><td>Record the playback time for the current line</td></tr>
<tr><td><code>Ctrl+Z</code></td><td>Undo the last timing change</td></tr>
<tr><td><code>Ctrl+Y</code> / <code>Ctrl+Shift+Z</code></td><td>Redo</td></tr>
<tr><td><code>Ctrl+S</code></td><td>Save as an LRC file</td></tr>
<tr><td><code>Ctrl+Space</code></td><td>Play / pause</td></tr>
<tr><td><code>←</code> / <code>→</code></td><td>Seek one second back / forward</td></tr>
<tr><td><code>F2</code> · <code>Delete</code></td><td>Edit · delete the selected lyric (after confirming)</td></tr>
<tr><td><code>F1</code> · <code>Shift+F1</code></td><td>This help · the shortcut reference</td></tr>
</table>
<p>The <b>Shortcuts</b> button at the bottom of the window shows the list too.</p>""",
       ("lrc_timing", "shortcuts"),
       ("키보드 단축키", "keyboard shortcuts")),

    _e("lyrics_editor", "lyrics", ("가사를 영상에 표시하기", "Showing lyrics in the video"),
       ("가사,요소,타이밍", "lyrics,sources,timing"),
       """<p>곡에 가사를 연결하고 캔버스에 <b>가사 / 자막</b> 요소를 추가하면, 재생 위치에 맞춰 가사가 표시됩니다.</p>
<h2>표시 규칙</h2>
<p>현재 가사와 이전·다음 가사 묶음이 재생 시간에 맞춰 이동합니다. 첫 가사 전에는 <a href='topic:lyrics_intro'>인트로 임시 줄</a>을 표시할 수 있습니다. 중간의 긴 무보컬 구간에도 임시 줄을 넣을 수 있으며, 끝나면 가사 전환과 함께 사라집니다.</p>
<h2>가사 요소의 속성 설정</h2>
<ul>
<li><b>속성 → 애니메이션</b>: 글로우·라이즈·페이드·슬라이드·줌·순차 이동·바운스 등의 기본 전환과 <a href='topic:lyrics_transition'>자막 전환 설정</a>. 이동 방향·줄 배치·이전/현재/다음 스타일·인트로는 카테고리별 고급 모드로 조절합니다.</li>
<li><b>문맥 줄</b>: 이전·다음 묶음 수, 줄 간격, 이전 가사의 불투명도·블러. 거리별 흐림은 고급 줄 스타일에서 이전·다음 각각 설정합니다.</li>
<li><b>줄별 스타일</b>: 각 묶음 안의 2번째 텍스트 줄부터 원문·번역의 글꼴과 크기를 다르게 정합니다. <a href='topic:lyrics_line_styles'>사용 방법</a></li>
<li><b>음악에 맞춰 반응</b>: 현재 가사 묶음만 음악에 맞춰 확대하거나 움직입니다. <a href='topic:music_reaction'>반응 조절</a></li>
<li><b>대체 문구</b>: 가사가 없는 곡에 보여 줄 문구. 이전·현재·다음 가사에 지정한 블러와 불투명도는 적용하지 않고 선명하게 표시합니다.</li>
<li><b>타이밍 보정</b>: 모든 곡에 공통으로 적용되며, 곡별 보정과 더해집니다.</li>
</ul>
<h2>곡별 타이밍</h2>
<p>한 곡만 어긋나면 <a href='topic:track_lyrics'>곡 정보/설정 → 가사 설정</a>의 <b>곡별 타이밍 보정</b>을 씁니다. 양수는 가사를 더 빠르게, 음수는 더 늦게 표시합니다. 크게 틀리면 <a href='topic:lrc_generator'>가사 편집기</a>로 다시 기록하세요.</p>""",
       """<p>Attach lyrics to a track and add a <b>Lyrics / subtitles</b> source to the canvas: lyrics then follow the playback position.</p>
<h2>How lines appear</h2>
<p>Current, previous and upcoming cues move with playback time. A <a href='topic:lyrics_intro'>temporary intro row</a> can appear before the first lyric and during long vocal-free gaps, then disappear with the lyric transition.</p>
<h2>Lyrics source settings in the inspector</h2>
<ul>
<li><b>Inspector → Animation</b>: basic transitions such as Glow, Rise, Fade, Slide, Zoom, Staggered flow and Bounce, plus <a href='topic:lyrics_transition'>Subtitle transition settings</a>. Advanced mode is independent for motion, layout, role styles and intro.</li>
<li><b>Context lines</b>: previous/next cue counts, spacing and previous opacity/blur. Advanced styles add separate distance falloff for previous and next cues.</li>
<li><b>Per-line styles</b>: different fonts and sizes for original text and translations, starting with text line 2 in each cue. <a href='topic:lyrics_line_styles'>Instructions</a></li>
<li><b>React to music</b>: only the current cue scales or moves with the audio. <a href='topic:music_reaction'>Tuning the response</a></li>
<li><b>Fallback text</b>: what to show for tracks without lyrics. It stays sharp and does not inherit previous/current/next cue blur or opacity.</li>
<li><b>Timing offset</b>: applies to every track and adds to each track's own offset.</li>
</ul>
<h2>Per-track timing</h2>
<p>If just one track is off, use <b>Per-track timing offset</b> in <a href='topic:track_lyrics'>Track information/settings → Lyrics settings</a>: positive shows lyrics earlier, negative later. If it is badly off, re-record it in the <a href='topic:lrc_generator'>lyrics editor</a>.</p>""",
       ("lyrics_files", "track_lyrics", "lrc_generator"),
       ("가사 요소 자막 표시 하이라이트 강조 인트로 줄별 스타일 음악 반응 문맥 줄 보정",
        "lyrics source subtitles highlight intro per-line styles music reaction context lines offset")),

    _e("lyrics_editor", "lyrics_files", ("가사 파일 연결하기", "Attaching lyric files"),
       ("가사,플레이리스트", "lyrics,playlist"),
       """<ul>
<li>지원 형식: <b>LRC</b>, <b>SRT</b>, <b>WebVTT(.vtt)</b></li>
<li><b>자동 연결</b>: 음악을 추가할 때 같은 폴더에 이름이 같은 가사 파일이 있으면 함께 연결합니다. <b>도구 → 설정 → 콘텐츠 → 가사·자막 파일 자동 연결</b>에서 항상 함께 추가(권장) · 물어보기 · 추가하지 않음을 고릅니다. 이름이 비슷한 파일은 설정과 관계없이 물어봅니다.</li>
<li><b>끌어 놓기</b>: 가사 파일을 플레이리스트의 곡 위나 프로젝트 콘텐츠에서 곡 위로 끌어 놓습니다.</li>
<li><b>곡 정보/설정 → 가사 설정</b>: 파일 불러오기, 프로젝트 콘텐츠에서 고르기, 연결 해제, LRC로 내보내기</li>
</ul>
<div class='note'>여러 줄 가사는 LRC로 내보낼 때 문자 <code>\\n</code>으로 저장됩니다.</div>""",
       """<ul>
<li>Supported: <b>LRC</b>, <b>SRT</b> and <b>WebVTT (.vtt)</b></li>
<li><b>Automatic</b>: when you add music, a lyric file with the same name in the same folder is attached. Choose always (recommended), ask, or never in <b>Tools → Settings → Content → Attach lyric and subtitle files</b>. Similarly named files are always asked about.</li>
<li><b>Drag and drop</b>: drop a lyric file on a track in the playlist, or from Project Content onto a track.</li>
<li><b>Track information/settings → Lyrics settings</b>: load a file, pick one from project content, detach, or export as LRC</li>
</ul>
<div class='note'>Multi-line lyrics are written to LRC with a literal <code>\\n</code>.</div>""",
       ("lyrics", "track_lyrics", "playlist"),
       ("lrc srt vtt 자막 파일 자동 연결 사이드카 끌어 놓기", "lrc srt vtt subtitle file auto attach sidecar drag drop")),
)


# ---------------------------------------------------------------------------------------------
# AutoMix editor
# ---------------------------------------------------------------------------------------------

_AUTOMIX = (
    _e("automix_editor", "automix", ("AutoMix란?", "What AutoMix does"),
       ("AutoMix,곡 전환,분석", "AutoMix,transitions,analysis"),
       """<p><b>AutoMix(베타)</b>는 곡과 곡 사이를 템포에 맞춰 DJ처럼 자연스럽게 이어 주는 기능입니다. <b>프로젝트 → 프로젝트 설정 → 곡 전환</b>에서 <b>AutoMix</b>를 고르면 켜집니다(기본값은 꺼짐).</p>
<h2>어떻게 동작하나요</h2>
<ul>
<li>곡마다 박자·다운비트·보컬 구간(Beat This!, Open-Unmix), 키·에너지·곡 구조(인트로·아웃트로)를 분석합니다. 모델은 설치본에 들어 있어 인터넷 없이 PC에서 동작합니다.</li>
<li>두 곡의 템포·키·에너지·보컬 겹침을 비교해 전환 위치, 길이와 <a href='topic:automix_styles'>방식</a>을 고릅니다. 반주 인트로·아웃트로를 우선 쓰고, 양쪽이 모두 노래하는 곳에서는 짧게 넘깁니다.</li>
<li>템포가 맞지 않거나 분석에 실패한 곡은 기본 크로스페이드 또는 이어서 재생으로 연결됩니다.</li>
<li>미리보기와 내보내기는 같은 계획과 오디오 처리(EQ·필터 등)를 씁니다. 화면(가사·커버·곡 정보) 전환 시점과 영상 길이도 전환 위치를 따릅니다.</li>
</ul>
<h2>분석 결과와 캐시</h2>
<ul>
<li>BPM·키는 플레이리스트와 <a href='topic:track_analysis'>곡 정보/설정 → 분석</a>에서 확인합니다.</li>
<li>분석 결과는 PC의 캐시에 저장되어 다시 쓰이며, 180일 동안 쓰지 않은 항목은 자동으로 정리됩니다. <b>도구 → 설정 → 콘텐츠 → AutoMix 분석 캐시 → 비우기</b>로 지울 수 있습니다.</li>
<li>프로젝트 파일에는 AutoMix 사용 여부와 직접 편집한 전환만 저장됩니다.</li>
</ul>
<div class='note'>전환을 직접 다듬고 싶으면 <a href='topic:automix_editor'>AutoMix 편집기</a>를 여세요. 믹스와 미리듣기에는 FFmpeg가 필요합니다.</div>""",
       """<p><b>AutoMix (beta)</b> joins each song to the next in tempo, like a DJ. Turn it on by choosing <b>AutoMix</b> in <b>Project → Project settings → Transitions</b> (off by default).</p>
<h2>How it works</h2>
<ul>
<li>Each song is analyzed for beats, downbeats and vocal sections (Beat This!, Open-Unmix), and for key, energy and structure (intro/outro). The models ship with the app and run locally, offline.</li>
<li>Comparing tempo, key, energy and vocal overlap of two songs, it chooses where, how long and <a href='topic:automix_styles'>how</a> to mix. Instrumental intros and outros are preferred; where both songs are singing it keeps the mix short.</li>
<li>Songs whose tempos do not fit, or whose analysis failed, join with a plain crossfade or back to back.</li>
<li>Preview and Export use the same plan and audio processing (EQ, filters, …). The canvas switches (lyrics, cover, track info) and the video length follow the transitions too.</li>
</ul>
<h2>Analysis results and cache</h2>
<ul>
<li>See BPM and key in the playlist and in <a href='topic:track_analysis'>Track information/settings → Analysis</a>.</li>
<li>Results are cached on the PC and reused; entries unused for 180 days are cleaned up. Clear them in <b>Tools → Settings → Content → AutoMix analysis cache → Clear</b>.</li>
<li>A project file stores only whether AutoMix is on and the transitions you edited.</li>
</ul>
<div class='note'>To fine-tune a transition yourself, open the <a href='topic:automix_editor'>AutoMix editor</a>. Mixing and listening need FFmpeg.</div>""",
       ("automix_editor", "preview_mix", "project_settings", "track_analysis"),
       ("자동 믹스 템포 BPM 비트 다운비트 보컬 키 에너지 인트로 아웃트로 크로스페이드 캐시 베타",
        "automatic mix tempo BPM beat downbeat vocals key energy intro outro crossfade cache beta")),

    _e("automix_editor", "automix_editor", ("AutoMix 편집기 화면", "The AutoMix editor"),
       ("AutoMix,곡 전환", "AutoMix,transitions"),
       """<p>곡과 곡 사이의 <b>전환</b>을 두 곡 타임라인에서 직접 다듬는 창입니다. <span style='color:#F59E0B'>주황(A)</span>은 끝나는 곡, <span style='color:#38BDF8'>파랑(B)</span>은 이어지는 곡입니다. 분석이 정한 자동 전환에서 출발해 원하는 부분만 고치면 되고, 모든 변경은 바로 프로젝트에 저장되어 미리보기와 내보내기에 같은 설정으로 쓰입니다.</p>
<h2>여는 방법</h2>
<ul><li><b>도구 → AutoMix 편집기</b> 또는 플레이리스트의 <b>AutoMix 편집</b> 버튼(AutoMix 전환 모드에서)</li>
<li>플레이리스트에서 곡 사이의 <b>⟷ 자동 전환</b> / <b>✎ 수동</b> 표시를 누르면 그 전환이 바로 열립니다.</li></ul>
[[img:automix_editor]]
<ol>
<li><b>전환 선택</b> — ‹ › 버튼(<code>Alt+←</code>/<code>Alt+→</code>)이나 목록에서 편집할 전환을 고릅니다. ✎는 직접 설정한 전환입니다.</li>
<li><b>기본 / 고급 · 더 보기 · ?</b> — <b>기본</b>(<code>Ctrl+1</code>)은 스타일·길이와 핵심 큐만, <b>고급</b>(<code>Ctrl+2</code>)은 큐·템포·대역 레인과 효과 세부값까지 보여 줍니다. 같은 편집을 다르게 보여 줄 뿐이라 전환해도 값은 그대로입니다. <b>더 보기(⋯)</b>에는 복사·붙여넣기·프리셋·초기화·작업 공간 배치가 있습니다.</li>
<li><b>도구 막대</b> — 실행 취소·다시 실행, <b>스냅</b>(끄기·박자·마디), 축소·맞춤·확대, <b>대역 레인</b>, <b>속성</b> 패널 표시</li>
<li><b>타임라인</b> — 두 곡의 파형과 마디 번호, 아웃트로·인트로 끝·보컬 표시, 전환 구간(믹스 시작~끝). 핸들을 끌어 조정합니다: <a href='topic:automix_timeline'>타임라인 편집</a></li>
<li><b>속성 패널</b> — 스타일, 겹침 길이, 큐, 템포, 대역, 효과를 수치로 정합니다: <a href='topic:automix_properties'>속성과 고정</a></li>
<li><b>미리듣기</b> — 선택한 전환 구간만 렌더해 듣고, 자동 결과와 비교합니다: <a href='topic:automix_listen'>듣고 비교하기</a></li>
</ol>
<div class='note'>편집기를 처음 열면 짧은 안내가 나옵니다. 창은 최대화할 수 있고, 작업 공간 배치는 다음에도 유지됩니다.</div>""",
       """<p>This window fine-tunes the <b>transition</b> between two songs on a two-track timeline. <span style='color:#F59E0B'>Orange (A)</span> is the song ending, <span style='color:#38BDF8'>blue (B)</span> the one starting. Start from the automatic transition analysis chose and change only what you want; every change is saved to the project at once, and Preview and Export use the same settings.</p>
<h2>Opening it</h2>
<ul><li><b>Tools → AutoMix Editor</b>, or <b>AutoMix editor</b> in the playlist (in AutoMix transition mode)</li>
<li>Click a <b>⟷ Auto transition</b> / <b>✎ Manual</b> chip between two tracks in the playlist to open that transition.</li></ul>
[[img:automix_editor]]
<ol>
<li><b>Transition picker</b> — choose a transition with ‹ › (<code>Alt+←</code>/<code>Alt+→</code>) or the list. ✎ marks one set by hand.</li>
<li><b>Basic / Advanced · More · ?</b> — <b>Basic</b> (<code>Ctrl+1</code>) shows style, length and the key cues; <b>Advanced</b> (<code>Ctrl+2</code>) adds cue, tempo and band lanes and effect details. Both show the same edit, so switching never changes a value. <b>More (⋯)</b> holds copy, paste, presets, resets and workspace layout.</li>
<li><b>Tool bar</b> — undo/redo, <b>Snap</b> (off, beat, bar), zoom out, fit, zoom in, <b>band lanes</b>, and the <b>Properties</b> panel</li>
<li><b>Timeline</b> — both waveforms with bar numbers, outro/intro-end/vocal markers and the transition span (mix start to end). Drag its handles: <a href='topic:automix_timeline'>Editing on the timeline</a></li>
<li><b>Properties panel</b> — style, overlap length, cues, tempo, bands and effects as numbers: <a href='topic:automix_properties'>Properties and keeping values</a></li>
<li><b>Listening</b> — render just the selected transition, listen, and compare with automatic: <a href='topic:automix_listen'>Listen and compare</a></li>
</ol>
<div class='note'>A short guide appears the first time the editor opens. The window can be maximized, and its layout is remembered.</div>""",
       ("automix_styles", "automix_timeline", "automix_properties", "automix_listen"),
       ("편집기 열기 전환 선택 기본 고급 타임라인 속성 A B 나가는 곡 들어오는 곡",
        "open editor pick transition basic advanced timeline properties outgoing incoming")),

    _e("automix_editor", "automix_styles", ("전환 스타일", "Transition styles"),
       ("AutoMix,곡 전환", "AutoMix,transitions"),
       """<p>속성 패널의 <b>전환 스타일</b>에서 두 곡을 섞는 방식을 고릅니다. <b>자동 추천</b>은 두 곡을 분석해 이 구간에 어울리는 방식을 고릅니다.</p>
<h2>EQ 믹스 · 박자가 맞는 곡</h2>
<ul>
<li><b>베이스 스왑</b>: 저음을 겹침 가운데서 한 번에 교대해 두 곡의 베이스가 겹쳐 웅웅거리지 않게 합니다. 중·고음은 부드럽게 교차합니다.</li>
<li><b>보컬 보호</b>: 저음에 이어 보컬 대역(중음)도 교대해 두 목소리가 동시에 들리는 시간을 줄입니다.</li>
<li><b>필터 스윕</b>: 나가는 곡이 저음부터 빠지며 점점 얇아지고(하이패스), 다음 곡이 고음부터 그 자리를 채웁니다.</li>
<li><b>필터 블렌드</b>: 다음 곡이 고음부터 살짝 들리다가 점점 온전하게 들어옵니다(3대역 블렌드).</li>
<li><b>EQ 직접</b>: 저음·중음·고음을 각각 언제 넘길지 대역 레인에서 직접 정합니다.</li>
</ul>
<h2>페이드</h2>
<ul>
<li><b>크로스페이드</b>: 한 곡이 작아지는 동안 다음 곡이 커집니다. 가장 무난합니다.</li>
<li><b>드롭인</b>(여운 위로 시작): 다음 곡이 처음부터 제 음량으로 시작하고, 앞 곡의 여운은 그 아래로 사라집니다.</li>
</ul>
<h2>효과 · 템포가 달라도 됨</h2>
<ul>
<li><b>에코 아웃</b>: 앞 곡이 프레이즈 첫 박에서 멈추고, 마지막 박자의 메아리(저음 제거)만 박자에 맞춰 잦아드는 사이 다음 곡이 시작합니다.</li>
<li><b>테이프 스톱</b>: 턴테이블 전원이 꺼지듯 앞 곡이 느려지며 음이 내려가다 멈춥니다.</li>
<li><b>컷</b>: 겹치지 않고 큐 지점에서 바로 넘어갑니다. 큐를 박에 두면 박에서 박으로 깔끔하게 끊깁니다.</li>
</ul>
<div class='note'>분석이 부족하거나 섞을 길이가 모자란 곳은 <b>이어서 재생</b>(앞 곡의 마지막 소리 바로 뒤에 다음 곡)으로 연결됩니다. 전환 방식별 대역 곡선은 <a href='topic:transition_details'>전환 살펴보기</a>에서 그림으로 볼 수 있습니다.</div>""",
       """<p>Pick how the two songs mix under <b>Transition style</b> in the properties panel. <b>Auto pick</b> analyzes both songs and picks what suits this spot.</p>
<h2>EQ mixes · beat-matched songs</h2>
<ul>
<li><b>Bass swap</b>: swaps the lows once, mid-overlap, so two basslines never rumble together; mids and highs cross smoothly.</li>
<li><b>Vocal protection</b>: swaps the vocal band (mids) after the lows too, so two voices overlap as little as possible.</li>
<li><b>Filter sweep</b>: the outgoing song thins out from the bass up (high-pass) while the next fills in from the top.</li>
<li><b>Filter blend</b>: the next song is heard first in the highs, then comes in fully (a three-band blend).</li>
<li><b>Custom EQ</b>: set when the lows, mids and highs each hand over, in the band lanes.</li>
</ul>
<h2>Fades</h2>
<ul>
<li><b>Crossfade</b>: one song fades down while the next fades up. The safest choice.</li>
<li><b>Drop in</b> (start over the tail): the next song starts at full level and the previous tail fades away under it.</li>
</ul>
<h2>Effects · any tempo</h2>
<ul>
<li><b>Echo out</b>: the song stops on a phrase downbeat and only the echo of its last beat (bass removed) decays in time while the next song starts.</li>
<li><b>Tape stop</b>: the song slows down and drops in pitch until it stops, like a turntable losing power.</li>
<li><b>Cut</b>: no overlap; it jumps at the cue. With the cue on a beat, it cuts cleanly beat to beat.</li>
</ul>
<div class='note'>Where analysis is missing or there is too little room to mix, the songs play <b>back to back</b> (the next starts right after the last sound). <a href='topic:transition_details'>Transition details</a> draws each style's band curves.</div>""",
       ("automix_properties", "transition_details"),
       ("자동 추천 베이스 스왑 보컬 보호 필터 스윕 필터 블렌드 EQ 직접 크로스페이드 드롭인 에코 아웃 테이프 스톱 컷 이어서 재생",
        "recommended bass swap vocal protection filter sweep filter blend auto pick custom eq crossfade drop in echo out tape stop cut back to back")),

    _e("automix_editor", "automix_timeline", ("타임라인에서 끌어 조정하기", "Editing on the timeline"),
       ("AutoMix,타임라인", "AutoMix,timeline"),
       """<p>타임라인의 핸들과 막대를 끌어 전환을 옮깁니다. 마우스를 올리면 무엇을 바꾸는지 안내가 나옵니다.</p>
<ul>
<li><b>믹스 시작</b> 핸들: 위치와 길이를 함께 조정</li>
<li><b>믹스 끝</b> 핸들: 겹침 길이 조정</li>
<li><b>전환 구간</b> 가운데: 길이는 그대로 두고 옮기기</li>
<li><b>A 곡</b> 파형: 끌면 A의 큐(나가는 곡이 섞이기 시작하는 원본 시간)가 바뀌며 전환이 함께 움직입니다.</li>
<li><b>B 곡</b> 파형: 들어오는 곡의 시작 지점을 바꿉니다(인트로 건너뛰기).</li>
<li><b>템포 변경 시작</b> 핸들: A가 B의 템포로 바뀌기 시작하는 지점</li>
<li><b>대역 레인</b>(고급): 저음·중음·고음 막대(A 주황 실선, B 파랑 점선)를 끌어 교대 시점을 정합니다.</li>
</ul>
[[img:automix_editor_advanced]]
<h2>스냅과 보기</h2>
<ul>
<li><b>스냅</b>: 끄기·박자·마디. <code>S</code>로 켜고 끄며, <code>Shift</code>를 누른 채 끄는 동안은 반대로 동작합니다. 박자·마디는 분석을 믿을 수 있을 때만 고를 수 있습니다.</li>
<li><code>Ctrl</code>+휠 · <code>Ctrl+=</code>/<code>Ctrl+-</code>: 확대/축소, <b>맞춤</b>(<code>Ctrl+0</code>): 전환 전체 보기, 휠·가운데 버튼 끌기: 좌우 이동</li>
<li>끄는 중 <code>Esc</code>: 끌기 취소(아무것도 바뀌지 않음)</li>
</ul>
<h2>키보드로 정밀하게</h2>
<ul>
<li><code>↑</code>/<code>↓</code>: 편집 대상(핸들·곡·대역) 선택</li>
<li><code>←</code>/<code>→</code>: 선택한 대상을 한 박자 이동, <code>Shift</code>를 함께 누르면 0.01초</li>
</ul>
<div class='note'>고정한 값은 타임라인에서 움직이지 않습니다(속성 패널에서 고정을 풀면 됩니다). 요청한 위치가 곡 끝이나 앞 전환에 막히면 끄는 동안 “▸ … 에서 멈춤”처럼 이유가 표시되고, 실제 적용된 값은 속성 패널에 나옵니다.</div>""",
       """<p>Drag the timeline's handles and bars to move a transition; hover one to see what it changes.</p>
<ul>
<li><b>Mix start</b> handle: moves the start and changes the length</li>
<li><b>Mix end</b> handle: changes the overlap length</li>
<li><b>Transition</b> span: moves it, keeping its length</li>
<li><b>Track A</b> waveform: changes A's cue (where in the outgoing song the mix begins); the transition moves with it</li>
<li><b>Track B</b> waveform: changes where the incoming song starts (to skip an intro)</li>
<li><b>Tempo change</b> handle: where A starts easing onto B's tempo</li>
<li><b>Band lanes</b> (Advanced): drag the low/mid/high bars (A orange solid, B blue dashed) to set each handover</li>
</ul>
[[img:automix_editor_advanced]]
<h2>Snap and view</h2>
<ul>
<li><b>Snap</b>: off, beat or bar. <code>S</code> toggles it; holding <code>Shift</code> while dragging inverts it. Beat and bar are offered only when the analysis is reliable.</li>
<li><code>Ctrl</code>+wheel · <code>Ctrl+=</code>/<code>Ctrl+-</code>: zoom, <b>Fit</b> (<code>Ctrl+0</code>): the whole transition, wheel or middle-button drag: scroll</li>
<li><code>Esc</code> while dragging: cancel (nothing changes)</li>
</ul>
<h2>Precise keyboard editing</h2>
<ul>
<li><code>↑</code>/<code>↓</code>: choose what to edit (handle, track, band)</li>
<li><code>←</code>/<code>→</code>: move it one beat; with <code>Shift</code>, 0.01 s</li>
</ul>
<div class='note'>Kept values do not move on the timeline (unkeep them in the properties panel). When a request hits a song's end or the previous transition, the drag says why (“▸ stops at …”) and the properties panel shows the value actually applied.</div>""",
       ("automix_properties", "automix_shortcuts"),
       ("핸들 끌기 믹스 시작 믹스 끝 큐 템포 변경 대역 레인 스냅 박자 마디 확대 맞춤",
        "handle drag mix start mix end cue tempo change band lanes snap beat bar zoom fit")),

    _e("automix_editor", "automix_properties", ("속성 패널과 고정", "Properties and keeping values"),
       ("AutoMix,속성", "AutoMix,properties"),
       """[[img:automix_properties]]
<ul>
<li><b>상태</b>: <b>● 자동(분석)</b> — 분석이 정한 대로 섞입니다. 값을 하나라도 바꾸면 <b>직접 설정</b>이 되고, 자동과 다른 항목에는 ● 표시가 붙습니다.</li>
<li><b>겹침 길이</b>: 4초·8초·16초 버튼이나 초·마디 단위로 입력</li>
<li><b>전환 위치</b>(고급): <b>A 큐</b>(나가는 곡의 이 지점에서 섞이기 시작) · <b>B 시작</b>(들어오는 곡을 이 지점부터 재생). 초 단위 입력 후 <code>Enter</code></li>
<li><b>템포 맞춤</b>: A를 B의 템포로 맞추기(템포 차이가 커도), 템포가 바뀌는 구간(자동: 8마디, 차이가 크면 16마디)</li>
<li><b>키 맞춤</b>: 나가는 곡의 끝부분만 반음 단위로 옮겨 다음 곡의 키와 어울리게 합니다(자동/끄기/반음 지정).</li>
<li><b>보컬 교대 지점</b>, <b>대역 타이밍</b>: 대역마다 A가 사라지고 B가 올라오는 구간(겹침 대비 %). 모든 대역에 프리셋 적용, A·B 막대 함께 옮기기</li>
<li><b>효과 세부값</b>: 에코 간격(½·1·2박), 여운 감쇠, 에코 저음 걷어내기, 에코가 울리는 시간, 다음 곡이 들어오는 지점 등</li>
</ul>
<h2>고정과 되돌리기</h2>
<ul>
<li>항목 옆 <b>고정</b>(자물쇠)을 켜면 자동 추천을 다시 받아도 그 값은 바뀌지 않고, 타임라인에서 실수로 끌리지도 않습니다.</li>
<li><b>자동값으로</b>: 그 항목만 자동 결과로 되돌립니다.</li>
<li><b>자동 추천 다시 받기</b>(<code>Ctrl+Backspace</code>): 고정한 값은 두고 나머지를 분석이 다시 추천합니다. 고정한 값이 없으면 전환 전체가 <b>자동으로 되돌아갑니다</b>.</li>
<li><b>전환 전체 초기화</b>(더 보기 메뉴): 고정까지 풀고 자동 전환으로 되돌립니다.</li>
</ul>
<div class='note'>고정한 값을 지키는 추천이 없으면 값을 바꾸지 않고 저장된 값으로 재생하며, 가장 가까운 추천값을 안내합니다.</div>""",
       """[[img:automix_properties]]
<ul>
<li><b>State</b>: <b>● Automatic</b> — mixed as analysis decided. Changing anything makes it <b>set by hand</b>, and values that differ from automatic get a ● mark.</li>
<li><b>Overlap length</b>: 4 s, 8 s, 16 s buttons, or type seconds or bars</li>
<li><b>Where it sits</b> (Advanced): <b>A cue</b> (the mix starts here in the outgoing song) · <b>B start</b> (the incoming song plays from here). Type seconds and press <code>Enter</code></li>
<li><b>Tempo match</b>: bring A to B's tempo (even when far apart) and the span the tempo changes over (automatic: 8 bars, 16 when far apart)</li>
<li><b>Key match</b>: shifts only the end of the outgoing song by semitones to suit the next key (auto, off or a fixed shift)</li>
<li><b>Vocal handover</b> and <b>band timing</b>: per band, when A fades and B rises (percent of the overlap); apply a preset to all bands or move A and B bars together</li>
<li><b>Effect details</b>: echo spacing (½, 1, 2 beats), echo decay, echo low cut, echo length, where the next song comes in, and more</li>
</ul>
<h2>Keeping and resetting</h2>
<ul>
<li><b>Keep</b> (the lock) next to a value keeps it through new recommendations and stops it from being dragged by accident.</li>
<li><b>Reset</b> (next to a value): returns just that value to the automatic result.</li>
<li><b>Recommend again</b> (<code>Ctrl+Backspace</code>): keeps kept values and lets analysis recommend the rest. With nothing kept, the whole transition <b>goes back to automatic</b>.</li>
<li><b>Reset the whole transition</b> (More menu): unkeeps everything and returns to automatic.</li>
</ul>
<div class='note'>If no recommendation respects the kept values, the saved values keep playing unchanged and the nearest recommendation is shown.</div>""",
       ("automix_timeline", "automix_styles", "automix_reuse"),
       ("겹침 길이 A 큐 B 시작 템포 맞춤 키 맞춤 반음 보컬 교대 대역 타이밍 에코 고정 자물쇠 자동값 자동 추천",
        "overlap length A cue B start tempo match key match semitone vocal handover band timing echo keep lock automatic recommend")),

    _e("automix_editor", "automix_listen", ("듣고 비교하기", "Listen and compare"),
       ("AutoMix,재생", "AutoMix,playback"),
       """<p>아래 미리듣기 막대에서 <b>선택한 전환의 앞뒤 구간만</b> 렌더해 들어 봅니다. 전체 믹스를 다시 만들지 않으므로 빠르며, 최종 내보내기와 같은 계획·오디오 처리로 렌더됩니다.</p>
<ul>
<li><b>▶ 재생</b>(<code>Space</code>) / <b>■</b> 정지·구간 처음으로 / <b>반복</b>(<code>L</code>) / 위치 슬라이더 / 음량</li>
<li><b>A/B 비교</b>: <b>내 편집</b>과 <b>자동 결과</b>(같은 위치에서 분석이 정했을 전환)를 <code>B</code>로 번갈아 듣습니다. 편집하면 내 편집으로 돌아옵니다.</li>
<li>상태 표시: <b>● 준비 중</b>(이 구간만 렌더) → <b>● 최신 편집 반영됨</b>. 편집 직후에는 <b>● 갱신 필요 · 지금은 이전 편집이 들림</b>이 잠시 나오고, 준비되면 같은 위치에서 새 편집으로 바뀝니다. 실패하면 <b>다시 시도</b>를 누릅니다.</li>
</ul>
<div class='note'>미리듣기에는 FFmpeg가 필요합니다. 없으면 “미리듣기 불가 · FFmpeg 없음”이 표시됩니다.</div>""",
       """<p>The listening bar at the bottom renders <b>just the selected transition and a little around it</b>. It never rebuilds the whole mix, so it is quick, and it uses the same plan and audio processing as the final export.</p>
<ul>
<li><b>▶ Play</b> (<code>Space</code>) / <b>■</b> stop and back to the start / <b>Loop</b> (<code>L</code>) / position slider / volume</li>
<li><b>A/B</b>: switch between <b>My edit</b> and <b>Automatic</b> (what analysis would do at the same spot) with <code>B</code>. Any edit switches back to yours.</li>
<li>Status: <b>● Preparing</b> (rendering this window) → <b>● Up to date</b>. Right after an edit, <b>● Out of date · you hear the previous edit</b> shows briefly; the new edit takes over at the same position when ready. If it fails, press <b>Retry</b>.</li>
</ul>
<div class='note'>Listening needs FFmpeg; without it the bar says “No audition · FFmpeg not found”.</div>""",
       ("automix_editor", "ffmpeg"),
       ("미리듣기 A/B 비교 내 편집 자동 결과 반복 재생 렌더 FFmpeg", "audition listen A/B compare my edit automatic loop play render FFmpeg")),

    _e("automix_editor", "automix_reuse", ("복사·붙여넣기·프리셋", "Copy, paste and presets"),
       ("AutoMix,프리셋", "AutoMix,presets"),
       """<p>잘 만든 전환 설정을 다른 전환에 다시 씁니다. 모두 <b>더 보기(⋯)</b> 메뉴에 있습니다.</p>
<ul>
<li><b>전환 설정 복사</b>(<code>Ctrl+C</code>): 스타일·길이·템포·대역·효과를 복사합니다(큐 위치도 함께 보관).</li>
<li><b>붙여넣기 · 곡별 큐 제외</b>(<code>Ctrl+V</code>): 대상 전환의 큐 위치는 그대로 두고 붙여 넣습니다. 바뀐 항목이 상태에 표시됩니다.</li>
<li><b>큐 위치까지 붙여넣기</b>(<code>Ctrl+Shift+V</code>)</li>
<li><b>프리셋</b>: <b>현재 설정을 프리셋으로 저장…</b>하고, 목록에서 골라 적용하거나 삭제합니다. 프리셋은 스타일·길이·템포·대역·효과만 담습니다(큐와 고정값은 그대로).</li>
<li><b>자동 추천 다시 받기</b> · <b>전환 전체 초기화</b> · <b>작업 공간 기본 배치로</b></li>
</ul>
<p>모든 편집은 <code>Ctrl+Z</code> / <code>Ctrl+Shift+Z</code>로 되돌릴 수 있습니다(끌기 한 번이 한 단계).</p>""",
       """<p>Reuse a transition you like on others. Everything is in the <b>More (⋯)</b> menu.</p>
<ul>
<li><b>Copy transition settings</b> (<code>Ctrl+C</code>): style, length, tempo, bands and effects (cues are kept with it).</li>
<li><b>Paste · without cues</b> (<code>Ctrl+V</code>): keeps the target transition's cues; the status line lists what changed.</li>
<li><b>Paste including cues</b> (<code>Ctrl+Shift+V</code>)</li>
<li><b>Presets</b>: <b>Save these settings as a preset…</b>, then pick one to apply or delete. A preset holds style, length, tempo, bands and effects only (cues and kept values stay).</li>
<li><b>Recommend again</b> · <b>Reset the whole transition</b> · <b>Default workspace layout</b></li>
</ul>
<p>Every edit can be undone with <code>Ctrl+Z</code> / <code>Ctrl+Shift+Z</code> (one drag is one step).</p>""",
       ("automix_properties", "automix_shortcuts"),
       ("복사 붙여넣기 프리셋 저장 적용 삭제 초기화 작업 공간 실행 취소", "copy paste preset save apply delete reset workspace undo")),

    _e("automix_editor", "automix_playlist", ("플레이리스트의 전환 표시", "Transition chips in the playlist"),
       ("AutoMix,플레이리스트", "AutoMix,playlist"),
       """<p>AutoMix 프로젝트의 플레이리스트에는 곡과 곡 사이에 전환 표시가 나옵니다.</p>
<ul>
<li><b>⟷ 자동 전환</b>: 앞 곡에서 이 곡으로 넘어가는 전환을 AutoMix가 정합니다. 클릭하면 편집기에서 직접 설정할 수 있습니다.</li>
<li><b>⟷ 자동 · 고정값 유지</b>: AutoMix가 정하되 편집기에서 고정한 값은 그대로 둡니다. 우클릭하면 고정까지 풀고 자동으로 되돌립니다.</li>
<li><b>✎ 수동 · (길이) · (스타일)</b>: 직접 설정한 전환입니다. 클릭하면 편집하고, 우클릭 → <b>자동으로 되돌리기</b>.</li>
</ul>
<p>곡 순서를 바꾸면 새로 이웃한 곡 쌍에는 자동 전환이 적용됩니다.</p>""",
       """<p>In an AutoMix project the playlist shows a chip between every two tracks.</p>
<ul>
<li><b>⟷ Auto transition</b>: AutoMix decides how the previous track hands over to this one. Click to set it yourself in the editor.</li>
<li><b>⟷ Auto · values kept</b>: AutoMix decides, but values you kept in the editor stay. Right-click to unkeep them and go back to automatic.</li>
<li><b>✎ Manual · (length) · (style)</b>: a transition set by hand. Click to edit; right-click → <b>Back to automatic</b>.</li>
</ul>
<p>After reordering, newly adjacent pairs start out automatic.</p>""",
       ("playlist", "automix_editor"),
       ("자동 전환 수동 고정값 유지 우클릭 자동으로 되돌리기", "auto transition manual values kept right click back to automatic")),

    _e("automix_editor", "automix_shortcuts", ("AutoMix 편집기 단축키", "AutoMix editor shortcuts"),
       ("AutoMix,단축키", "AutoMix,shortcuts"),
       """<table cellspacing='4'>
<tr><th>키</th><th>동작</th></tr>
<tr><td><code>Space</code> · <code>L</code> · <code>Home</code></td><td>재생/일시정지 · 구간 반복 · 구간 처음으로</td></tr>
<tr><td><code>B</code></td><td>A/B 비교: 내 편집 ↔ 자동 결과</td></tr>
<tr><td><code>Ctrl+Z</code> · <code>Ctrl+Shift+Z</code></td><td>실행 취소 · 다시 실행 (끌기 한 번 = 한 단계)</td></tr>
<tr><td><code>Alt+←</code> / <code>Alt+→</code></td><td>이전 / 다음 전환</td></tr>
<tr><td><code>Ctrl+1</code> / <code>Ctrl+2</code></td><td>기본 / 고급</td></tr>
<tr><td><code>S</code> · <code>Shift</code>+끌기</td><td>스냅 켜기/끄기 · 누르는 동안 반대로</td></tr>
<tr><td><code>Esc</code></td><td>끌기 취소</td></tr>
<tr><td><code>↑</code> / <code>↓</code></td><td>타임라인에서 편집 대상 선택</td></tr>
<tr><td><code>←</code> / <code>→</code> (<code>Shift</code>)</td><td>선택한 대상 한 박자 (0.01초) 이동</td></tr>
<tr><td><code>Ctrl</code>+휠 · <code>Ctrl+=</code> / <code>-</code> · <code>Ctrl+0</code></td><td>확대 · 축소 · 맞춤</td></tr>
<tr><td>휠 · 가운데 버튼 끌기</td><td>타임라인 좌우 이동</td></tr>
<tr><td><code>Ctrl+C</code> / <code>Ctrl+V</code> · <code>Ctrl+Shift+V</code></td><td>설정 복사 / 붙여넣기(큐 제외) · 큐까지 붙여넣기</td></tr>
<tr><td><code>Ctrl+Backspace</code></td><td>자동으로 되돌리기 · 고정값이 있으면 다시 추천</td></tr>
<tr><td><code>F1</code> · <code>Shift+F1</code></td><td>이 도움말 · 단축키 요약 창</td></tr>
</table>
<p>숫자 입력칸에 커서가 있으면 입력이 먼저입니다.</p>""",
       """<table cellspacing='4'>
<tr><th>Key</th><th>Action</th></tr>
<tr><td><code>Space</code> · <code>L</code> · <code>Home</code></td><td>Play/pause · loop the window · window start</td></tr>
<tr><td><code>B</code></td><td>A/B: my edit ↔ automatic</td></tr>
<tr><td><code>Ctrl+Z</code> · <code>Ctrl+Shift+Z</code></td><td>Undo · redo (one drag = one step)</td></tr>
<tr><td><code>Alt+←</code> / <code>Alt+→</code></td><td>Previous / next transition</td></tr>
<tr><td><code>Ctrl+1</code> / <code>Ctrl+2</code></td><td>Basic / advanced</td></tr>
<tr><td><code>S</code> · <code>Shift</code>+drag</td><td>Snap on/off · inverted while held</td></tr>
<tr><td><code>Esc</code></td><td>Cancel a drag</td></tr>
<tr><td><code>↑</code> / <code>↓</code></td><td>Select what to edit on the timeline</td></tr>
<tr><td><code>←</code> / <code>→</code> (<code>Shift</code>)</td><td>Nudge the selection a beat (0.01 s)</td></tr>
<tr><td><code>Ctrl</code>+wheel · <code>Ctrl+=</code> / <code>-</code> · <code>Ctrl+0</code></td><td>Zoom in · out · fit</td></tr>
<tr><td>Wheel · middle-button drag</td><td>Scroll the timeline</td></tr>
<tr><td><code>Ctrl+C</code> / <code>Ctrl+V</code> · <code>Ctrl+Shift+V</code></td><td>Copy / paste settings (no cues) · paste including cues</td></tr>
<tr><td><code>Ctrl+Backspace</code></td><td>Back to automatic · recommend again if values are kept</td></tr>
<tr><td><code>F1</code> · <code>Shift+F1</code></td><td>This help · the shortcut summary</td></tr>
</table>
<p>While a number field has the cursor, typing goes to it.</p>""",
       ("automix_timeline", "shortcuts"),
       ("키보드 단축키", "keyboard shortcuts")),
)


# ---------------------------------------------------------------------------------------------
# Track information/settings
# ---------------------------------------------------------------------------------------------

_TRACK = (
    _e("track", "track_details", ("곡 정보/설정 창", "The Track information/settings window"),
       ("곡 정보,플레이리스트", "track info,playlist"),
       """<p>플레이리스트에서 곡을 고르고 <b>곡 정보/설정</b>을 누르거나, 곡을 더블클릭하거나, <code>Enter</code>를 누르면 열립니다. 이 창의 설정은 <b>이 프로젝트의 이 곡</b>에만 적용되고, 원본 음원 파일은 바뀌지 않습니다.</p>
[[img:track_info]]
<ol>
<li><b>머리글</b> — 커버, 제목, 아티스트·앨범과 길이·형식·샘플레이트·가사 줄 수·BPM·키(캠럿) 같은 요약. 음원 파일이 없으면 ⚠ 표시가 나옵니다.</li>
<li><b>탭</b> — <a href='topic:track_info'>곡 정보</a>, <a href='topic:track_analysis'>분석</a>, <a href='topic:track_lyrics'>가사 설정</a>, <a href='topic:track_videos'>이 곡의 영상</a>, <a href='topic:track_audio'>오디오</a></li>
<li><b>탭 내용</b></li>
<li><b>적용 · 취소</b> — 적용을 눌러야 바뀐 내용이 프로젝트에 반영됩니다(<code>Ctrl+Z</code>로 되돌리기 가능).</li>
</ol>
<div class='note'>이 창에서 <b>F1</b>을 누르면 지금 보고 있는 탭의 도움말이 열립니다.</div>""",
       """<p>Select a track in the playlist and choose <b>Track info/settings</b>, double-click it, or press <code>Enter</code>. Settings here apply to <b>this track in this project</b> only; the audio file itself never changes.</p>
[[img:track_info]]
<ol>
<li><b>Header</b> — cover, title, artist and album plus a summary: length, format, sample rate, lyric lines, BPM and key (Camelot). A ⚠ appears if the audio file is missing.</li>
<li><b>Tabs</b> — <a href='topic:track_info'>Track information</a>, <a href='topic:track_analysis'>Analysis</a>, <a href='topic:track_lyrics'>Lyrics settings</a>, <a href='topic:track_videos'>Videos for this track</a>, <a href='topic:track_audio'>Audio</a></li>
<li><b>Tab content</b></li>
<li><b>Apply · Cancel</b> — changes reach the project only when you apply them (and <code>Ctrl+Z</code> undoes them).</li>
</ol>
<div class='note'>Press <b>F1</b> here to open the help for the tab you are on.</div>""",
       ("track_info", "track_analysis", "track_lyrics", "track_audio"),
       ("곡 상세 더블클릭 엔터 머리글 적용 취소", "track details double-click enter header apply cancel")),

    _e("track", "track_info", ("곡 정보 탭", "Track information tab"),
       ("곡 정보,메타데이터", "track info,metadata"),
       """<ul>
<li><b>기본 정보</b>: 제목·아티스트·앨범. 텍스트 요소의 <code>%title%</code> 같은 <a href='topic:text_tokens'>토큰</a>과 트랙 목록·현재 재생 카드에 쓰입니다. 원본 파일의 태그는 바꾸지 않습니다.</li>
<li><b>파일</b>: 위치, 재생 시간, 형식(모노/스테레오), 음질(비트레이트·샘플레이트·비트 깊이), 크기. <b>폴더에서 보기</b>는 탐색기에서 음원 파일을 선택해 보여 줍니다.</li>
<li><b>앨범 커버</b>: <b>이미지 변경…</b>으로 프로젝트에서 쓸 곡별 커버를 고르고, <b>내장 커버 사용</b>으로 음원에 들어 있는 커버로 돌아갑니다. 앨범 커버·배경·퍼스널 컬러가 이 커버를 씁니다.</li>
</ul><p>원본 MP3의 태그와 커버를 수정하려면 도구 메뉴의 <a href='topic:mp3_metadata'>MP3 메타데이터 편집기</a>를 사용하세요.</p>""",
       """<ul>
<li><b>Basic information</b>: title, artist and album, used by <a href='topic:text_tokens'>tokens</a> such as <code>%title%</code>, the track list and the now playing card. The file's tags are not changed.</li>
<li><b>File</b>: location, length, format (mono/stereo), quality (bitrate, sample rate, bit depth) and size. <b>Show in folder</b> selects the audio file in Explorer.</li>
<li><b>Album cover</b>: <b>Change image…</b> picks a per-track cover for this project; <b>Use embedded artwork</b> goes back to the one inside the audio file. Album cover sources, backgrounds and personal color use it.</li>
</ul><p>To edit tags and artwork in the original MP3, use <a href='topic:mp3_metadata'>MP3 Metadata Editor</a> in the Tools menu.</p>""",
       ("track_details", "text_tokens"),
       ("제목 아티스트 앨범 태그 커버 이미지 변경 내장 커버 폴더에서 보기 음질 비트레이트",
        "title artist album tags cover change image embedded cover show in folder quality bitrate")),

    _e("track", "mp3_metadata", ("MP3 메타데이터 편집기", "MP3 Metadata Editor"),
       ("메타데이터,MP3,음악", "metadata,MP3,music"),
       """<p><b>도구 → MP3 메타데이터 편집기</b>에서 MP3 파일에 저장된 ID3 태그와 앨범 커버를 편집합니다.</p>
[[img:mp3_metadata]]
<ol>
<li><b>파일 열기…</b>로 파일을 선택합니다. 파일을 열기 전에는 입력 칸과 저장 버튼이 비활성화됩니다.</li>
<li>제목·아티스트·앨범·앨범 아티스트·연도·장르·트랙 번호·디스크 번호·BPM·설명을 수정합니다.</li>
<li>커버를 선택하거나 제거하고 미리보기로 확인합니다.</li>
<li><b>저장</b>을 누르면 선택한 원본 MP3 파일에 태그와 커버를 씁니다. 창을 닫는 것만으로는 저장되지 않습니다.</li>
</ol>
<div class='note'><a href='topic:track_info'>곡 정보/설정</a>은 프로젝트 안에서 쓸 제목·아티스트·앨범·커버를 바꾸는 기능입니다. MP3 편집기는 원본 파일을 변경하며, 이미 추가한 곡의 프로젝트 정보는 곡 정보/설정에서 따로 확인하세요.</div>""",
       """<p>Open <b>Tools → MP3 Metadata Editor</b> to edit the ID3 tags and artwork stored in an MP3 file.</p>
[[img:mp3_metadata]]
<ol>
<li>Use <b>Open file…</b> to select a file. Input fields and Save remain disabled until a file is opened.</li>
<li>Edit title, artist, album, album artist, year, genre, track/disc number, BPM and comment.</li>
<li>Select or remove artwork and check its preview.</li>
<li><b>Save</b> writes tags and artwork into the selected original file. Closing the window does not save.</li>
</ol>
<div class='note'><a href='topic:track_info'>Track information/settings</a> changes project-specific titles, artists, albums and covers. This editor changes the original MP3; check project information separately for tracks already added.</div>""",
       ("track_info", "text_tokens"),
       ("ID3 태그 원본 파일 커버 앨범 아티스트 장르 연도 트랙 번호 디스크 BPM 코멘트 저장",
        "ID3 tags original file artwork album artist genre year track disc BPM comment save")),

    _e("track", "track_analysis", ("분석 탭", "Analysis tab"),
       ("곡 정보,분석,AutoMix", "track info,analysis,AutoMix"),
       """[[img:track_analysis]]
<p><b>이 곡 분석</b>을 누르면 AutoMix를 쓰지 않아도 이 곡만 바로 분석합니다. 오디오 읽기 → 템포·비트 찾기 → 마디 나누기 → 키·에너지·무음 구간 → 비트 모델(Beat This!) → 보컬 감지 → 곡 구조 순서로 진행 상황을 보여 주며, 분석 중 창을 닫아도 계속되고 결과는 플레이리스트에 반영됩니다.</p>
<ul>
<li><b>템포</b>(BPM과 신뢰도), <b>키</b>(캠럿 표기), <b>박자</b>(마디 신뢰도), <b>에너지</b>, <b>보컬</b>(첫·마지막 보컬 시각)</li>
<li><b>믹스 준비도</b>: 좋음(마디에 맞춰 섞을 수 있음) · 템포만 확인됨 · 기본 크로스페이드로 연결</li>
<li><b>에너지 곡선</b>: 인트로·아웃트로, 여운 시작(점선), 무음 구간, 위쪽 눈금은 마디</li>
<li>세부 값: 비트/마디 수, 시작 무음, 소리 끝, 여운 시작, 보컬 측정 구간, 인트로 끝·아웃트로 시작, 구간, 사용한 분석기</li>
</ul>
<p>결과는 저장되어 AutoMix 미리보기와 내보내기에서도 다시 쓰입니다. 파일이 그대로면 <b>다시 분석</b>은 저장된 결과를 바로 불러오고, 바뀌었으면 새로 분석합니다.</p>
<div class='note'>분석에는 FFmpeg가 필요합니다.</div>""",
       """[[img:track_analysis]]
<p><b>Analyze this track</b> analyzes just this track, with or without AutoMix. It shows each step — reading audio → tempo and beats → bars → key, energy and silence → beat model (Beat This!) → vocals → song structure — keeps running if you close the window, and updates the playlist when done.</p>
<ul>
<li><b>Tempo</b> (BPM and confidence), <b>key</b> (with Camelot), <b>meter</b> (bar confidence), <b>energy</b>, <b>vocals</b> (first and last vocal)</li>
<li><b>Mix readiness</b>: good (can mix on bars) · tempo only · joins with a plain crossfade</li>
<li><b>Energy curve</b>: intro/outro, tail start (dashed), silence; ticks on top are bars</li>
<li>Details: beats and bars, leading silence, sound end, tail start, vocal range, intro end and outro start, sections, analyzer used</li>
</ul>
<p>Results are saved and reused by AutoMix Preview and Export. If the file is unchanged, <b>Analyze again</b> loads the saved result at once; if it changed, it analyzes anew.</p>
<div class='note'>Analysis needs FFmpeg.</div>""",
       ("automix", "track_details"),
       ("분석 BPM 템포 키 캠럿 박자 마디 에너지 보컬 믹스 준비도 에너지 곡선 인트로 아웃트로 다시 분석",
        "analysis BPM tempo key camelot meter bars energy vocals mix readiness energy curve intro outro analyze again")),

    _e("track", "track_lyrics", ("가사 설정 탭", "Lyrics settings tab"),
       ("곡 정보,가사,타이밍", "track info,lyrics,timing"),
       """[[img:track_lyrics]]
<h2>가사 / 자막</h2>
<ul>
<li><b>파일 불러오기…</b>: LRC·SRT·VTT 파일을 이 곡에 연결</li>
<li><b>프로젝트 콘텐츠…</b>: 프로젝트 콘텐츠에 있는 가사 파일 중에서 고르기</li>
<li><b>LRC 생성기로 편집…</b>: 이 곡의 오디오와 가사·타이밍을 <a href='topic:lrc_generator'>가사 편집기</a>에서 고치기</li>
<li><b>LRC로 내보내기…</b>: 곡별 보정을 적용한 현재 가사를 LRC 파일로 저장</li>
<li><b>연결 해제</b>: 이 곡에서 가사를 뗍니다.</li>
</ul>
<h2>곡별 타이밍 보정</h2>
<p><b>−0.10</b> / <b>+0.10</b> 버튼이나 직접 입력으로 이 곡의 가사를 앞당기거나 늦춥니다. 양수는 가사를 더 빠르게, 음수는 더 늦게 표시하며, 가사 요소의 공통 보정값과 더해집니다. 아래 목록에 보정이 적용된 타임코드가 미리 보입니다.</p>
<h2>노래와 가사 미리보기</h2>
<p>재생·정지와 위치 슬라이더로 실제 노래를 들으며 이전·현재·다음 가사가 맞게 나오는지 확인합니다. 볼륨은 미리보기와 공유됩니다.</p>""",
       """[[img:track_lyrics]]
<h2>Lyrics / subtitles</h2>
<ul>
<li><b>Load file…</b>: attach an LRC, SRT or VTT file to this track</li>
<li><b>From project content…</b>: pick one of the lyric files in project content</li>
<li><b>Edit in LRC Generator…</b>: fix this track's lyrics and timing in the <a href='topic:lrc_generator'>lyrics editor</a> with its audio</li>
<li><b>Export as LRC…</b>: save the current lyrics, with this track's offset applied, as an LRC file</li>
<li><b>Detach</b>: remove the lyrics from this track</li>
</ul>
<h2>Per-track timing offset</h2>
<p>Use <b>−0.10</b> / <b>+0.10</b> or type a value to move this track's lyrics. Positive shows them earlier, negative later, and it adds to the lyrics source's common offset. The list below previews the adjusted timecodes.</p>
<h2>Song and lyrics preview</h2>
<p>Play, stop and seek the real song and watch the previous, current and next lyric line up. The volume is shared with Preview.</p>""",
       ("lyrics", "lyrics_files", "lrc_generator"),
       ("가사 파일 불러오기 연결 해제 LRC 내보내기 곡별 타이밍 보정 오프셋 빠르게 늦게 미리보기",
        "load lyrics detach export LRC per-track timing offset earlier later preview")),

    _e("track", "track_videos", ("이 곡의 영상 탭", "Videos for this track tab"),
       ("곡 정보,영상", "track info,video"),
       """[[img:track_videos]]
<p>캔버스의 <a href='topic:video_source'>영상 요소</a>에서 <b>곡마다 다른 영상 사용</b>을 선택하면, 이 곡이 재생되는 동안 여기 등록한 영상 목록을 씁니다. 영상 요소의 크기·반복·속도·효과 설정은 그대로 적용됩니다.</p>
<ul><li><b>영상 추가…</b>, <b>선택 제거</b>, <b>위로</b> / <b>아래로</b>(재생 순서)</li></ul>""",
       """[[img:track_videos]]
<p>When a canvas <a href='topic:video_source'>video source</a> is set to <b>Use different videos for each track</b>, it plays this list while this track is on. The video source's size, repeat, speed and effect settings still apply.</p>
<ul><li><b>Add videos…</b>, <b>Remove selected</b>, <b>Up</b> / <b>Down</b> (play order)</li></ul>""",
       ("video_source",),
       ("곡별 영상 추가 제거 순서", "per-track video add remove order")),

    _e("track", "track_audio", ("오디오 탭 · 곡 볼륨과 EQ", "Audio tab · track volume and EQ"),
       ("곡 정보,오디오", "track info,audio"),
       """[[img:track_audio]]
<ul>
<li><b>곡 볼륨</b>(dB): 다른 곡과 비교한 이 곡의 상대적인 크기입니다. 전체 믹스는 여전히 −14 LUFS로 맞춰집니다.</li>
<li><b>이퀄라이저</b>(60 Hz · 250 Hz · 1 kHz · 4 kHz · 12 kHz, dB): 대역별로 소리를 키우거나 줄입니다. <b>초기화</b>로 평탄하게 되돌립니다.</li>
<li><b>미리듣기</b>: 재생·정지와 위치 슬라이더. 값을 바꾸면 잠시 뒤 재생 중인 소리에 반영됩니다.</li>
<li><b>원본과 비교(볼륨/EQ 끄기)</b>: 켜면 원래 소리를 들려줍니다.</li>
</ul>
<p>이 곡에만 적용되며 미리보기와 내보내기에 반영됩니다.</p>
<div class='note'>EQ 미리듣기에는 FFmpeg가 필요합니다. 없으면 원본으로 재생합니다.</div>""",
       """[[img:track_audio]]
<ul>
<li><b>Track volume</b> (dB): how loud this track is relative to the others. The whole mix is still normalized to −14 LUFS.</li>
<li><b>Equalizer</b> (60 Hz · 250 Hz · 1 kHz · 4 kHz · 12 kHz, dB): boost or cut each band. <b>Reset</b> flattens it.</li>
<li><b>Listen</b>: play, stop and seek. Changes reach the playing sound after a moment.</li>
<li><b>Compare with original (bypass)</b>: hear the untouched sound.</li>
</ul>
<p>It affects only this track, in both Preview and Export.</p>
<div class='note'>The EQ preview needs FFmpeg; without it the original plays.</div>""",
       ("track_details", "preview_mix"),
       ("볼륨 음량 dB EQ 이퀄라이저 저음 고음 LUFS 원본과 비교 미리듣기",
        "volume loudness dB EQ equalizer bass treble LUFS compare original listen")),
)


# ---------------------------------------------------------------------------------------------
# Other: projects, export, settings, maintenance
# ---------------------------------------------------------------------------------------------

_OTHER = (
    _e("other", "projects", ("프로젝트 저장·열기·복구", "Saving, opening and recovering projects"),
       ("프로젝트,저장,복구", "project,saving,recovery"),
       """<h2>프로젝트 파일</h2>
<ul>
<li><code>.pvsproj</code>(권장): 프로젝트와 포함된 미디어를 하나로 묶은 파일입니다. 설치 버전에서는 탐색기에서 더블클릭하면 바로 열립니다.</li>
<li><code>.project.json</code>(레거시): 열 수 있지만 <b>프로젝트 → 레거시 프로젝트 업그레이드</b>로 패키지로 바꾸는 것을 권장합니다.</li>
<li><b>파일 → 최근 프로젝트</b>에서 최근 파일을 엽니다. 목록 지우기는 파일을 삭제하지 않습니다.</li>
</ul>
<h2>저장</h2>
<ul>
<li><code>Ctrl+S</code> 저장, <code>Ctrl+Shift+S</code> 다른 이름으로 저장. 저장 상태는 오른쪽 위에 “저장 필요”처럼 표시됩니다.</li>
<li>저장하지 않은 채 닫거나 다른 프로젝트를 열면 <b>저장 · 저장 안 함 · 취소</b>를 묻습니다.</li>
</ul>
<h2>자동 저장과 복구</h2>
<p>작업 중 복구 사본이 주기적으로 기록됩니다(상태 표시줄에 “자동 저장됨”). 프로그램이 비정상 종료되면 다음 시작 때 복구 여부를 묻습니다. 문제가 된 프로젝트는 오류 보고와 함께 안전하게 여는 방법을 안내합니다.</p>
<h2>누락 미디어</h2>
<p>원본 위치 참조로 저장한 프로젝트에서 파일을 옮기면, 열 때 <b>누락 미디어</b> 창에서 새 위치를 지정해 다시 연결합니다. 다른 PC로 옮길 때는 콘텐츠를 <b>프로젝트에 포함</b>하세요.</p>""",
       """<h2>Project files</h2>
<ul>
<li><code>.pvsproj</code> (recommended): the project and its included media in one file. With the installed app, double-click it in Explorer to open it.</li>
<li><code>.project.json</code> (legacy): opens, but <b>Project → Upgrade legacy project</b> turns it into a package.</li>
<li><b>File → Recent projects</b> reopens recent files; clearing the list never deletes files.</li>
</ul>
<h2>Saving</h2>
<ul>
<li><code>Ctrl+S</code> saves, <code>Ctrl+Shift+S</code> saves as. The top right shows whether it needs saving.</li>
<li>Closing or opening another project with unsaved work asks <b>Save · Don't save · Cancel</b>.</li>
</ul>
<h2>Autosave and recovery</h2>
<p>A recovery copy is written regularly while you work (“Autosaved” in the status bar). After a crash, the next start offers to recover it. A project that caused a problem comes with an error report and a safe way to open it.</p>
<h2>Missing media</h2>
<p>If files of a project that references originals have moved, the <b>Missing media</b> window on opening lets you point to their new location. For a project that moves to another PC, <b>include content in the project</b>.</p>""",
       ("project_settings", "project_content", "troubleshooting"),
       ("저장 열기 pvsproj json 레거시 업그레이드 최근 프로젝트 자동 저장 복구 비정상 종료 누락 미디어",
        "save open pvsproj json legacy upgrade recent projects autosave recovery crash missing media")),

    _e("other", "project_settings", ("프로젝트 설정", "Project settings"),
       ("프로젝트,곡 전환,캔버스", "project,transitions,canvas"),
       """<p><b>프로젝트 → 프로젝트 설정</b>에서 현재 프로젝트의 화면, 곡 전환, 저장 방식을 바꿉니다.</p>
[[img:project_settings]]
<ul>
<li><b>프로젝트 정보</b>: 이름, 작성자, 설명, <b>프로젝트 썸네일</b>(현재 캔버스 자동 사용 또는 사용자 이미지)</li>
<li><b>캔버스</b>: 화면 비율과 너비·높이. <b>요소 위치와 크기도 함께 조정(권장)</b> 또는 <b>캔버스만 변경</b>. 바꾸기 전에 확인하며 <code>Ctrl+Z</code>로 되돌릴 수 있습니다.</li>
<li><b>곡 전환</b>
<ul><li><b>없음</b>(즉시 전환, 기본값): 앞 곡이 끝나면 다음 곡을 겹침 없이 재생</li>
<li><b>크로스페이드</b>: 지정한 초(0.5~30초)만큼 앞 곡의 끝과 다음 곡의 시작을 겹쳐 재생. 곡 분석은 필요 없습니다.</li>
<li><b><a href='topic:automix'>AutoMix</a></b>(베타): 템포를 분석해 전환 위치와 방식을 자동으로 정합니다.</li></ul>
전환을 쓰면 곡이 겹치는 만큼 전체 길이가 줄어듭니다.</li>
<li><b>파일 관리</b>: <b>추가한 콘텐츠를 프로젝트에 포함</b>(이미지·음원·폰트·가사를 패키지에 복사, 다른 PC에서도 안전) 또는 <b>외부 콘텐츠를 프로젝트에서 참조</b>(파일은 작지만 원본을 옮기면 다시 연결 필요)</li>
</ul>
<div class='note'>내보내기 해상도는 출력 품질 설정일 뿐 프로젝트의 화면 비율을 바꾸지 않습니다. 세로형·사용자 지정 비율도 검은 여백 없이 내보내집니다.</div>""",
       """<p><b>Project → Project settings</b> changes the current project's screen, transitions and storage.</p>
[[img:project_settings]]
<ul>
<li><b>Project information</b>: name, author, description and the <b>project thumbnail</b> (the current canvas automatically, or your own image)</li>
<li><b>Canvas</b>: aspect ratio, width and height. <b>Also adjust source positions and sizes (recommended)</b> or <b>change only the canvas</b>. You confirm first, and <code>Ctrl+Z</code> undoes it.</li>
<li><b>Transitions</b>
<ul><li><b>None</b> (cut, default): the next track starts when the previous ends, no overlap</li>
<li><b>Crossfade</b>: overlaps the end of one track and the start of the next by the seconds you set (0.5–30 s); no analysis needed</li>
<li><b><a href='topic:automix'>AutoMix</a></b> (beta): analyzes tempo and chooses where and how to mix</li></ul>
Transitions shorten the video by the overlaps.</li>
<li><b>File management</b>: <b>include added content in the project</b> (images, audio, fonts and lyrics are copied into the package; safe on any PC) or <b>reference external content</b> (smaller, but moved originals must be reconnected)</li>
</ul>
<div class='note'>Export resolution is an output quality setting and never changes the project's aspect ratio; portrait and custom ratios export without black bars.</div>""",
       ("automix", "projects", "start"),
       ("화면 비율 캔버스 크기 곡 전환 없음 크로스페이드 AutoMix 포함 참조 썸네일 작성자 설명",
        "aspect ratio canvas size transitions none crossfade AutoMix include reference thumbnail author description")),

    _e("other", "export", ("영상·오디오·자막 내보내기", "Exporting video, audio, and subtitles"),
       ("내보내기,자막,LRC,SRT", "export,subtitles,LRC,SRT"),
       """<p>오른쪽 위 <b>내보내기</b>(<code>Ctrl+E</code>)를 누르면 내보내기 설정 창이 열립니다. 영상·오디오와 믹싱 시간 확인에는 <a href='topic:ffmpeg'>FFmpeg</a>가 필요합니다. 믹싱 없는 자막은 FFmpeg 없이 저장할 수 있습니다.</p>
[[img:export_settings]]
<ul>
<li><b>출력 파일</b>: 내보내기 항목·파일 형식·파일 이름·저장 폴더를 따로 선택합니다. 같은 파일이 있으면 덮어쓸지 묻습니다.</li>
<li><b>영상 + 오디오</b>: MP4·MOV·MKV. <b>오디오만 내보내기</b>: MP3·M4A·WAV·FLAC·OGG.</li>
<li><b>자막 내보내기</b>: LRC·SRT. 등록한 가사와 시간 보정을 실제 믹싱 타임라인에 맞춥니다. 겹치는 가사는 <b>나가는 곡을 윗줄, 들어오는 곡을 아랫줄</b>에 표시하고 각 줄 앞에 <code>- </code>를 붙입니다. 겹침이 끝나면 한 곡만 표시합니다.</li>
<li><b>용도</b>: <b>권장 · 대부분의 영상</b>(균형, 사용 가능한 NVIDIA GPU는 자동 사용) · <b>빠른 내보내기</b> · <b>고화질 보관용</b> · <b>작은 파일</b>. 고급 값을 바꾸면 <b>사용자 설정</b>이 됩니다.</li>
<li><b>해상도</b>(HD·Full HD·QHD·4K, 프로젝트 비율 유지)와 <b>프레임 레이트</b>. 예상 작업량이 함께 표시됩니다.</li>
<li><b>고급 인코딩 설정</b>: 비디오 인코더(자동 선택 권장, CPU H.264/H.265, 지원되는 GPU 인코더), 화질 CRF(낮을수록 고화질), 인코딩 속도(preset), 오디오 품질(AAC). <b>권장 설정으로 되돌리기</b></li>
<li><b>이 값을 다음 내보내기의 기본값으로 저장</b></li>
</ul>
<p>내보내기에 포함된 곡만 들어가며(플레이리스트에서 제외한 곡은 빠짐), 곡 전환·곡별 볼륨/EQ·가사·애니메이션이 미리보기와 같게 적용됩니다.</p>
<div class='note'>4K나 60 FPS는 화면 준비와 인코딩 시간을 크게 늘립니다. 잘 모르겠으면 <b>권장</b>과 Full HD 30 FPS로 시작하세요.</div>""",
       """<p><b>Export</b> at the top right (<code>Ctrl+E</code>) opens the export settings. Video/audio and mix timing need <a href='topic:ffmpeg'>FFmpeg</a>. Unmixed subtitles can be saved without FFmpeg.</p>
[[img:export_settings]]
<ul>
<li><b>Output file</b>: choose the export type, format, file name, and folder separately. You are asked before overwriting.</li>
<li><b>Video + audio</b>: MP4, MOV, MKV. <b>Audio only</b>: MP3, M4A, WAV, FLAC, OGG.</li>
<li><b>Subtitles</b>: LRC and SRT. Attached lyrics and timing offsets follow the actual mix timeline. Overlapping lyrics show the <b>outgoing track above the incoming track</b>, each with a <code>- </code> prefix. Only one track remains after the overlap.</li>
<li><b>Purpose</b>: <b>Recommended · Most videos</b> (balanced; uses an available NVIDIA GPU) · <b>Fast export</b> · <b>High-quality archive</b> · <b>Small file</b>. Changing an advanced value makes it <b>Custom</b>.</li>
<li><b>Resolution</b> (HD, Full HD, QHD, 4K — the project ratio is kept) and <b>frame rate</b>, with the estimated workload</li>
<li><b>Advanced encoding</b>: video encoder (automatic recommended, CPU H.264/H.265, supported GPU encoders), quality CRF (lower is better), encoding speed (preset), audio quality (AAC). <b>Back to recommended</b></li>
<li><b>Save these values as the default for the next export</b></li>
</ul>
<p>Only tracks included in export go in (tracks excluded in the playlist are left out); transitions, per-track volume/EQ, lyrics and animations are applied just like in Preview.</p>
<div class='note'>4K or 60 FPS greatly lengthen screen preparation and encoding. When unsure, start with <b>Recommended</b> at Full HD 30 FPS.</div>""",
       ("export_process", "ffmpeg", "performance"),
       ("내보내기 MP4 해상도 FPS 프레임 레이트 인코더 CRF 화질 preset AAC 용도 권장 빠른 고화질 작은 파일 저장공간 H.264 H.265 NVENC",
        "export MP4 resolution FPS frame rate encoder CRF quality preset AAC purpose recommended fast archive small storage H.264 H.265 NVENC")),

    _e("other", "export_process", ("내보내기 진행과 완료", "Export progress and completion"),
       ("내보내기,진행", "export,progress"),
       """<h2>진행 단계</h2>
<ol>
<li><b>화면 준비</b>: 애니메이션·가사·곡 정보가 바뀌는 순간의 캔버스를 캡처합니다(변하지 않는 레이어는 한 번만).</li>
<li><b>오디오 준비</b>: 곡의 음량을 맞추고 전환(크로스페이드·AutoMix)을 적용해 하나로 합칩니다.</li>
<li><b>효과 준비</b>: 비주얼라이저·파형·레벨 미터·파티클을 음악에 맞춰 렌더링합니다.</li>
<li><b>영상 만들기</b>: FFmpeg가 화면과 오디오를 최종 MP4로 인코딩합니다.</li>
</ol>
<p>진행 창은 단계별 진행률, 남은 시간, 병렬 작업 수와 <b>저장 공간 사용량</b>(화면·오디오·효과·결과 영상의 실제 크기와 출력 드라이브 여유 공간)을 보여 줍니다.</p>
<ul>
<li>내보내기 중에는 프로젝트가 바뀌지 않도록 편집 화면이 잠깁니다. <b>최소화</b>해도 작업은 계속되며, 작업 표시줄에서 창을 복원하면 진행 창도 다시 나옵니다.</li>
<li><b>취소</b>하고 확인하면 실행 중인 작업과 임시 파일을 정리한 뒤 편집 화면으로 돌아갑니다.</li>
<li>화면은 기본적으로 실시간 파이프로 FFmpeg에 바로 전달되어 큰 중간 파일을 만들지 않습니다. 실패하면 무손실 중간 영상 방식으로 자동 재시도합니다.</li>
<li><b>도구 → 설정 → 알림</b>에서 단계별 Windows 알림(화면 준비·오디오 준비·효과 준비·영상 만들기·완료·오류 및 취소)과 표시 조건을 정합니다.</li>
</ul>
<h2>완료 창</h2>
<p>파일 크기와 출력 확인 결과를 보여 주고 <b>영상 재생하기</b>, <b>저장 폴더 열기</b>, <b>경로 복사</b>, <b>다시 내보내기</b>를 제공합니다.</p>""",
       """<h2>Stages</h2>
<ol>
<li><b>Screen</b>: captures the canvas at every moment animations, lyrics or track info change (unchanging layers only once).</li>
<li><b>Audio</b>: levels the tracks and applies transitions (crossfade, AutoMix) into one mix.</li>
<li><b>Effects</b>: renders visualizers, waveforms, level meters and particles to the music.</li>
<li><b>Video</b>: FFmpeg encodes the picture and sound into the final MP4.</li>
</ol>
<p>The progress window shows each stage's progress, the time left, parallel jobs and <b>storage use</b> (actual sizes of screen, audio, effects and the result, and free space on the output drive).</p>
<ul>
<li>The editor is locked during export so the project cannot change. <b>Minimize</b> keeps it running; restoring the app from the taskbar brings the progress window back.</li>
<li><b>Cancel</b> and confirm to stop the work, clean up temporary files and return to the editor.</li>
<li>Frames go straight to FFmpeg through a live pipe, so no large intermediate files are written. If that fails, it retries automatically with a lossless intermediate video.</li>
<li><b>Tools → Settings → Notifications</b> sets Windows notifications per stage (screen, audio, effects, video, done, errors and cancel) and when they show.</li>
</ul>
<h2>Completion</h2>
<p>Shows the file size and the output check, with <b>Play video</b>, <b>Open folder</b>, <b>Copy path</b> and <b>Export again</b>.</p>""",
       ("export", "performance", "settings"),
       ("진행 단계 화면 준비 오디오 준비 효과 준비 인코딩 취소 최소화 저장 공간 알림 완료 다시 내보내기",
        "progress stages screen audio effects encoding cancel minimize storage notifications done export again")),

    _e("other", "performance", ("성능과 내보내기 시간 줄이기", "Performance and faster exports"),
       ("성능,내보내기,미리보기", "performance,export,preview"),
       """<h2>미리보기가 느릴 때</h2>
<ul><li>동시에 보이는 비주얼라이저·파형·파티클 수와 파티클 밀도, 비주얼라이저 막대 수를 줄입니다.</li>
<li>큰 영상 요소가 많으면 프록시 준비가 끝날 때까지 기다립니다.</li>
<li>그래픽 드라이버 문제가 있을 때만 <a href='topic:preview_quality'>CPU 호환 모드</a>를 씁니다.</li></ul>
<h2>내보내기가 오래 걸릴 때</h2>
<ul><li>필요하지 않다면 4K·60 FPS 대신 Full HD 30 FPS를 씁니다.</li>
<li>가사 애니메이션이나 매우 짧은 변화가 많으면 준비할 화면이 늘어납니다.</li>
<li>GPU 인코더는 최종 인코딩을 줄이지만 화면 준비 시간까지 줄이지는 않습니다.</li>
<li>진행 창의 단계명과 백분율로 시간이 어디(화면·효과·인코딩)에서 쓰이는지 먼저 확인하세요.</li></ul>
<h2>작업 모드</h2>
<p><b>도구 → 설정 → 내보내기 → 작업 모드</b></p>
<ul><li><b>자동(권장)</b>: PC 성능·해상도·작업 수에 맞춰 병렬 처리량을 조절</li>
<li><b>안정</b>: 동시 작업을 1개로 제한. 느리지만 저사양 PC, 4K, 메모리가 부족할 때 가장 안전</li>
<li><b>최대 속도</b>: 더 많은 CPU 작업을 사용(CPU·메모리 사용 증가)</li></ul>
<div class='note'>변하지 않는 이미지·도형·텍스트 레이어는 한 번만 준비하고, 같은 화면이 이어지는 구간은 다시 캡처하지 않으므로 정적인 디자인일수록 빠릅니다.</div>""",
       """<h2>If Preview is slow</h2>
<ul><li>Show fewer visualizers, waveforms and particles at once; lower particle density and visualizer bar count.</li>
<li>With many large videos, wait for their proxies to finish.</li>
<li>Use <a href='topic:preview_quality'>CPU compatibility mode</a> only for graphics driver problems.</li></ul>
<h2>If Export takes long</h2>
<ul><li>Prefer Full HD 30 FPS over 4K or 60 FPS unless you need them.</li>
<li>Lots of lyric animation or very short changes mean more screens to prepare.</li>
<li>GPU encoders shorten the final encoding, not screen preparation.</li>
<li>Check the stage and percentage in the progress window to see where the time goes (screen, effects, encoding).</li></ul>
<h2>Work mode</h2>
<p><b>Tools → Settings → Export → Work mode</b></p>
<ul><li><b>Automatic (recommended)</b>: scales parallel work to the PC, resolution and number of jobs</li>
<li><b>Stable</b>: one job at a time. Slower, but safest on low-end PCs, in 4K or when memory is short</li>
<li><b>Maximum speed</b>: more CPU jobs (more CPU and memory use)</li></ul>
<div class='note'>Unchanging image, shape and text layers are prepared once, and stretches where the screen does not change are not captured again, so static designs export faster.</div>""",
       ("export_process", "preview_quality", "audio_visuals"),
       ("성능 최적화 느림 빠르게 작업 모드 안정 자동 최대 속도 병렬 메모리 4K 60fps",
        "performance optimize slow faster work mode stable automatic maximum speed parallel memory 4K 60fps")),

    _e("other", "ffmpeg", ("FFmpeg 설치와 확인", "Installing and checking FFmpeg"),
       ("FFmpeg,설정,내보내기", "FFmpeg,settings,export"),
       """<p>FFmpeg는 영상과 음성을 읽고 합쳐 최종 동영상 파일로 만드는 오픈 소스 엔진입니다. 편집은 FFmpeg 없이도 되지만 <b>내보내기, 믹스 미리보기, AutoMix 분석·미리듣기, EQ 미리듣기</b>에는 필요합니다. 프로그램에 기본 포함되지 않습니다.</p>
<h2>자동 설치(권장)</h2>
<ol><li><b>도구 → 설정 → FFmpeg</b>를 엽니다.</li>
<li>설치 버전 목록에서 <b>(권장)</b> 버전을 고르고 <b>선택 버전 다운로드 및 설치</b>를 누릅니다.</li>
<li>진행 창에서 다운로드, SHA-256 검증, 압축 해제와 실행 확인이 끝나면 바로 적용됩니다. 설치 중에는 다른 작업을 할 수 없습니다.</li></ol>
<p>Windows 64비트용 BtbN FFmpeg GPL 배포본이 <code>%LOCALAPPDATA%\\PlaylistCanvas\\tools\\ffmpeg</code>에 설치됩니다. <b>업데이트</b>는 권장 버전으로 교체, <b>재설치</b>는 같은 버전을 다시 검증해 교체, <b>삭제</b>는 앱이 설치한 FFmpeg만 지웁니다.</p>
<h2>직접 지정</h2>
<p>이미 설치된 <code>ffmpeg.exe</code>가 있으면 <b>FFmpeg 경로</b>에서 선택하고 <b>확인</b>을 누릅니다.</p>
<h2>비디오 인코더 자동 선택</h2>
<p><b>자동 선택(권장)</b>은 NVIDIA GPU가 있으면 H.264 NVENC를 실제 출력 설정으로 먼저 검사하고, 없으면 CPU H.264를 씁니다. NVENC 검사에 실패하면 긴 준비를 시작하기 전에 원인과 함께 CPU로 계속할지 묻습니다.</p>""",
       """<p>FFmpeg is an open-source engine that reads, combines and compresses audio and video into the final file. You can edit without it, but <b>Export, mixed Preview, AutoMix analysis and listening, and the EQ preview</b> need it. It is not bundled.</p>
<h2>Automatic install (recommended)</h2>
<ol><li>Open <b>Tools → Settings → FFmpeg</b>.</li>
<li>Pick the <b>(recommended)</b> version and choose <b>Download and install the selected version</b>.</li>
<li>The progress window downloads, verifies SHA-256, unpacks and test-runs it, then it applies at once. Other work is blocked meanwhile.</li></ol>
<p>The BtbN FFmpeg GPL build for 64-bit Windows is installed to <code>%LOCALAPPDATA%\\PlaylistCanvas\\tools\\ffmpeg</code>. <b>Update</b> switches to the recommended version, <b>Reinstall</b> downloads and verifies the same version again, and <b>Delete</b> removes only the FFmpeg the app installed.</p>
<h2>Using your own</h2>
<p>If you already have <code>ffmpeg.exe</code>, choose it under <b>FFmpeg path</b> and press <b>Check</b>.</p>
<h2>Automatic video encoder</h2>
<p><b>Automatic (recommended)</b> first tests H.264 NVENC with the real output settings when an NVIDIA GPU is present, and uses CPU H.264 otherwise. If NVENC fails the test, you are asked — before the long preparation starts — whether to continue on the CPU.</p>""",
       ("export", "settings", "troubleshooting"),
       ("FFmpeg 다운로드 설치 경로 SHA-256 업데이트 재설치 삭제 인코더 NVENC GPU CPU",
        "FFmpeg download install path SHA-256 update reinstall delete encoder NVENC GPU CPU")),

    _e("other", "settings", ("설정", "Settings"),
       ("설정", "settings"),
       """<p><b>도구 → 설정</b>에서 프로그램 전체의 기본값을 관리합니다.</p>
[[img:settings_general]]
<ul>
<li><b>일반</b>: 언어, <a href='topic:language_packs'>외부 언어팩</a>, 부드러운 스크롤(끄기 또는 80~420ms, <code>Ctrl</code>+휠 확대에는 영향 없음), <a href='topic:preview_quality'>미리보기 렌더러</a>(GPU 레이어 / CPU 호환 모드, 재시작 후 적용). 화면은 다크 스튜디오 테마로 고정입니다.</li>
<li><b>내보내기</b>: 기본 출력 폴더(비우면 기본 비디오 폴더), 기본 해상도·FPS·비디오 인코더·CRF·인코딩 preset·오디오 품질, <a href='topic:performance'>작업 모드</a></li>
<li><b>FFmpeg</b>: 경로, 설치 버전, 상태 확인과 <a href='topic:ffmpeg'>관리 설치</a></li>
<li><b>콘텐츠</b>: <a href='topic:lyrics_files'>가사·자막 파일 자동 연결</a>, AutoMix 분석 캐시 크기와 <b>비우기</b></li>
<li><b>알림</b>: 내보내기 진행 알림 사용, 표시 조건(포커스가 없을 때만 / 항상), 알림 단계</li>
<li><b>유지 관리</b>: <a href='topic:maintenance'>무결성 검사, 강제 재설치, 프로그램 초기화</a></li>
</ul>
<p><b>저장</b>을 눌러야 적용됩니다.</p>""",
       """<p><b>Tools → Settings</b> holds the app-wide defaults.</p>
[[img:settings_general]]
<ul>
<li><b>General</b>: language, <a href='topic:language_packs'>external language packs</a>, smooth scrolling (off or 80–420 ms; <code>Ctrl</code>+wheel zoom is unaffected), <a href='topic:preview_quality'>preview renderer</a> (GPU layers / CPU compatibility, applies after a restart). The look is always the dark studio theme.</li>
<li><b>Export</b>: default output folder (empty = your Videos folder), default resolution, FPS, video encoder, CRF, encoding preset, audio quality, and <a href='topic:performance'>work mode</a></li>
<li><b>FFmpeg</b>: path, installed version, status check and <a href='topic:ffmpeg'>managed install</a></li>
<li><b>Content</b>: <a href='topic:lyrics_files'>attaching lyric and subtitle files</a>, AutoMix analysis cache size and <b>Clear</b></li>
<li><b>Notifications</b>: export progress notifications, when to show them (only when the app is not focused / always) and which stages</li>
<li><b>Maintenance</b>: <a href='topic:maintenance'>integrity check, forced reinstall and program reset</a></li>
</ul>
<p>Press <b>Save</b> to apply.</p>""",
       ("maintenance", "ffmpeg", "language_packs"),
       ("설정 일반 언어 스크롤 렌더러 내보내기 기본값 출력 폴더 콘텐츠 캐시 알림 유지 관리",
        "settings general language scrolling renderer export defaults output folder content cache notifications maintenance")),

    _e("other", "maintenance", ("유지 관리 · 검사, 재설치, 초기화", "Maintenance · check, reinstall, reset"),
       ("설정,문제 해결", "settings,troubleshooting"),
       """[[img:settings_maintenance]]
<ul>
<li><b>무결성 검사</b>: 설치된 프로그램 파일이 빠지거나 손상되지 않았는지 공식 배포본의 SHA-256 목록과 비교합니다. 문제가 있으면 강제 재설치로 복구할지 묻습니다(설치된 프로그램에서만 사용 가능).</li>
<li><b>강제 재설치</b>: GitHub에서 최신 공식 Setup을 내려받아 버전과 관계없이 다시 설치합니다. 프로젝트와 설정은 유지됩니다.</li>
<li><b>프로그램 초기화</b>: 모든 설정, 최근 프로젝트 목록, 창 배치, AutoMix 편집기 프리셋, 분석·미리보기 캐시, 내려받은 업데이트 파일을 지우고 처음 설치한 상태로 다시 시작합니다. <b>프로젝트 파일, 사용자 프리셋, 언어팩, 설치된 FFmpeg는 지우지 않습니다.</b> 처음 실행 안내도 다시 나옵니다.</li>
</ul>""",
       """[[img:settings_maintenance]]
<ul>
<li><b>Integrity check</b>: compares the installed program files with the official build's SHA-256 list to find missing or damaged files, and offers a forced reinstall if needed (installed app only).</li>
<li><b>Forced reinstall</b>: downloads the latest official Setup from GitHub and reinstalls whatever the version. Projects and settings are kept.</li>
<li><b>Reset program</b>: clears all settings, the recent projects list, window layouts, AutoMix editor presets, analysis and preview caches, and downloaded updates, then restarts as freshly installed. <b>Project files, user presets, language packs and the installed FFmpeg are kept.</b> The first-run guides show again.</li>
</ul>""",
       ("settings", "troubleshooting", "updates"),
       ("무결성 검사 SHA-256 강제 재설치 프로그램 초기화 캐시 삭제 안내 다시 보기",
        "integrity check SHA-256 forced reinstall reset program clear cache show guides again")),

    _e("other", "language_packs", ("외부 언어팩", "External language packs"),
       ("설정,언어", "settings,language"),
       """<p>한국어·영어 외의 번역은 <b>도구 → 설정 → 일반 → 외부 언어팩</b>에서 UTF-8 JSON 파일로 가져옵니다. 프로그램을 다시 빌드할 필요가 없습니다.</p>
<ul><li><b>가져오기</b>: 검증한 언어팩을 사용자 폴더에 설치 · <b>제거</b> · <b>폴더 열기</b> · <b>새로고침</b></li>
<li>번역이 빠진 문장은 영어로 표시됩니다.</li>
<li>잘못된 JSON, 호환되지 않는 형식, 자리표시자 오류, 4MB를 넘는 파일은 불러오지 않습니다. 실행 코드는 들어갈 수 없습니다.</li>
<li>Windows에서는 <code>%LOCALAPPDATA%\\PlaylistCanvas\\languages\\</code>에 번역 값이 비어 있는 <code>language-pack-template.json</code>이 만들어집니다. 복사해 로캘 이름으로 바꾸고 값을 채우면 됩니다.</li></ul>""",
       """<p>Translations beyond Korean and English are imported as UTF-8 JSON in <b>Tools → Settings → General → External language packs</b>, with no rebuild needed.</p>
<ul><li><b>Import</b> installs a validated pack to your user folder · <b>Remove</b> · <b>Open folder</b> · <b>Reload</b></li>
<li>Untranslated text falls back to English.</li>
<li>Broken JSON, incompatible schemas, placeholder mistakes and files over 4 MB are rejected; packs cannot contain code.</li>
<li>On Windows, <code>%LOCALAPPDATA%\\PlaylistCanvas\\languages\\</code> gets a <code>language-pack-template.json</code> with empty values: copy it, rename it to your locale and fill it in.</li></ul>""",
       ("settings",),
       ("언어 번역 언어팩 json 가져오기 제거 새로고침 템플릿 로캘", "language translation pack json import remove reload template locale")),

    _e("other", "updates", ("프로그램 업데이트", "Updating Playlist Canvas"),
       ("업데이트", "updates"),
       """<p>시작 후 공식 GitHub 저장소의 최신 안정 릴리즈를 백그라운드에서 확인합니다. 새 버전이 있으면 릴리즈 노트와 현재·최신 버전을 보여 줍니다.</p>
<ol><li><b>다운로드 및 업데이트</b>를 고릅니다.</li>
<li>Setup 다운로드와 파일 크기·SHA-256 검증이 끝날 때까지 기다립니다.</li>
<li>작업 내용을 복구본으로 자동 보관하고 프로그램을 종료합니다. 추가 확인이나 설치 버튼 없이 설치를 마친 뒤 다시 실행해 같은 작업을 엽니다. 진행 중인 내보내기·저장은 먼저 완료될 때까지 기다립니다.</li></ol>
<p>관리자 승인 없이 업데이트할 수 있도록 사용자 폴더에 설치합니다. 원본 프로젝트는 덮어쓰지 않으며 저장되지 않은 변경도 그대로 복구합니다.</p>
<p>시작 알림을 닫으면 그 버전은 다시 자동으로 알리지 않습니다. <b>도움말 → 업데이트 확인</b>으로 언제든 수동으로 확인·설치할 수 있습니다. 초안·사전 릴리즈는 대상이 아닙니다.</p>""",
       """<p>After startup the app checks the official GitHub repository for the latest stable release in the background. A newer one shows its release notes with the current and latest versions.</p>
<ol><li>Choose <b>Download and update</b>.</li>
<li>Wait for the Setup download and its size and SHA-256 check.</li>
<li>The workspace is backed up automatically and the app closes. Installation and restart require no further clicks, and the same workspace reopens. Active exports and saves finish first.</li></ol>
<p>Setup uses the current user's folder without requesting administrator approval. The original project file is kept, including its unsaved changes in the recovered workspace.</p>
<p>Dismissing the startup notice stops automatic reminders for that version. <b>Help → Check for updates</b> checks and installs any time. Drafts and pre-releases are ignored.</p>""",
       ("maintenance",),
       ("업데이트 최신 버전 github 릴리즈 setup 건너뛰기 확인", "update latest version github release setup skip check")),

    _e("other", "shortcuts", ("창별 단축키 모음", "Shortcuts in every window"),
       ("단축키", "shortcuts"),
       """<p>창마다 단축키가 다릅니다. 각 창에서 <code>F1</code>을 누르면 그 창의 도움말이 열립니다.</p>
<ul>
<li><a href='topic:canvas_shortcuts'>메인 창(캔버스·플레이리스트)</a> — <b>도움말 → 단축키 안내</b>에도 표로 있습니다.</li>
<li><a href='topic:preview_controls'>미리보기</a> — <code>Space</code>, <code>←</code>/<code>→</code>, <code>Shift+←</code>/<code>→</code>, <code>↑</code>/<code>↓</code></li>
<li><a href='topic:lrc_shortcuts'>가사 편집기</a> — <code>Space</code> 기록, <code>Ctrl+Space</code> 재생, <code>Shift+F1</code> 단축키 창</li>
<li><a href='topic:automix_shortcuts'>AutoMix 편집기</a> — <code>Space</code>, <code>L</code>, <code>B</code>, <code>S</code>, <code>Shift+F1</code> 단축키 창</li>
</ul>
<p>텍스트나 숫자 입력칸에 커서가 있으면 일반 입력이 우선합니다.</p>""",
       """<p>Each window has its own keys, and <code>F1</code> in any window opens its help.</p>
<ul>
<li><a href='topic:canvas_shortcuts'>Main window (canvas, playlist)</a> — also as a table in <b>Help → Keyboard shortcuts</b></li>
<li><a href='topic:preview_controls'>Preview</a> — <code>Space</code>, <code>←</code>/<code>→</code>, <code>Shift+←</code>/<code>→</code>, <code>↑</code>/<code>↓</code></li>
<li><a href='topic:lrc_shortcuts'>Lyrics editor</a> — <code>Space</code> records, <code>Ctrl+Space</code> plays, <code>Shift+F1</code> shortcut window</li>
<li><a href='topic:automix_shortcuts'>AutoMix editor</a> — <code>Space</code>, <code>L</code>, <code>B</code>, <code>S</code>, <code>Shift+F1</code> shortcut window</li>
</ul>
<p>While a text or number field has the cursor, typing wins.</p>""",
       ("canvas_shortcuts", "preview_controls", "lrc_shortcuts", "automix_shortcuts"),
       ("키보드 단축키 f1 도움말 핫키", "keyboard shortcuts f1 help hotkeys")),

    _e("other", "troubleshooting", ("문제 해결과 지원 정보", "Troubleshooting and support"),
       ("문제 해결", "troubleshooting"),
       """<h2>프로그램이 실행되지 않을 때</h2>
<p>설치 폴더에서 EXE만 따로 옮기지 마세요. <code>_internal</code> 폴더와 함께 있어야 합니다. 파일이 손상된 것 같으면 <a href='topic:maintenance'>무결성 검사</a>를 실행합니다.</p>
<h2>미디어를 찾을 수 없을 때</h2>
<p>프로젝트를 열 때 나오는 누락 미디어 창에서 새 위치를 지정합니다. 다른 PC로 옮길 프로젝트는 콘텐츠를 포함한 <code>.pvsproj</code>로 저장하세요.</p>
<h2>내보내기·믹스가 실패할 때</h2>
<ul><li><b>설정 → FFmpeg</b>의 상태를 확인하고 필요하면 권장 버전을 다시 설치합니다.</li>
<li>GPU 인코더가 실패하면 CPU H.264(<code>libx264</code>)로 다시 시도합니다.</li>
<li>디스크 공간이 부족하면 출력 위치를 바꾸거나 공간을 비웁니다. 내보내기 진행 창에서 저장 공간 사용량을 확인할 수 있습니다.</li></ul>
<h2>미리보기 화면이 이상할 때</h2>
<p>GPU 미리보기가 불안정하면 <a href='topic:preview_quality'>CPU 호환 모드</a>로 바꾼 뒤 프로그램을 다시 시작합니다.</p>
<h2>지원 정보</h2>
<p><b>도움말 → 프로그램 정보</b>에서 버전·실행 환경을 확인하고 진단 정보를 복사하거나 로그 폴더를 엽니다. 문의할 때 진단 정보와 최근 로그를 함께 보내 주세요. 프로그램이 비정상 종료되면 다음 실행 때 오류 보고 창이 나옵니다.</p>""",
       """<h2>The app does not start</h2>
<p>Never move the EXE out of its folder; it needs the <code>_internal</code> folder next to it. If files seem damaged, run the <a href='topic:maintenance'>integrity check</a>.</p>
<h2>Media cannot be found</h2>
<p>Point to the new locations in the Missing media window when the project opens. Save projects that move between PCs as <code>.pvsproj</code> with content included.</p>
<h2>Export or mixing fails</h2>
<ul><li>Check the status in <b>Settings → FFmpeg</b> and reinstall the recommended version if needed.</li>
<li>If a GPU encoder fails, retry with CPU H.264 (<code>libx264</code>).</li>
<li>If the disk is full, change the output location or free space. The export progress window shows storage use.</li></ul>
<h2>Preview looks wrong</h2>
<p>If GPU preview is unstable, switch to <a href='topic:preview_quality'>CPU compatibility mode</a> and restart.</p>
<h2>Support information</h2>
<p><b>Help → About Playlist Canvas</b> shows the version and runtime, copies diagnostics and opens the log folder. Send the diagnostics and the latest log when asking for help. After a crash, an error report appears at the next start.</p>""",
       ("maintenance", "ffmpeg", "projects"),
       ("오류 문제 로그 진단 정보 프로그램 정보 실행 안됨 충돌 비정상 종료 누락 미디어 실패 디스크 공간",
        "error problem log diagnostics about does not start crash missing media fail disk space")),
)

_ENTRIES: tuple[_Entry, ...] = _CANVAS + _PREVIEW + _LYRICS + _AUTOMIX + _TRACK + _OTHER
