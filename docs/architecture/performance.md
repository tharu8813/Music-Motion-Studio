# 성능 분석과 검증

## 2026-10-26: 출력되지 않는 Canvas 장면 캡처 생략

가사와 애니메이션의 서로 다른 샘플 일정이 합쳐지면, 출력 FPS보다 많은 장면을 캡처할 수 있다.
기존 인코더는 누적 길이를 `max(1, floor(seconds * fps + 0.5))`로 반올림해 프레임을 내보내므로
일부 장면은 그리기와 RGBA 변환을 끝낸 뒤에도 실제로는 한 프레임도 출력되지 않았다.

스트리밍 내보내기의 capturer가 같은 계산으로 이 장면을 식별하고 직전 이미지를 전달한다.
각 샘플의 길이와 전달 순서는 그대로 유지해 부동소수점 누적, 첫 프레임, 다음 장면의 선택과
타이밍을 보존한다. 파이프와 중간 영상 인코더도 출력 프레임이 없는 장면의 색상 변환을 생략한다.
PNG 대체 경로에는 이 최적화를 적용하지 않는다. 스트림당 직전 이미지 하나만 추가로 보관한다.
완료 로그의 `skipped_cfr_captures`에서 생략 횟수를 확인할 수 있다.

실제 21분 프로젝트, 1920×1080/60 FPS, Canvas 스트림 3개로 측정했다.
각 구간은 변경 전후 순서를 번갈아 실행했으며, 같은 PC에서 기존 설치본의 내보내기도 진행 중이었다.

| 측정 구간 | 기존 중앙값 | 개선 중앙값 | 시간 감소 | 생략한 캡처 |
|---|---:|---:|---:|---:|
| 30–33초, 각 3회 | 9.30초 | 6.98초 | 24.9% | 157 / 553 |
| 90–93초, 각 2회 | 10.72초 | 8.37초 | 21.9% | 104 / 503 |

두 구간 모두 CFR 확장 후 프레임 수와 모든 출력 픽셀의 해시가 동일했다.
이 수치는 Canvas 일정 처리·캡처·해시 검증 시간이며, 오디오/시각화 준비, FFmpeg 합성·압축,
파이프 대기를 포함하는 전체 내보내기 속도 개선치가 아니다.
원본 측정: [2026-10-26-export-capture.json](benchmarks/2026-10-26-export-capture.json).

재현: `.build-venv312/Scripts/python.exe tools/export_capture_benchmark.py PROJECT.pvsproj --start 30 --seconds 3 --rounds 3 --output work/export-capture.json`.
원본 프로젝트를 수정하지 않고 테스트용 임시 캐시에서 읽는다.
24/30/60 FPS, 불투명/투명 스트림의 픽셀·길이 동일성, 첫 프레임과 짧은 장면,
진행 callback 및 취소 검사, 불필요한 변환/I/O 생략을 회귀 테스트로 검증했다.
관련 15개 모듈의 180개 테스트에서 실패는 없었고, 조건부 테스트 6개는 생략됐다.
별도 NVENC 640×360/60 FPS/3초 실제 내보내기에서도 live·intermediate 경로가 모두
180프레임을 출력했다. 두 경로 모두 개선 코드를 사용한 동작 검증으로, 변경 전후 성능 비교는 아니다.

2026-10-05. 구조는 [overview.md](overview.md), 원본 측정은 [benchmarks/2026-10-05.json](benchmarks/2026-10-05.json)에 기록한다. Python/PySide6를 유지하고 새 dependency/native module을 추가하지 않았다.

## 조사 결과와 적용 범위

| 우선순위 | 코드에서 확인한 사항 | 이번 처리 |
|---|---|---|
| 1 Preview | `SourceItem._video_frame_changed`의 정확한 최소 간격 검사가 조금 일찍 도착한 정상 30/60fps callback을 버림 | 실제 영상 before/after 후 10% jitter 허용, cadence 회귀 테스트 |
| 2 UI blocking | `QVideoFrame.toImage()`가 GUI callback에서 실행됨. bass-reactive export 준비의 동기 decode/FFT도 확인 | Export 저음 준비는 단일 worker로 이전하고 취소/GUI heartbeat 검증. 영상 callback은 계속 계측 대상 |
| 3 copy | native NV12 준비가 원본과 같은 크기에도 fancy indexing하고 `tobytes()` 중간 복사 생성 | 동일 크기 fast path, NumPy stride packing, bytes 임시 객체 제거 |
| 4 Export | bounded pipe와 constBits 전달이 이미 있음 | stage 계측, 기존 live/staged export 동작 검증 |
| 5 분석 경쟁 | Python 분석 루프와 Preview가 경쟁 가능; ONNX와 FFmpeg 자체 병렬 실행까지 중첩됨 | 기존 Preview 분석 worker 2/낮은 우선순위/취소/cache 정책 유지, 비교용 benchmark 추가 |
| 6 Memory | GPU texture cache 예산/eviction과 최신 프레임 대기 구조가 이미 있음 | allocation/unique scene 통계, RSS/frame buffer/backlog 샘플링, 장시간 검증 |
| 7 Native | waveform/FFT 후처리/입자 수치 루프 후보 | 실제 self-time 증거 확보 전 Rust/C++ 도입 보류 |

위 표는 조사 후 수정 대상을 선택한 기록이다. 최초 개선은 아래 cadence/plane 두 곳에 적용했고, 후속 요청으로 Export 시작 응답성도 개선했다. 나머지는 계측과 기존 안전장치 유지에 집중했다.

## 1. 디코더 cadence 개선

조건: Windows, Python 3.12.10, PySide6 6.11.1, NumPy 2.5.1, FFmpeg 9.0-latest, NVIDIA GeForce RTX 5060, OpenGL 3.3, 논리 CPU 20개. 1920×1080 H.264 30fps 패턴 영상, `gpu_layers` presentation, 7초 warmup + 약 15초 측정, 추가 분석 worker 없음. profiler와 plane 최적화는 양쪽 모두 활성화했고, 이 비교에서는 cadence 검사만 바꿨다.

| 지표 | Before | After |
|---|---:|---:|
| 새 scene 표시 FPS | 16.35 | 26.16 |
| 디코더 수락 FPS | 16.35 | 28.01 |
| 새 scene 간격 평균 | 61.19 ms | 38.17 ms |
| 새 scene 간격 p95 | 105.50 ms | 72.43 ms |
| 늦은 새 scene 간격 (>50 ms) | 186 | 63 |
| 디코더 drop (모두 cadence throttle) | 206 | 29 |
| worker 압력 drop | 0 | 0 |
| presentation callback FPS | 30.32 | 30.32 |

callback FPS에는 같은 장면 repaint/frame swap이 포함된다. 이것만 보면 기존 문제를 놓친다. 새 scene serial 기준 수치를 함께 추가했다. 30fps를 항상 달성한다는 결과는 아니며, p95 지연은 여전히 남아 있다.

원인: `now - last_accepted < 1/fps`는 실제 Qt timer/decoder jitter를 허용하지 않아 nominal cadence의 프레임을 건너뛴다. 공유 함수의 기준을 `0.90/fps`로 완화해 CPU/native 경로 모두에 적용했다. 30fps 32ms, 60fps 16ms callback은 수락하고 두 배 빈도의 callback은 계속 제한하는 테스트가 있다. 이 값은 soft cap이다. 반복적으로 경계만큼 빠른 입력은 최대 약 11% 더 수락할 수 있으므로 엄격한 cap이 필요해지면 누적 deadline 방식으로 교체한다.

이 영상의 Qt decoder handle은 `NoHandle`이었다. 따라서 GPU presentation을 검증했지만 zero-copy native decode 경로의 전체 FPS 개선을 입증한 것은 아니다. plane 최적화 효과는 별도 microbenchmark에서 측정했다.

## 2. NV12 CPU plane 준비 개선

원본 함수는 작업 시작 시 clean이었던 `gpu_texture_surface.py`의 Git HEAD `58445bb`에서 추출했다. 같은 mapped QVideoFrame을 사용해 before/after를 같은 프로세스에서 번갈아 실행했다. 10회 × 각 20회, Y+UV 합계, byte 동일성 확인. 단독 실행 결과도 원본 JSON에 보존했다.

| 원본 크기 / scale | Before 중앙값 | After 중앙값 |
|---|---:|---:|
| 1280×720 / 1.0 | 5.494 ms | 0.435 ms |
| 1280×720 / 0.65 | 2.035 ms | 1.911 ms |
| 1280×720 / 0.5 | 1.159 ms | 1.128 ms |
| 1920×1080 / 1.0 | 13.466 ms | 0.647 ms |
| 1920×1080 / 0.65 | 5.859 ms | 5.474 ms |
| 1920×1080 / 0.5 | 3.515 ms | 3.247 ms |
| 3840×2160 / 1.0 | 52.000 ms | 4.155 ms |
| 3840×2160 / 0.65 | 23.135 ms | 21.492 ms |
| 3840×2160 / 0.5 | 14.367 ms | 13.467 ms |

같은 크기일 때 불필요한 sampling을 없앤 이득이 가장 크다. resize는 기존 nearest-sample 수식과 출력 byte를 유지하고 중간 bytes 복사만 줄였다. `_copy_video_plane`의 Python row loop는 NumPy stride slice/contiguous packing으로 바꿨다. frame을 unmap한 뒤에도 유효해야 하므로 최종 소유권을 가진 bytearray 복사는 유지한다.

별도 tracemalloc 실행에서 1080p full-size 준비 peak는 6,245,866→3,111,762 bytes, 4K는 24,932,266→12,442,962 bytes였다. Python/NumPy 추적 allocation이며 전체 프로세스 RSS나 GPU VRAM이 아니다. CPU plane 준비 시간은 OpenGL upload/실제 GPU 실행 시간이나 앱 전체 frame time과 다르다.

## 3. Export 시작 응답성

`ExportSession._prepare_capture()`가 GUI 스레드에서 저음 반응 배경의 전체 오디오 decode/FFT를 호출해, 준비가 끝날 때까지 창 갱신과 취소 입력이 멈췄다. scene 검사와 Canvas capture는 GUI 스레드에 두고, decode/FFT/envelope 계산만 `ThreadPoolExecutor(max_workers=1)`로 옮겼다. 기존 `pump_ui` callback으로 GUI 이벤트를 처리하며 10ms 단위로 worker 완료를 기다린다. 취소 후에도 worker가 종료될 때까지 이벤트 처리를 이어가므로 임시 파일을 먼저 지우거나 분석 스레드를 남기지 않는다.

곡별 진행 표시도 GUI에서 갱신한다. 분석 오류는 기존처럼 해당 곡만 경고 후 건너뛰고, 사용자 취소는 `RenderCancelledError`로 전달한다. 완성된 envelope만 session cache에 반영하고 취소된 부분 결과는 보관하지 않는다. 새 의존성, worker fan-out, 영구 audio cache는 추가하지 않았다. live/staged 경로가 함께 사용하는 준비 함수 한 곳을 수정했다.

10분 synthetic WAV, 실제 FFmpeg decode, 30fps/24-band FFT, 20ms Qt heartbeat 측정:

| 지표 | Before | After |
|---|---:|---:|
| 저음 배경 준비 전체 시간 | 1.696초 | 1.527초 |
| 준비 중 GUI heartbeat 실행 | 0회 | 75회 |
| 가장 긴 heartbeat 공백 | 1,695.88ms | 37.97ms |
| envelope frame 수 | 18,000 | 18,000 |
| envelope SHA-256 | `4edad114…933bc3` | 동일 |

계산 결과는 바이트 단위로 동일하다. 이 결과는 준비 중 GUI 응답성을 개선한 것이며 전체 Export가 10% 빨라진다는 의미가 아니다. FFT/Python loop가 GIL을 공유하므로 모든 시스템에서 특정 최대 지연을 보장하지 않는다. 별도 실제 분석 취소 측정에서는 설정된 100ms timer가 시작 후 125.32ms에 요청을 전달했고, 요청부터 worker 정리까지 4.28ms가 걸렸다. 오디오를 재생하지 않는 benchmark다. 원본: [benchmarks/2026-10-05-export-start.json](benchmarks/2026-10-05-export-start.json).

```powershell
& .build-venv312/Scripts/python.exe tools/performance_benchmark.py export-start `
  --audio-seconds 600 --ffmpeg 'C:/path/to/ffmpeg.exe' --output build/export-start.json
& .build-venv312/Scripts/python.exe tools/performance_benchmark.py export-start `
  --audio-seconds 600 --cancel-after-ms 100 --ffmpeg 'C:/path/to/ffmpeg.exe' `
  --output build/export-start-cancel.json
```

FFT sample rate/band 수/보간/normalization과 render 결과는 유지한다. session별 재사용 범위도 유지하므로 live 실패 후 새 session으로 재시도하면 다시 분석할 수 있다. 여러 곡 준비도 순차 처리해 PCM memory와 CPU 경쟁을 제한한다.

저음 반응 배경을 켠 3초/640×360/30fps 실제 CPU H.264 export도 기존 `export_pipe_benchmark.py`로 확인했다. live pipe와 intermediate 경로 모두 90프레임을 출력했고 두 출력의 PSNR 평균은 56.61 dB였다. 현재 두 경로의 동작 검증이며 이 실행 시간을 이번 변경의 before/after 개선치로 해석하지 않는다.

## 복사와 cache 경로

### 영상 Preview

CPU fallback: `QVideoFrame.toImage()` → QImage → bounded `VideoFrameFilterTask` → Qt resize/blur 및 필요 시 NumPy 색상 연산 → QImage/QPixmap → preview layer → OpenGL texture. `QImage(image)`는 대체로 implicit sharing이며 모두 deep copy라고 세면 안 된다. `frame_filter.py`의 색상 변경/format 변환/resize는 실제 allocation이 발생할 수 있다. 색상 shader가 처리하는 경우 CPU 색상 보정은 이미 우회한다.

Native-frame 후보: QVideoFrame을 보관 → map → NV12 Y/UV plane pack → GL texture pair → shader 합성. 이번 plane 수정은 이 경로의 CPU 준비를 줄인다. GPU handle이 있어도 현재 map/upload 구현 전체가 zero-copy는 아니다.

`GpuTexturePreviewSurface`는 layer key와 `QImage.cacheKey()`/native serial로 texture를 재사용한다. 크기가 같으면 새 allocation 대신 update한다. frame마다 전체 Canvas texture를 재생성하지 않는다. 최신 pending scene 하나만 보관하며 texture budget 기본 256 MiB, 미사용 eviction 기본 90프레임을 유지한다. 필요한 active scene이 예산보다 클 때까지 강제로 제거하는 절대 상한은 아니다.

이번 1080p 측정 창에서 texture allocation 증가는 0, texture cache는 2개/7,008,768 bytes로 유지됐다. After 창의 upload 397회/reuse 397회는 더 많은 프레임 수락에 대응한다. upload bytes는 bus 전달량 누계이고 VRAM 보유량이 아니다. 변하지 않은 layer reuse를 보존했으며 repaint swap이 새 scene으로 중복 집계되지 않는 테스트를 추가했다.

### Canvas / Export

`QGraphicsScene` → 재사용 가능한 QImage buffer/dirty region → `QImage(image)`로 queue 소유권 유지 → 필요한 RGB format 변환 → `memoryview(image.constBits())` → Windows named pipe/POSIX FIFO → FFmpeg. 이미 raw bytes 전체를 Python `tobytes()`로 만드는 구조가 아니다. queue를 넘긴 원본을 수정하면 Qt detach가 필요할 수 있으므로 소유권 복사를 무작정 없애지 않는다.

bounded pipeline은 기존 base 3/foreground 2 capacity, FFmpeg `thread_queue_size=4`를 유지한다. 느린 encoder에 backpressure를 걸고 취소를 전달한다. GPU Preview가 GPU Export의 전 과정을 대체하는 것은 아니다. CPU libx264 fallback과 기존 hardware encoder 선택 계약을 유지한다.

## 계측 사용

일반 실행에서는 `PLAYLIST_CANVAS_PROFILE`이 비어 있어 `@timed`가 원래 함수를 그대로 반환한다. timer/lock wrapper를 hot path에 남기지 않는다. 활성화 시 고정된 metric 이름마다 누적 count/total/min/max와 최근 최대 1,024개 sample의 p50/p95를 보관한다. 종료 시 JSON을 atomic replace로 쓰고 실패하면 경고만 남긴다. media/frame 내용이나 파일 경로는 저장하지 않는다.

```powershell
$env:PLAYLIST_CANVAS_PROFILE = Join-Path $PWD 'build/profile.json'
& .build-venv312/Scripts/python.exe main.py
Remove-Item Env:PLAYLIST_CANVAS_PROFILE
```

앱 진입 파일이 다른 배포 환경에서는 해당 진입 파일을 사용한다. profiler 활성화는 import 전에 해야 한다. 다음 benchmark는 오디오를 명시적으로 음소거한다.

```powershell
$env:TEMP = Join-Path $PWD 'build/performance-tmp'
New-Item -ItemType Directory -Force $env:TEMP | Out-Null
$env:TMP = $env:TEMP
$python = '.build-venv312/Scripts/python.exe'
& $python tools/performance_benchmark.py planes --iterations 30 --output build/planes.json
& $python tools/performance_benchmark.py preview --platform windows --backend gpu_layers `
  --ffmpeg 'C:/path/to/ffmpeg.exe' --width 1920 --height 1080 --fps 30 `
  --warmup 7 --seconds 60 --output build/preview.json
```

`--analysis-workers 0|1|2|4`는 실제 BasicAnalysisProvider + 4개 synthetic 오디오 파일로 경쟁을 재현한다. cold import/librosa JIT/cache 여부, 분석 완료 여부, 정상 frame 수를 함께 비교한다. fair 비교는 같은 길이/백엔드/해상도와 일정한 warmup, 독립 프로세스, 다른 benchmark/test가 없는 상태로 반복한다. 이번 2/4 worker 탐색은 cold startup/취소, 일부 다른 작업과의 겹침이 있어 정책 결정 근거로 사용하지 않았다.

| 대상 | 측정 |
|---|---|
| Preview | callback FPS와 unique scene FPS, scene/callback mean/p95/late interval, decoder accepted/throttle/pressure, refresh/scene render/capture/filter CPU wall time |
| GPU | submit/present/unique scene, upload count/bytes, reuse/allocation/eviction/cache bytes, CPU GL submission/upload 준비 시간 |
| Queue | decoder pending, GPU pending scene, 200ms sampled backlog 최대, 기존 pipe diagnostics의 pending/capacity/backpressure |
| Export | 외부 전체 경과 시간, bass prepare/capture session/frame generation/queue/delivery/audio prepare/renderer/FFmpeg process wall time |
| AutoMix | decode, beat analysis, basic downbeat guess, key, energy, vocal, structure, cache 포함 service, final/parallel mix |
| Memory | 현재 Python 프로세스 RSS 시작/끝/peak, Qt cacheKey로 중복 제거한 frame buffer bytes, GPU texture bytes |

`preview.refresh_seconds`에는 deferred/no-op 요청이 포함된다. frame 간격은 표시 빈도이고 함수 CPU time은 연산 비용이다. `late`는 목표 간격의 1.5배 초과를 뜻한다. decoder drop에는 의도한 throttle과 worker 압력 drop이 포함되므로 별도 원인을 확인한다.

`ffmpeg.process_wall_seconds`는 decode/audio/mux/encode 명령을 모두 포함하며, live pipe에서는 frame 대기도 포함한다. 순수 codec CPU/GPU 실행 시간으로 해석하지 않는다. 필요한 세부 encode-only 분석은 동일 FFmpeg 명령을 독립 실행해 비교한다. Beat This!는 beat/downbeat를 한 모델에서 함께 추론하므로 `automix.beat_downbeat_model_seconds`로 기록하고 두 시간을 임의로 나누지 않는다.

모든 timed metric은 inclusive wall time이다. 중첩/동시 구간을 합산하지 않는다. preview JSON의 profile에는 warmup/종료도 포함되고, top-level FPS/drop delta는 측정 창만 포함한다. frame buffer bytes는 Preview가 보관하는 base/dynamic/overlay QImage cache의 합이며 decoder/QPixmap/GL framebuffer 전체를 포함하지 않는다. GPU allocation bytes도 관리 중인 texture만 집계한다. 200ms sampling은 순간 queue/RSS peak를 놓칠 수 있다. RSS에는 Qt/NumPy가 포함되지만 FFmpeg/다른 프로세스의 memory는 포함되지 않는다. 실제 GPU execution timer query, process별 CPU self-time, 연속적인 모든 allocation tracing은 이번 최소 계측에 포함하지 않았다.

## GIL / 동시 실행 정책

| 방식 | 장점 | 비용/제약 | 결정 |
|---|---|---|---|
| threading 유지 | cache/session 공유, 낮은 시작 비용, 기존 취소 연결 | Python loop가 GIL 경쟁, native worker까지 중첩 | 기본 유지, Preview worker 수 제한 |
| FFmpeg subprocess | codec/decode/DSP를 GIL 밖에서 실행 | pipe copy, 프로세스 startup, CPU 경쟁은 남음 | 이미 사용 중인 backend 재사용 |
| Python subprocess / multiprocessing | Python CPU loop의 GIL 분리 | Windows spawn, ndarray 복사/IPC, model/cache 중복, frozen 앱 취소/패키징 | Python self-time 병목이 반복 측정될 때 개별 작업 단위로 검토 |
| NumPy / ONNX 내부 병렬 | native numerical loop, 가능한 GIL release | outer worker × inner thread oversubscription, memory bandwidth | 기존 설정 유지; ORT spinning off/half-core thread와 inference lock 확인 |
| 낮은 우선순위 | GUI 실행 여지 확보 | GIL 보장이나 hard real-time을 제공하지 않음 | 기존 background Qt/thread/FFmpeg 정책 유지 |
| worker 제한 | 과도한 CPU/GIL 경쟁 억제 | 분석 완료가 늦을 수 있음 | progressive preview 기존 2 유지 |
| Preview 중 지연/취소 | 중복 분석 제거, 재생 우선 | 결과 가용 시점 관리 필요 | 일반 AutoMix background pass 취소 후 preview 종료 시 재개 유지 |
| cache reuse | decode/ML/structure 반복 자체 제거 | key/invalidation 정확성 필요 | AnalysisService/structure cache 유지, cache-or-analysis 구간 계측 |

`PreviewController`는 AutoMix preview를 열 때 일반 background 분석을 취소한다. progressive preview가 필요한 분석과 기존 결과 cache를 소유한다. 이 정책을 확인하지 않고 또 하나의 executor나 cache를 추가하지 않았다. worker 수를 늘린 변경도 없다.

## 검증

앞선 Preview 성능 작업의 전체 unittest suite: **1,517개, 실패/오류 0, skip 65, 533.15초**. 추가 Windows OpenGL/계측/video filter/실제 MP4 테스트: **43개 모두 통과, 72.60초**, 60초 무음 Preview soak 포함. 후속 Export 시작 수정의 대상 회귀 검사는 **240개, 실패/오류 0, skip 9, 67.51초**였다. Qt heartbeat, GUI/worker thread 분리, 실제 DSP 결과 일치, 중도 취소와 worker 종료, session cache 재사용, 개별 곡 실패/비활성 배경 생략을 확인했다. MainWindow Export 준비/닫기/취소/오류와 Canvas frame equivalence/pipe 검사도 포함한다. 해당 실행의 skip에는 Windows에서 실행할 수 없는 POSIX FIFO 6개와 선택 실행 조건의 3개가 있다.

기존 assertion이나 테스트는 삭제하지 않았다. 테스트 QSettings는 disposable INI로 격리해 Windows registry 접근과 사용자 설정 오염을 피했고, AutoMix editor의 language fixture는 이전 언어를 복구하도록 했다. 앞선 전체 검증 harness는 테스트 프로세스의 QAudioOutput만 음소거했다.

추가 검증 명령:

```powershell
$env:QT_QPA_PLATFORM = 'windows'
$env:PLAYLIST_CANVAS_TEST_FFMPEG = 'C:/path/to/ffmpeg.exe'
$env:PLAYLIST_CANVAS_RUN_PREVIEW_SOAK = '1'
$env:PLAYLIST_CANVAS_PREVIEW_SOAK_SECONDS = '60'
& .build-venv312/Scripts/python.exe -m unittest tests.test_gpu_texture_surface `
  tests.test_performance_profile tests.test_video_frame_filter `
  tests.test_real_video_preview_performance -q
```

전체 suite의 skip은 opt-in FFmpeg/long-soak, offscreen에서 사용할 수 없는 OpenGL, 선택 설치 모델/backend 등이다. 실제 OpenGL 11개 테스트는 위 Windows 실행에서 skip 없이 통과했다. plane byte 동일성/stride/소유권, texture reuse/allocation/eviction, swap과 scene 구분, profiler bounded/concurrent/exception/save, 30/60fps jitter와 과속 제한을 확인했다. 전체 suite는 프로젝트 backward compatibility, 취소, decoder 생명주기, 반복 Preview, 수동 AutoMix, 가사/입자/애니메이션/export 등의 기존 회귀 검사도 포함한다. `compileall`과 `git diff --check`도 통과했다.

추가 독립 1080p/30fps/7초 warmup/60.20초 측정은 실제 OpenGL 3.3 사용을 확인하고 음소거로 실행했다. 새 scene 평균 **26.10fps**, 간격 p95 **73.40ms**, RSS 종료 증가 **1.16 MiB**/측정 중 peak 증가 **5.34 MiB**, texture 추가 allocation **0**, texture 보유 **7,008,768 bytes**, Preview QImage buffer peak **7,008,768 bytes**, sampled pending scene 최대 **1**이었다. 60초 동안 지속적인 texture allocation이나 큰 메모리 증가가 관찰되지 않았다는 결과이며 장시간 누수 부재를 증명하지 않는다.

기존 live/staged CPU H.264 export는 각각 90프레임, PSNR 평균 57.30 dB로 출력 확인했다. 해당 탐색은 다른 작업과 겹쳤으므로 15.53초/4.00초 실행 시간을 이번 코드 최적화의 before/after 또는 공정한 mode 성능 비교로 사용하지 않는다. GPU upload 검증과 GPU encoder end-to-end 검증은 별개이다. installer/PyInstaller 전체 배포물은 이번에 빌드하지 않았다.

## 수정 파일

최초 성능 동작 변경은 `app/canvas/source_item.py`, `app/preview/gpu_texture_surface.py` 두 곳이다. GPU allocation/unique scene 통계도 후자에 있다. 후속 Export 시작 개선은 `app/preview/export_session.py`, 검증은 `tests/test_export_session.py`, 재현 benchmark는 기존 `tools/performance_benchmark.py`의 `export-start` mode다.

새 계측/benchmark: `app/utils/performance.py`, `tools/performance_benchmark.py`. 기존 호출 경계 계측: `app/dialogs/export_preview_dialog.py`, `app/preview/{canvas_snapshot,export_canvas_capture,export_session}.py`, `app/renderer/{ffmpeg_renderer,canvas_pipe,python_visualizer}.py`, `app/automix/analysis/{basic,beat_this_onnx,service,vocals}.py`, `app/automix/structure/{service,sonara}.py`, `app/automix/{renderer,parallel_mix}.py`, `app/utils/particle_painter.py`, `app/video/frame_filter.py`.

검증: `tests/test_performance_profile.py`, `tests/test_gpu_texture_surface.py`, `tests/test_video_frame_filter.py`, `tests/__init__.py`, `tests/test_automix_editor_dialog.py`. 문서: 이 파일, `overview.md`, 원본 benchmark JSON. 이 목록은 이번 작업에서 추가한 변경만 설명한다. Git diff에는 작업 시작 전의 다른 사용자 변경도 있다.

## 내보내기 블러 후속 최적화 (2026-10-26)

블러 변환의 임시 메모리와 캐시 교체, 소비가 끝난 프레임의 참조 수명을 줄였다.
격리된 블러 변환은 약 31% 빨라졌지만 실제 20초 내보내기는 51.046 → 50.781초로,
전체 속도의 유의미한 추가 향상은 확인하지 못했다. 디코딩한 1,200프레임은 동일했다.
측정 범위와 제외한 실험은 [후속 검증 기록](export-blur-2026-10-26.md)에 있다.

## Canvas 내보내기 병렬화 (2026-10-27)

독립 Canvas 밴드의 래스터화를 별도 Qt 프로세스로 분리하고 기존 FFmpeg 파이프에
직접 공급한다. IVY 1080p/60fps/20초 표본에서 상세 계측 없이 49.406초 →
35.578/35.172초로 약 28.4% 단축됐다. 프레임·타임스탬프·음성 PCM은 동일했다.
최종 빌드된 렌더 작업자는 첫 실행 44.093초, 재실행 36.187초였고 기존 경로의 후속
재측정은 54.391초였다. 실행 편차가 있어 소스 측정 개선율을 설치본에 그대로 적용하지 않는다.
범위, 취소와 메모리 비용은 [병렬화 검증 기록](parallel-canvas-2026-10-27.md)에 있다.

## 다음 최적화 순서

1. **Preview**: 실제 프로젝트의 여러 video/blur/lyrics/visualizer 조합과 60fps/4K에서 unique scene p95 및 callback/toImage/filter 구간 확인. NoHandle fallback 비용과 flatten 트리거를 구분한다.
2. **UI 준비**: 저음 배경 준비의 worker 이전은 완료했다. 여러 영상의 metadata probe 등 남은 GUI 준비 경로를 heartbeat로 측정하고, 확인된 장시간 작업만 UI 밖으로 옮긴다. Qt scene 자체는 worker로 옮기지 않는다.
3. **copy / Export**: pipe format 변환의 필요 여부와 capture/render 비중을 확인한다. 소유권/취소/backpressure를 지키며 변환을 한 곳으로 모을 때만 변경한다.
4. **분석 경쟁**: warmed model/캐시 miss와 hit를 분리한 0/1/2/4 worker 반복 비교. 분석 처리량보다 unique scene FPS/p95/late frame을 우선한다.
5. **memory**: 다양한 layer와 반복 open/close를 포함한 수십 분 재생에서 RSS slope, texture cache, FFmpeg 별도 RSS를 확인한다. 15~60초 안정성으로 누수가 없다고 단정하지 않는다.
6. **native 후보**: waveform/RMS/band 후처리를 먼저 vectorize하고 Python self-time이 계속 frame budget을 지배할 때만 Rust + PyO3 batch API를 평가한다. 다음으로 입자/animation 수치 계산을 고려하되 Qt draw call이 병목이면 native 계산 이전만으로 해결되지 않는다. 현재 native로 옮기기로 확정한 부분은 없다.
