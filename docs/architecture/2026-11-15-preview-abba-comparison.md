# 실시간 미리보기 A-B-B-A 측정 절차 (2026-11-15)

이 문서는 [#159](https://github.com/tharu8813/Music-Motion-Studio/issues/159)의 **0단계 측정 게이트**를 위한 것이다. 실제 미리보기 FPS를 측정하지 않은 PR에서 개선 폭을 주장하지 않는다.

## 동일 조건 유지

- 같은 Windows 장치, 전원 관리, 모니터 주사율, 드라이버, Python/Qt/FFmpeg 버전 및 배경 작업 상태에서 실행한다.
- 순서는 **A1 → B1 → B2 → A2**. A=기준 main, B=후보 코드. 네 실행 모두 동일한 품질 모드·해상도·장면 옵션을 사용한다.
- 3단계 품질: `eco` (절전), `balanced` (균형, 기존 절전), `quality` (최상). **모드끼리 다른 수치를 '동일 화질에서 빨라졌다'라고 비교하지 않는다.**
- AutoMix 측정은 `--integrated --known-beats`를 사용해 실제 믹스를 준비하되, 준비가 끝나지 않은 결과는 비교 대상에서 제외한다.
- 저장된 결과의 `canvas_scale_min`/`canvas_scale_max`를 함께 확인한다. 같은 모드라도 적응형 해상도 범위가 다르면 FPS만으로 동일 화질 비교 불가.
- 네 실행의 캔버스 배율 범위가 **누락·NaN·무한대·역전·0 이하**이면 `valid_canvas_scale_ranges=false`와 `same_effective_canvas_scale=false`로 표시한다. `None == None`을 동등 화질의 근거로 사용하지 않는다.
- `warmup_seconds`와 `requested_seconds`가 네 실행 모두 동일해야 한다. 이전 JSON에 두 필드가 모두 없다면 기존 결과도 읽지만, 새 값과 누락된 값을 섞어 비교하지 않는다.
- 벤치마크의 `seconds`는 실제 경과 시간이므로 요청한 시간과 미세한 차이는 정상이다. 다만 네 실행의 최장/최단 **실측 시간이 10%를 넘게 차이 나면 비교를 거부**한다. 이벤트 루프 지연 등으로 측정 창이 달라졌을 가능성을 먼저 조사한다.
- `NaN`, 무한대, 불리언, 음수 디코더 드롭 등 유효하지 않은 숫자는 성능 지표에 0으로 대입하지 않고 제외한다.
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
- 프레임 간격 `P50/P95/P99`와 콜백 간격 `P95`는 관측된 값의 **nearest-rank(표본 수 × 분위수를 올림)** 방식으로 계산한다. 이전 버전의 P95는 일부 표본 개수에서 내림으로 계산되어 긴 지연을 과소평가할 수 있었다. 코드 변경 전후 결과를 비교할 때 계산 규칙 변경을 고려한다.
- `mean_parent_cpu_percent_of_one_core`: 앱 프로세스 CPU 시간 (100이 코어 하나에 해당).
- `decode_drops_per_second`, `rss_peak_bytes`, `frame_buffer_peak_bytes`: 디코드 손실과 메모리 비용.
- A/A, B/B 두 실행의 편차(`*_pair_spread_percent`)를 반드시 확인한다. 편차 범위보다 작은 차이로 개선을 확정하지 않는다.
- `same_effective_canvas_scale=false`이면 해상도 변화가 결과에 영향을 줬다는 경고다.
- NVIDIA 전체 GPU 사용률은 공유 장치 수치로, 해당 프로그램의 GPU 점유율 증거가 아니다.
- `performance_only=true`: 어떤 결과도 자동으로 품질·타이밍·배포 검증 통과를 뜻하지 않는다.

## 렌더링 단계별 P95 확인

A-B-B-A 보고서의 `profile_stage_p95_ms`에는 프로파일이 활성화된 실행의 **Canvas 캡처, Qt 장면 페인트, GPU 레이어 준비/업로드, 영상 CPU 필터·블러, GL 제출, GUI 지연** 등 이미 기록된 개별 단계가 포함된다.

- `baseline_p95_mean_ms` / `candidate_p95_mean_ms`: A1·A2와 B1·B2 각각의 단계별 최근 95백분위 호출 시간을 평균한 값. **실시간 전체 프레임의 P95와 다르다.**
- `sample_counts_abba`: A1 → B1 → B2 → A2의 **전체 누적 호출 수**. 캐시·프록시 사용 방식이나 장면이 달라 호출 빈도가 변하면 시간 수치만 단독으로 해석하지 않는다.
- `recent_sample_counts_abba`: 실제 최근 P95 계산에 사용된 **최근 표본 수**. 프로파일러는 최근 최대 1,024회만 유지하므로, 장시간 실행의 누적 호출 수와 다를 수 있다. 구버전 JSON에 `recent_count`가 없으면 `null`로 표시하며 누적 호출 수로 채우지 않는다. 영상 PTS 절댓값 P95에도 동일 규칙을 적용한다.
- `candidate_change_percent`와 `*_pair_spread_percent`를 함께 보고 재현 가능한 차이인지 판단한다.
- 모든 결과에서 동일한 단계가 기록되지 않았거나 유효 표본이 없으면 해당 항목은 **생략**한다. 누락을 0ms나 최적화 성공으로 간주하지 않는다.
- 단계별 계측은 **포함 관계가 있는 wall time**이다. 예컨대 Canvas 전체 캡처 안에 장면 페인트가 포함될 수 있으므로 숫자를 합치지 않는다. GL 호출 시간은 GPU 칩 내부 실행 시간이 아니다.

이 도구는 원인 후보를 좁히기 위한 진단 수단이다. 변경 전후 화면 픽셀, 타임스탬프, 음원 동기, GPU 드라이버 안정성은 여전히 별도로 검증한다.

## 영상 타임스탬프 동기 계측

프로파일이 활성화된 장면에서 실제 영상 디코더 프레임의 PTS와 미리보기의 요청 시간 차이를 기록한다.

- `video.eligible_pts_error_seconds`: 기존 **부호가 있는** 차이. 앞선 프레임은 음수, 늦은 프레임은 양수다.
- `video.eligible_pts_abs_error_seconds`: 신규 **오차 크기**. 이 값의 최근 P95를 사용해 빨리 도착한 프레임과 늦게 도착한 프레임이 서로 상쇄되지 않도록 한다.
- A-B-B-A 결과의 `video_pts_abs_p95_ms`는 A1·A2와 B1·B2의 최근 P95 평균, 반복 실행 편차, 관측 표본 수를 별도로 보여준다. CPU/GPU 단계별 포함 wall time인 `profile_stage_p95_ms`에는 섞지 않는다.
- 네 번 중 한 번이라도 유효 표본이 없으면 `video_pts_abs_p95_ms=null`이다. 영상 없음, 측정 비활성, 표본 부족을 0ms나 완벽한 동기로 오인하지 않는다.
- **주의:** 이 값은 검사 대상으로 받아들인 디코더 영상 프레임의 타임스탬프 오차다. 탐색 중 거절된 프레임은 포함하지 않으며 실제 스피커 소리와 화면 사이의 A/V 싱크를 직접 측정한 값이 아니다. 별도 청각·영상 검증은 계속 필요하다.

## 장면별 후속 검증

기존 [미리보기 실기기 검증](2026-11-04-preview-rendering-validation.md)의 A~F 장면과 비교할 때는 **과거 원본 코드/품질 모드가 현재와 다름**을 유의한다. 새로운 기준 A1/B1/B2/A2는 최신 baseline과 후보를 **동일 모드**로 다시 측정해야 한다.
