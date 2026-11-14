# 실시간 미리보기 A-B-B-A 측정 절차 (2026-11-15)

이 문서는 [#159](https://github.com/tharu8813/Music-Motion-Studio/issues/159)의 **0단계 측정 게이트**를 위한 것이다. 실제 미리보기 FPS를 측정하지 않은 PR에서 개선 폭을 주장하지 않는다.

## 동일 조건 유지

- 같은 Windows 장치, 전원 관리, 모니터 주사율, 드라이버, Python/Qt/FFmpeg 버전 및 배경 작업 상태에서 실행한다.
- 순서는 **A1 → B1 → B2 → A2**. A=기준 main, B=후보 코드. 네 실행 모두 동일한 품질 모드·해상도·장면 옵션을 사용한다.
- 3단계 품질: `eco` (절전), `balanced` (균형, 기존 절전), `quality` (최상). **모드끼리 다른 수치를 '동일 화질에서 빨라졌다'라고 비교하지 않는다.**
- AutoMix 측정은 `--integrated --known-beats`를 사용해 실제 믹스를 준비하되, 준비가 끝나지 않은 결과는 비교 대상에서 제외한다.
- 저장된 결과의 `canvas_scale_min`/`canvas_scale_max`를 함께 확인한다. 같은 모드라도 적응형 해상도 범위가 다르면 FPS만으로 동일 화질 비교 불가.
- 이 도구는 **숫자와 불확실성만** 기록한다. 픽셀 동등성, 소리 지연, 재생 탐색, GPU 컨텍스트 안전성은 별도 테스트가 필요하다.

## 재현 가능한 명령 예시

Windows PowerShell의 프로젝트 루트에서 A와 B 각각 **동일한** 다음 옵션을 사용하고, 결과 파일만 구분한다:

```powershell
python -m tools.performance_benchmark preview --platform windows --backend gpu_layers --quality balanced --fps 30 --warmup 3 --seconds 8 --width 1920 --height 1080 --video-count 2 --integrated --known-beats --styled --sample-nvidia --output build/preview-A1.json
```

다음 실행에서 출력만 `build/preview-B1.json`, `build/preview-B2.json`, `build/preview-A2.json`로 각각 바꾸고 A/B 버전을 교대로 체크아웃한다. 원본 benchmark 스크립트가 별도 `--ffmpeg`를 요구하는 환경이면 동일한 실행 파일 경로를 지정한다.

결과 4개가 준비되면:

```powershell
python -m tools.preview_ab_report --baseline build/preview-A1.json build/preview-A2.json --candidate build/preview-B1.json build/preview-B2.json --output build/preview-ABBA-report.json
```

## 판독 기준

- `presented_scene_fps`: 실제 새 장면 프레임. 화면 반복 교체 콜백(`average_fps`)을 성능 향상으로 혼동하지 않는다.
- `scene_frame_time_p95_ms`, `heartbeat_max_ms`: 긴 지연과 UI 이벤트 응답 상태.
- `mean_parent_cpu_percent_of_one_core`: 앱 프로세스 CPU 시간 (100이 코어 하나에 해당).
- `decode_drops_per_second`, `rss_peak_bytes`, `frame_buffer_peak_bytes`: 디코드 손실과 메모리 비용.
- A/A, B/B 두 실행의 편차(`*_pair_spread_percent`)를 반드시 확인한다. 편차 범위보다 작은 차이로 개선을 확정하지 않는다.
- `same_effective_canvas_scale=false`이면 해상도 변화가 결과에 영향을 줬다는 경고다.
- NVIDIA 전체 GPU 사용률은 공유 장치 수치로, 해당 프로그램의 GPU 점유율 증거가 아니다.
- `performance_only=true`: 어떤 결과도 자동으로 품질·타이밍·배포 검증 통과를 뜻하지 않는다.

## 장면별 후속 검증

기존 [미리보기 실기기 검증](2026-11-04-preview-rendering-validation.md)의 A~F 장면과 비교할 때는 **과거 원본 코드/품질 모드가 현재와 다름**을 유의한다. 새로운 기준 A1/B1/B2/A2는 최신 baseline과 후보를 **동일 모드**로 다시 측정해야 한다.
