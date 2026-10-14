# 미리보기 응답성과 조작 개선 — 2026-10-08

전체 재생 미리보기의 타임라인 드래그와 CPU/GPU 화면 갱신을 개선했다. 기존 자막·가사·노래방 출력과 내보내기 설정은 유지하고 새 의존성을 추가하지 않았다.

## 수정 내용

- **드래그 중 반복 작업:** 기존에는 마우스 이동마다 전체 화면을 즉시 다시 그리고 오디오 탐색을 요청했다. Qt의 슬라이더 누름/해제 상태를 연결하고 기존 예약 갱신을 취소 가능한 단일 QTimer로 바꿨다. 드래그 중에는 목표 FPS 간격으로 최신 위치만 그리며, 놓을 때 예약 작업을 취소하고 최종 위치를 즉시 반영한다.
- **재생 상태:** 드래그 중 오디오·영상·재생 시계의 자동 진행을 잠시 멈춘다. 재생 중이었다면 최종 위치에서 재개하고, 일시정지 중이거나 드래그 도중 일시정지를 누르면 멈춘 상태를 유지한다. 드래그 시간이 재생 시계나 FPS 품질 판단에 섞이지 않게 한다.
- **비동기 로딩:** 뒤늦게 도착한 미디어 로딩 신호가 드래그 중 오디오를 재개하지 않게 한다. 영상 탐색 결과는 마우스를 누르고 있는 동안에도 갱신한다. 진행 중 AutoMix의 대기 믹스 교체는 드래그가 끝난 시점에 수행한다.
- **CPU 품질:** GPU에만 적용되던 기존 자동 품질 조절을 CPU에도 적용한다. 지속적인 부하에서 미리보기 렌더링 배율을 낮추고 안정적인 샘플이 쌓이면 복구한다. 한 번의 지연에 품질이 흔들리지 않는 기존 정책과 별도의 영상 디코더 예산은 유지한다.
- **사용성:** 첫 곡의 이전 곡 버튼과 마지막 곡의 다음 곡 버튼을 비활성화했다. 타임라인에 드래그·재생 상태·키보드 탐색 안내와 접근성 설명을 제공했다. 키보드·프로그램을 통한 탐색은 즉시 반영한다. 창을 닫으면 예약 화면 갱신도 중단한다.

## 사용자 조작 시뮬레이션

실제 Qt 미리보기, 1280×720 캔버스, 글로우 효과를 사용하는 2,000개 이중 언어 가사, 120초 WAV에서 연속 마우스 이동 이벤트 60개를 보냈다. 오디오는 검증 중 음소거했다. 드래그가 끝난 위치는 양쪽 모두 95.96초였으며 재생 의도도 유지됐다.

| 항목 | 변경 전 | 변경 후 |
| --- | ---: | ---: |
| 연속 드래그 처리 | 1,026.73ms | 21.91ms |
| 화면 갱신 요청 | 60회 | 1회 |
| 오디오 탐색 요청 | 60회 | 1회 |
| 최종 위치 | 95.96초 | 95.96초 |
| 놓은 뒤 재생 상태 | 재생 | 재생 |

연속 입력 묶음의 응답성 비교이며 모든 드래그가 한 프레임만 표시된다는 의미는 아니다. 천천히 끌거나 누른 채 기다리면 중간 위치도 목표 FPS 간격으로 갱신한다. 실제 시간은 입력 간격과 시스템 부하에 따라 달라진다.

원본: [변경 전](benchmarks/2026-10-08-preview-before.json), [변경 후](benchmarks/2026-10-08-preview-after.json). [한국어 미리보기 화면](benchmarks/2026-10-08-preview.png).

## 실제 영상 검증

- 미리보기·미디어·자막 내보내기·프레임 일치 관련 **16개 모듈, 182개 테스트: 실패 0, 건너뜀 0**.
- 실제 MP4와 이중 언어 노래방 가사를 겹친 상태에서 재생 → 드래그 → 재개 → 일시정지 탐색을 확인했다.
- 실제 CPU 영상의 30초 연속 재생, 영상 반복, 10회 미리보기 열기·일시정지·탐색·닫기와 메모리 상한 검사도 통과했다.
- 추가한 응답성 테스트 10개는 최신 위치 반영·중간 갱신·오디오 로딩·정지 의도·닫기·즉시 탐색·CPU 품질 복구·AutoMix 교체·영상 도착 갱신을 검증한다.
- GPU 미리보기 창에서도 실제 H.264 영상과 글로우/노래방 가사를 함께 재생했다. Windows 11, Python 3.12.10, PySide6 6.11.1, OpenGL 3.3, NVIDIA RTX 5060, 1280×720, 목표 30FPS, 5초 준비 뒤 약 10초 측정했다.

| GPU 실측 | 결과 |
| --- | ---: |
| 새로운 장면 표시 평균 | 30.22FPS |
| 영상 디코더 프레임 수락 평균 | 28.24FPS |
| 장면 간격 P95 | 40.63ms |
| 50ms 초과 장면 간격 | 2회 |
| 최대 대기 장면 수 | 1개 |

GPU 결과는 이번 구현의 실제 동작 확인이며 변경 전 대비 FPS 향상 수치가 아니다. 가사 애니메이션도 장면을 갱신하므로 장면 FPS와 영상 디코더 FPS를 구분했다. 순간 지연은 남아 있으며 모든 환경에서 일정한 30FPS를 보장하지 않는다. 이 영상의 Qt 디코더 핸들은 `NoHandle`이므로 GPU 합성 경로를 확인한 것이고 디코딩부터 화면까지의 zero-copy를 입증한 것은 아니다.

원본: [GPU 측정과 프로파일](benchmarks/2026-10-08-preview-gpu.json), [테스트 모듈별 결과](benchmarks/2026-10-08-preview-tests.json).

재현(PowerShell):

```powershell
$env:TEMP = Join-Path (Get-Location) 'work\test-temp'
$env:TMP = $env:TEMP
$env:PYTHONIOENCODING = 'utf-8'
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
.build-venv312/Scripts/python.exe tools/preview_workflow_benchmark.py --output work/preview-benchmark.json --screenshot work/preview.png
.build-venv312/Scripts/python.exe scripts/run_tests.py preview frame_state export_frame_equivalence mix_visual subtitle_export --timeout 240 --report work/preview-tests.json
```

실제 미디어 검사는 `PLAYLIST_CANVAS_TEST_FFMPEG`에 설치된 FFmpeg 경로를 지정하고, 30초 CPU 연속 검사에는 `PLAYLIST_CANVAS_RUN_PREVIEW_SOAK=1`, `PLAYLIST_CANVAS_PREVIEW_SOAK_SECONDS=30`, `PLAYLIST_CANVAS_PREVIEW_SOAK_BACKEND=cpu`를 사용했다.

```powershell
.build-venv312/Scripts/python.exe tools/performance_benchmark.py preview --lyrics --backend gpu_layers --platform windows --seconds 10 --warmup 5 --width 1280 --height 720 --ffmpeg 'C:/path/to/ffmpeg.exe' --profile work/preview-gpu-profile.json --output work/preview-gpu.json
```

소스 변경을 반영했다. 실행 파일·설치 프로그램은 새로 빌드하지 않았다.
