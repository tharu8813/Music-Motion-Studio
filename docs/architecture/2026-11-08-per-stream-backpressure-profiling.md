# 8단계: Canvas 스트림별 FFmpeg 역압력(backpressure) 추적

## 기존 A/B 측정에서 확인된 사항

2026-11-08 사용자 제공 **3분 43.25초, 1920×1080, 60FPS, 13,395프레임** 내보내기 기준:

- 최적화 전후 전체 시간: 289.46초 → 288.03초 (**0.50% 감소**, 단 1회씩 측정)
- FFmpeg 관측 CPU-core 시간: 1,138.61 → 941.38 CPU초 (**17.32% 감소**)
- Canvas 작업자 CPU-core 시간: 699.46 → 749.61 CPU초 (**7.17% 증가**)
- 최적화 후 작업자 3명 중 **2,679개 Canvas 프레임을 그린 작업자**의 `export.frame_pipe_write_seconds`는 **261.69초**, 실제 Qt 장면 렌더링 `canvas.scene_render_seconds`는 **6.79초**였다.
- 다른 작업자 2명은 각 13,395개 프레임을 그렸고, 파이프 쓰기는 각각 62.81초와 100.50초였다.

작업자별 시간이 서로 **병렬로 겹친다**는 점에 주의해야 한다. 파이프 쓰기 시간은 FFmpeg가 읽을 때까지 기다리는 시간을 포함하므로, 여기서 곧바로 Windows 파이프 자체가 병목이라고 결론 낼 수는 없다. 시점 동기화와 레이어 처리 속도를 함께 측정해야 한다.

## 계측 개선

### 1. Windows WriteFile 분해

`_WindowsNamedPipeEndpoint`는 다음 집계값을 모은다.

| 필드 | 의미 |
|---|---|
| `write_calls` | 실제 Win32 WriteFile 요청 건수 |
| `immediate_calls` | 동기적으로 완료된 WriteFile 수 |
| `pending_calls` | ERROR_IO_PENDING으로 비동기 완료된 수 |
| `partial_calls` | 요청 크기보다 적게 완료되어 나머지를 재시도한 수 |
| `bytes_written` | 실제 전송된 rawvideo 총 바이트 |
| `write_api_seconds` | Win32 WriteFile **호출 자체**에 소요된 벽시계 시간 |
| `pending_wait_seconds` | 완료되지 않은 I/O를 기다리는 데 사용한 벽시계 시간 |

`pending_wait_seconds`는 일반적으로 **FFmpeg 소비 속도에 따른 backpressure**를 포함한다. 이것만으로 CPU가 바빴거나 FFmpeg 필터가 느렸다는 증거는 아니다. 새 카운터는 진단용이며 영상 픽셀, FPS, 인코딩 품질, 버퍼 크기, FFmpeg 합성 동작을 변경하지 않는다.

### 2. 스트림과 실제 프로세스 연결

각 독립 Canvas worker의 `*.timings.worker-PID.json`에 다음 문맥을 저장한다.

```json
{
  "context": {
    "stream_key": "layer:0",
    "stream_width": 320,
    "stream_height": 180,
    "output_fps": 60,
    "submitted_states": 2679,
    "written_frames": 2679,
    "writer_io": {
      "write_calls": 13000,
      "pending_calls": 12000,
      "immediate_calls": 1000,
      "partial_calls": 0,
      "bytes_written": 617241600,
      "write_api_seconds": 0.4,
      "pending_wait_seconds": 250.0
    }
  }
}
```

위 값은 **형식 설명용 임의 예시**이며 사용자의 실제 측정이 아니다. `tools/export_bottleneck_profile.py`의 통합 보고서 `summary.canvas_streams`에서 `base`와 `layer:N`별로 CPU 렌더링/파이프 쓰기와 Win32 대기 정보를 확인할 수 있다.

### 3. 프로세스 분류 개선

Windows의 Python 가상환경 실행기가 별도 `python.exe main.py` 자식 프로세스를 생성하면, 이 실제 에디터 자식의 CPU를 `other`가 아닌 `editor`로 분류한다. Python multiprocessing spawn 작업자는 별도로 `canvas_workers`로 유지한다.

이는 **분류 정확도 변경**이다. CPU 부하를 줄이는 최적화가 아니므로 과거 보고서와 비교 시 editor/other의 수치가 바뀔 수 있다.

## 동일 설정으로 재측정

기존 최적화 기능은 그대로 사용하고, 진단 도구를 통해 한 번 내보낸 뒤 프로그램을 완전히 종료한다.

```powershell
python tools/export_bottleneck_profile.py --output build/stream-investigation.json -- python main.py
```

가능하면 `layer:0`와 다른 각 레이어의 `width × height`, `pending_wait_seconds`, `write_api_seconds`, `write_calls`, `frame_pipe_write_seconds`, `scene_render_seconds`를 비교한다.

- `pending_wait_seconds`가 높고 `write_api_seconds`가 작으면, Python/WinAPI 호출 오버헤드보다는 FFmpeg 소비 대기가 주요 부분일 가능성이 높다.
- `write_api_seconds`가 큰 경우 Windows 호출 자체의 시간 비용도 고려한다.
- 둘 다 작지만 `frame_pipe_write_seconds`가 크다면 즉시 완료 결과 확인, 큐·스케줄링 또는 별도 코드 경로를 조사한다.
- 각각의 레이어가 표시되는 타임라인 구간에 따라 `written_frames`가 서로 다를 수 있다. 그 수치만으로 프레임이 누락되었다고 판단하지 않는다.
- 전체 내보내기 소요 시간의 유의한 개선은 **동일 프로젝트를 3회 이상 반복**해 평균/변동 범위를 확인한 뒤 판단한다.

## A/B 보고서

변경된 `tools/export_ab_verify.py`는 두 프로파일 모두 `summary.canvas_streams`를 포함하면 추가로 **Canvas stream I/O and backpressure** 표를 생성한다.

```powershell
python tools/export_ab_verify.py --baseline-profile build/stream-before.json --optimized-profile build/stream-after.json
```

이 표는 같은 `stream_key`를 기준으로 파이프 쓰기 총 시간, Win32 I/O 완료 대기, 원시 전송량 및 호출 수를 비교한다. 기존 보고서로도 내보내기 총 시간은 비교할 수 있지만, **옛 프로파일에는 스트림 이름과 Win32 대기 계측이 없으므로 새 필드를 채울 수 없다.**

## 검증 범위

- Win32 즉시 완료/비동기 완료/부분 쓰기 시 전송 바이트의 보존과 이벤트 집계
- Python 가상환경 자식 에디터, Canvas spawn 작업자, FFmpeg 분류
- worker JSON에서 통합 프로파일로 메타데이터 병합 및 새 A/B Markdown 표 출력
- 영상 파이프라인의 FFmpeg 명령·색상 변환·프레임 반복 횟수·출력 파일 구조는 변경하지 않음

이번 PR은 **진단 기반 개선**이며, 사용자 장비에서 실제 병목의 원인을 판정하거나 속도 향상을 주장하지 않는다.
