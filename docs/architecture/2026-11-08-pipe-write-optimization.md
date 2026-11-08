# Windows Canvas → FFmpeg 파이프 최적화 (2단계)

## 실제 사용자 프로파일 (2026-11-08)

36분 30초, 13곡을 포함하는 프로젝트의 실제 내보내기 진단 JSON(약 401초 관측)을 바탕으로 다음 값을 확인했다.

| 지표 | 관측값 | 해석 |
|---|---:|---|
| `export.piped_capture_session_seconds` | 290.93초 | Canvas 캡처 및 파이프 처리의 벽시계 시간 |
| `export.renderer_seconds` | 290.01초 | 최종 인코더 처리 벽시계 시간 (위 구간과 겹침) |
| `export.frame_delivery_seconds` | 1,095.06초 | 복수 작업자의 포괄적 처리 시간 합계, I/O 대기 포함 |
| `export.frame_queue_seconds` | 403.99초 | 여러 스트림의 대기 시간 합계 |
| `canvas.scene_render_seconds` | 263.59초 | 여러 Canvas 렌더 작업의 포괄적 시간 합계 |
| FFmpeg 프로세스 CPU 시간 | 1,175.10 CPU초 | 다수 CPU 코어 사용 시간의 합계 |
| Canvas 작업자 CPU 시간 | 694.19 CPU초 | 다수 CPU 코어 사용 시간의 합계 |
| NVIDIA NVENC 사용률 | 평균 19.6% | 장치 전체 값이므로 이 프로그램의 전용 이용률이 아님 |

**위의 단계별 시간은 서로 중첩되거나 병렬로 누적된다. 단순 덧셈하거나 CPU 병목을 단정해서는 안 된다.**

## 변경 사항

- Windows named pipe의 커널 버퍼는 1 MiB로 유지하고, 사용자 공간의 `WriteFile` 요청 조각을 1 MiB → 기본 4 MiB로 늘린다. 메모리뷰를 슬라이스하므로 조각을 위해 별도 Python 바이트 복사본을 만들지 않는다. 한 1920×1080 4채널 프레임(약 7.9 MiB)의 요청 횟수는 이론상 8회에서 2회로 줄어든다. 실제 속도 향상률은 아직 실측하지 않았다.
- overlapped `WriteFile` 호출이 즉시 완료된 경우 불필요한 `WaitForMultipleObjects` 호출을 생략한다. 아직 완료되지 않은 경우에는 기존 이벤트 대기와 취소 처리를 그대로 사용한다.
- 타이밍 측정을 `export.frame_pixel_prepare_seconds`(QImage 포맷/메모리뷰 준비)와 `export.frame_pipe_write_seconds`(실제 쓰기, FFmpeg 소비 대기 포함)로 분해한다. 모두 worker별 파일에 기록되며 진단 JSON에 합쳐진다.
- 소스 픽셀, 프레임 반복 횟수, 출력 FPS, FFmpeg 필터 그래프, NVENC 설정 및 작업자 큐 크기는 변경하지 않는다.
- Win32 모의 테스트를 통해 4 MiB 조각화, 즉시 완료 및 부분 쓰기/비동기 대기의 데이터 순서를 검사한다.

## A/B 측정 방법 (PowerShell)

기준선과 비교할 때 출력 옵션, 트랙/레이어 구성, 음악 캐시 상태, 전원 상태를 동일하게 유지해야 한다. 실행할 때마다 앱을 종료해 결과를 기록한다.

기존 1 MiB 동작:

```powershell
$env:PLAYLIST_CANVAS_PIPE_WRITE_CHUNK_MIB = "1"
python tools/export_bottleneck_profile.py --output build/pipe-baseline.json -- python main.py
```

새로운 기본값 4 MiB 동작:

```powershell
$env:PLAYLIST_CANVAS_PIPE_WRITE_CHUNK_MIB = "4"
python tools/export_bottleneck_profile.py --output build/pipe-optimized.json -- python main.py
```

옵션을 초기화하려면 `Remove-Item Env:PLAYLIST_CANVAS_PIPE_WRITE_CHUNK_MIB`.

`export.frame_pipe_write_seconds`는 FFmpeg가 소비할 때까지 블로킹한 시간이 포함된다. 이 값만 줄었다고 전체 내보내기가 빨라진 건 아니므로, 먼저 `export.renderer_seconds`, 내보내기 전체 벽시계 시간, 실제 출력 프레임 수와 영상 오디오 동기화를 비교한다. 같은 설정으로 각 조건을 여러 번 측정하는 것이 좋다.

문제가 생기면 환경변수를 `1`로 설정하면 이전 WriteFile 조각 크기로 되돌릴 수 있다. 이 설정은 Windows named pipe 전송에만 적용된다.
