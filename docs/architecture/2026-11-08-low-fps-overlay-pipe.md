# 9단계: 일정한 반복 간격의 Canvas 투명 레이어를 낮은 FPS로 전달

## 배경

`stream-investigation.json`의 실제 1920×1080, 60FPS 프로젝트에서:

| Canvas 스트림 | 장면 렌더링 횟수 | 원시 전송 프레임 | 전송 데이터(10진 GB) |
|---|---:|---:|---:|
| base | 13,395 | 13,395 | 111.10 |
| layer:0 | 13,395 | 13,395 | 111.10 |
| **layer:1** | **2,679** | **13,395** | **111.10** |

`layer:1`은 하나의 이미지당 대략 다섯 장의 출력 프레임으로 확장되는 형태다. 이전 단계의 CFR 상태 제거는 **작업 지시**만 줄이고, FFmpeg에 쓰는 4바이트/픽셀 원시 프레임 수는 바꾸지 못했다.

## 변경: 검증된 균일 프레임 반복만 낮은 FPS 파이프 전송

`uniform_cfr_frame_step`이 스트림의 **전체 반복 가능한 시간표**를 확인한다. 원래 60FPS CFR writer가 사용하는 누적 `max(1, floor(total_seconds * fps + 0.5))` 반올림을 그대로 따라간다. 모든 새 이미지가 **연속으로 정확히 같은 출력 프레임 수**를 차지하고, 해당 정수 step으로 출력 FPS가 나뉘며, 낮춘 입력 FPS가 10 이상일 때만 안전한 step으로 판정한다.

- 60FPS에서 step=5인 경우 **Canvas 작업자 → FFmpeg는 12FPS RGBA 원시 비디오**를 보낸다.
- 원래 프레임 번호(5,10,15...)는 `cfr_target_frame_count`로 보존하며, 작업자 writer에는 변환된 **입력 프레임 번호(1,2,3...)**만 전달한다.
- FFmpeg 합성 필터에서 해당 입력의 `fps=60:start_time=0`을 먼저 실행한 후 기존 Z-index 순서대로 오버레이를 적용한다.
- 이 필터 다음에 `setpts=N/(60*TB)`를 다시 추가하지 않는다. 실제 FFmpeg YUV420P 테스트에서 재설정한 타임스탬프가 일부 경계 프레임을 어긋나게 할 수 있었다.
- 기본 `base` 스트림은 60FPS 유지. 여러 속도를 가진 애니메이션, 가사/키프레임 변경 시점, 짧은 특수 전환 등 **일정 조건을 한 번이라도 위반하는 레이어는 기존 60FPS 파이프로 자동 복귀**한다.
- 대체 PNG/직렬(Canvas worker 미사용) 내보내기와 에디터 미리보기는 기존 코드 경로 그대로.

### 계산 예시 (실측 성능 향상률 아님)

`layer:1`이 위의 **정확한 5프레임 균일 조건을 만족한다면**:

- 기존 전송: `13,395 × 1920 × 1080 × 4 = 111,103,488,000 bytes` (111.10 GB)
- 새로운 전송: `2,679 × 1920 × 1080 × 4 = 22,220,697,600 bytes` (22.22 GB)
- **해당 레이어 한 개의 원시 FFmpeg 입력량이 80% 감소**한다. base/layer:0는 그대로다.

이는 총합 기준 기존 333.31GB → 244.42GB로 약 26.7% 감소 가능성이다. 하지만 **실제 사용자 프로젝트가 전체 시간표 검사에 통과했는지 아직 알 수 없으므로** 단정하지 않는다. FFmpeg 내부의 `fps`가 프레임을 재복제하므로 CPU 사용량이나 완료 시간이 꼭 비례해 줄어들지는 않는다.

## 안전장치와 복구

- `PLAYLIST_CANVAS_DISABLE_LOW_FPS_OVERLAY_PIPE=1`이면 신규 전송 최적화를 사용하지 않는다.
- `PLAYLIST_CANVAS_DISABLE_CFR_SAMPLE_PRUNING=1`인 경우에도 신규 전송 최적화는 비활성화된다.
- 일정 검사가 실패하면 자동으로 이전 60FPS 파이프를 사용한다.
- 수정 범위는 부모의 프로파일 설정, 프레임 일정 사전 검사, 격리된 Canvas worker의 rawvideo 입력 FPS와 FFmpeg 필터 그래프뿐이다. 렌더링 소스/가사/반전/투명도/좌표/최종 FPS/NVENC 오디오 처리 변경 없음.

## 실제 프로젝트에서 측정

기존 경로, PowerShell:

```powershell
$env:PLAYLIST_CANVAS_DISABLE_LOW_FPS_OVERLAY_PIPE = "1"
python tools/export_bottleneck_profile.py --output build/lowfps-before.json -- python main.py
```

전체 내보내기를 완료하고 앱을 종료한다. 다음 실행을 별도로 시작한다.

```powershell
Remove-Item Env:PLAYLIST_CANVAS_DISABLE_LOW_FPS_OVERLAY_PIPE -ErrorAction SilentlyContinue
python tools/export_bottleneck_profile.py --output build/lowfps-after.json -- python main.py
```

같은 프로젝트와 동일 출력 설정으로 내보내고 앱을 종료한다.

```powershell
python tools/export_ab_verify.py `
  --baseline-profile build/lowfps-before.json `
  --optimized-profile build/lowfps-after.json `
  --baseline-video "C:\Videos\export-before.mp4" `
  --optimized-video "C:\Videos\export-after.mp4"
```

`summary.canvas_streams["layer:1"].pipe_fps`에서 입력 FPS를 확인한다. 60이면 자동 판정에서 기존 동작을 유지했다는 뜻이다. 12인 경우 `written_frames`와 `writer_io.bytes_written`가 실제로 약 5분의 1로 줄어들어야 한다. 출력 `fps`는 계속 60이어야 한다.

비교 도구는 기본적으로 초반/중간/후반 3프레임의 디코딩 해시만 확인하므로 모든 가사 순간까지 검증하지 못한다. 가사·키프레임 경계 전후 시점도 직접 검토해야 한다.

## 검증

- `tests/test_low_fps_canvas_overlay.py`: 60→12fps/30fps 정수 간격 선택, 전체 시간표 중 한 번의 이상 시각을 만나면 자동 fallback, writer 60fps와 12fps의 원본 RGBA 프레임 5배 바이트 수 차이 검증.
- 실제 FFmpeg(개발 환경)에 60FPS RGBA 전체 입력 또는 12FPS RGBA 입력+프레임 복원 필터를 적용하고, 10/15/25/55/60/125/300개 등 여러 출력 프레임 수의 **전체 디코딩된 YUV420P 픽셀이 동일**함을 확인했다.
- 위 검사를 CI에서도 FFmpeg 실행 파일을 이용할 수 있을 때 수행하도록 구현했다. Windows GitHub Actions에서 FFmpeg 자동 설치 단계가 실패할 경우 해당 통합 테스트는 실행되지 않는다.
- **1080p 실제 프로젝트에서 얼마나 빨라지는지는 아직 검증되지 않았다.** 최적화된 파이프가 판정 기준을 통과하는지부터 `pipe_fps`를 확인해야 한다.

## 별도 후속 작업

일정이 균일하지 않아 낮은 FPS 원시 파이프로 전환할 수 없는 레이어는 더 일반적인 타임스탬프를 가진 VFR lossless 입력 (FFV1/NUT 등)의 구현·검증이 필요하다. 그 방식은 프레임 생성·알파·timestamp·FFmpeg filtergraph을 더 크게 바꾸므로 이번 안전한 정수 반복 최적화와 분리한다.
