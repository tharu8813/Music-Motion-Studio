# t29 GPU 내보내기 소스·타임라인 호환성

2026-11-10 코드 기준. 이 문서는 릴리즈 노트가 아니라 구현·검증 계약이다.
GPU 엔진 전체 완성이나 기존 엔진 대비 가속을 뜻하지 않는다.

## Production 경로

`native_project_export.export_project_document` → `native_project_frames` →
`capture_native_canvas_track_frame` / `native_dynamic_layers` → native packet.
기존 엔진의 CanvasSnapshot, compiled timeline, PythonVisualizerRenderer와
KeyframeRenderBinding/CustomMotionRenderBinding을 재사용한다. Qt 래스터화와
오디오 분석은 CPU 작업이다. t28의 레이어 재사용과 별도로 동적 변환은 현재
Qt full-frame plane에서 수행하며 GPU affine 명령으로의 이전은 별도 통합이다.

| SourceType | 기존 FFmpeg 엔진 | production native 프레임 공급 |
| --- | --- | --- |
| IMAGE, LOGO, WATERMARK | CanvasSnapshot | 같은 painter. 파일 존재 검사 |
| TEXT, TIME | CanvasSnapshot | 같은 트랙·전역 시간과 painter |
| SHAPE | CanvasSnapshot | 같은 painter |
| PROGRESS_BAR | CanvasSnapshot | 같은 compiled plan·진행 시간 |
| ALBUM_COVER | 트랙별 CanvasSnapshot | 같은 트랙 표지 평가 |
| BACKGROUND | CanvasSnapshot, 트랙별 bass 분석 | 같은 painter와 원본 트랙 bass envelope |
| LYRICS | CanvasSnapshot | 같은 TTML/KRC cue·단어·겹침·karaoke 평가 |
| TRACK_LIST, NOW_PLAYING | CanvasSnapshot | 같은 playlist/track state |
| AUDIO_VISUALIZER | PythonVisualizerRenderer + FFmpeg transform | 같은 오디오 분석·프레임 생성 + native_dynamic_motion |
| AUDIO_WAVEFORM | PythonVisualizerRenderer + FFmpeg transform | 같은 waveform 프레임 + native_dynamic_motion |
| AUDIO_LEVEL_METER | PythonVisualizerRenderer + FFmpeg transform | 같은 stereo/peak history + native_dynamic_motion |
| PARTICLE_OVERLAY | PythonVisualizerRenderer + FFmpeg transform | 같은 seed·시간·프레임 생성 + native_dynamic_motion |
| VIDEO | video occurrences + FFmpeg decode/filter/overlay | **미지원**. 정확한 CFR decode/seek·repeat/speed·mask·색 필터 공급 없음 |

위 지원은 production `source_policy="dynamic-audio"`에 해당한다. 1..64 visible
source, 최대 8 packet layer, 서로 다른 static/dynamic Z, 짝수 64x64..3840x2160,
scale .25..1, FPS 1..120 조건은 그대로 유지한다. dynamic 이외 음악 반응
source는 BACKGROUND/TEXT/LYRICS만 허용한다. 그 밖은 preflight 오류다.

## 타임라인·모션 의미

| 항목 | 계약 / 제한 |
| --- | --- |
| 일반 playlist | enabled track만 compile. CFR PTS=n/fps, ceil(duration*fps) 프레임 |
| 트랙 경계 | 정확한 경계에서 다음 track, 해당 track의 source-local clock 사용 |
| 중첩 compiled plan 평가 | `visual_segments`의 기존 전환 중간점으로 소유권 변경. audio start로 조기 전환하지 않음 |
| crossfade / AutoMix 전체 export | **미지원 유지**. production audio 준비와 GUI frame source가 같은 transition plan을 공유하는 통합 필요. 평가 helper만으로 지원 선언하지 않음 |
| custom motion / keyframe / rotation | dynamic source도 production binding.values(전역 PTS) 평가. x/y/scale/rotation/opacity를 이미지 생성 후 적용. 기본 opacity는 한 번만 적용 |
| loop / entrance / exit | 기존 PythonVisualizerRenderer가 먼저 적용. custom motion은 production binding으로 후처리 |
| TTML / KRC | 기존 CanvasSnapshot.capture_track 경로 사용. 새 parser나 placeholder 없음 |
| bass / text music reaction | 원본 트랙에서 분석하고 track-local 시간으로 조회. visualizer는 기존 normalized playlist mix 분석을 유지 |
| dynamic seek | start_frame부터 그리되 attack/release/peak 분석은 frame zero 이력 유지. motion은 요청한 절대 PTS로 평가 |
| video seek | `app.video.timeline.resolve_video_position`은 기존 preview/export scheduling 규칙. native는 media frame provider가 없어 사용을 거부하며 preview mailbox 이미지를 export frame으로 쓰지 않음 |

## 오류와 fallback 계약

`native_project_video.video_preflight_reason`은 source name/id와
`native_video_frame_provider_unavailable`를 제공한다. transition 오류에는
실제 transition_mode와 동일 compiled audio/junction 평가가 필요한 이유를 제공한다.
오디오 준비·출력 생성 전에 validation이 실행된다. 예외가 나면 프로젝트 전체를
기존 FFmpeg 엔진으로 내보내야 한다. 일부 소스 삭제, 검정 프레임, 마지막 preview
frame, 임의 none transition으로 GPU 성공을 만들지 않는다. 자동 재시도/UI 안내는
t30 담당이며 이 변경만으로 자동 fallback을 구현했다고 주장하지 않는다.

`tools/native_canvas_project_probe.py`와 `tools/native_canvas_dynamic_overlays.py`는
독립적인 구형 진단 구현이다. 해당 probe의 제한은 production 지원표와 별개다.
canvas-only는 누락 source를 명시한 PARTIAL 진단만 허용하며 최종 export가 아니다.

## 검증 상태

- 이 worktree의 Python 3.14 `.venv`에서 Rust MSVC 및 `music_motion_native 0.3.0`
  prerequisite 통과. 아래 절차는 Juniper/Kit와 공유하여 재현했고 global 설치는 하지 않았다.
  sandbox에서는 rustc Access denied(os error 5)이므로 setup/prerequisite/harness는
  승인된 `require_escalated` 실행을 사용했다. 이전 prerequisite blocker는 해소되었다.

  ```powershell
  python3.14 -m venv .venv
  ./.venv/Scripts/python.exe scripts/setup_rust_native.py
  ./.venv/Scripts/python.exe -m pip install PySide6 numpy mutagen 'PyJWT[crypto]' librosa
  ./.venv/Scripts/python.exe scripts/rust_prerequisites.py
  ./.venv/Scripts/python.exe scripts/run_tests.py native_dynamic_motion native_motion_production_parity native_project_ownership native_project_runtime native_canvas_dynamic_overlays native_canvas_project_probe keyframe_rendering custom_motion mix_visual_transitions video_timeline ttml_lyrics --report docs/architecture/benchmarks/t29-compatibility-tests.json
  ```

- 공식 harness: 관련 12개 모듈 / 142개 테스트 실행 통과, 1 skip, 24초.
  상세 결과: `benchmarks/t29-compatibility-tests.json`. 저장소 전체 테스트는 실행하지 않았다.
- 신규 `tests/test_native_motion_production_parity.py`: 실제 production
  `ffmpeg_transform` graph를 FFmpeg에서 실행하여 native packet을 검정 배경에
  합성한 픽셀과 비교한다. non-unit source.scale=1.5, x/y/scale/rotation/opacity
  동시 animation, 비정방 64x32 source, output_scale=1/.5, custom in/out의
  이동·회전·크기·투명도, 24개 timestamp 조합을 검증한다.
  Qt/FFmpeg edge resampler 차이 때문에 bitwise 일치 주장은 하지 않는다.
  중심 오차 <=1.5px, visible 경계 <=2px, union 내 RGB 평균 오차 <6/255,
  peak red 차이 <=3/255, straight RGB 차이 <=3/255를 검사한다.
  거의 투명한 outro에서는 alpha 양자화로 경계가 흔들려 centre/colour/peak만 검사한다.
- 이 비교에서 production FFmpeg의 `rotate fillcolor=none` 배경 잔상을 재현했다.
  `none`은 투명 채우기가 아니라 채우기 비활성화이므로 공통 `ffmpeg_transform`을
  `black@0`로 수정했다. 이 수정 전 animated rotation 비교는 실패했고 수정 후 통과했다.
- 신규 `tests/test_native_project_ownership.py`: midpoint ±1e-6와 정확 경계,
  gap 시작/내부/끝 직전, incoming 시작/직후의 track id·track number·global PTS·
  source offset·playback rate·local time을 검사한다. gap은 임의 트랙 프레임 대신 오류다.
- `tests/test_native_dynamic_motion.py`: 90도 회전 box 확장, straight alpha,
  opacity 1회 적용, 전역 keyframe 역방향 seek 및 생산 경로 preflight를 검증한다.
- 영상·전환 전체 지원, 실제 GPU 프레임 동등성, 오디오 sync, 긴 작업·메모리,
  동일 조건 A/B 벽시계는 완료되지 않음. t30/t31/t32의 수용 조건으로 남음.
