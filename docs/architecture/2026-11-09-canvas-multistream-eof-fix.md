# 실시간 Canvas 다중 파이프 종료 정체: FFmpeg 저FPS 레이어 EOF 조정

## 관측된 회귀 (2026-11-09 Windows 1080p60 A/B)

사용자가 제공한 `lowfps-before.json`, `lowfps-after.json`, 작업자별 timing 파일을 분석한 결과:

- 저FPS를 끈 단일 실시간 내보내기는 약 **292.05초**였다.
- 저FPS를 켠 실행은 `layer:1`에 **12FPS**가 적용됐고 해당 파이프의 쓰기 시간이 **270.23초 → 8.03초**로 감소했다.
- 동시에 `base`와 `layer:0`은 마지막 2프레임의 파이프 쓰기에서 약 120초 대기했다. FFmpeg 실시간 파이프 경로가 정체된 뒤 중간 파일 경로로 재시도되어 사용자 작업 시간은 약 **967.35초**로 늘었다.
- 실제 FFmpeg 종료 로그는 없어 **원인을 100% 단정할 수 없지만**, 12→60FPS 변환 필터의 마지막 반복 프레임 처리가 상위 입력의 EOF를 기다리는 상황과, 기존 부모의 **순차적 `pipe.finish()`**가 교착될 가능성이 코드상 발견됐다.

PR #38은 저FPS rawvideo 파이프를 **기본 비활성화**했으며, 이 패치 역시 해당 안전 기본값을 유지한다.

## 변경

### 1. 작업자 종료 신호와 완료 대기 분리

`CanvasProcessStream.request_finish()`는 `None` EOF 패킷을 해당 작업자의 bounded job queue에 **한 번만 추가하고 반환**한다. 이미 전송한 샘플 뒤에 EOF가 배치되므로 프레임 순서가 보존된다.

기존 `CanvasProcessStream.finish()`는 `request_finish()`를 호출하고 완료를 기다리므로, 기존 개별 작업자 API 역시 유지한다.

### 2. 모든 독립 Canvas 파이프에 EOF를 먼저 알림

`ExportSession._run_piped`은 모든 프레임 상태를 제출한 다음 `_request_worker_eofs()`를 호출한다.

1. `CanvasProcessStream`만 골라 **원시 입력 FPS가 낮은 레이어부터** EOF 요청
2. 이어서 기본 영상과 나머지 레이어의 EOF 요청
3. 그다음 기존 순서대로 각 작업자의 `finish()` 완료를 대기

이렇게 하면 부모가 기본 영상의 프레임 쓰기 완료를 기다리는 동안, FFmpeg의 `fps=60` 프레임 보간 필터가 `layer:1`의 EOF를 먼저 받을 수 있다.

직렬 `PipedCanvasStream`, 미리보기, 오디오, PNG 중간 파일 경로에는 이 선행 EOF 메시지가 적용되지 않는다.

### 3. 측정 보강

부모 작업자 큐 진단에 다음을 추가한다.

- `eof_requested`: EOF가 정상적으로 큐에 전달되었는지
- `eof_drain_seconds`: EOF 전달 후 해당 작업자가 마지막 파이프를 쓰고 종료할 때까지의 시간

기존의 `frame_count`, `writer_io`, `worker_completed`와 함께 종료 시점에 어떤 스트림이 대기했는지 확인할 수 있다.

## 테스트와 한계

- 단위 테스트: EOF idempotence, 요청 중 오류 시 재시도, 저FPS 레이어가 먼저 EOF를 받고 **모든 작업자에게 신호를 보내기 전에 하나도 finish되지 않음**, 직렬 경로 무변경
- 실제 통합 테스트: Windows CI에서 독립 Canvas 작업자 3개(`base` 60FPS, `layer:0` 60FPS, `layer:1` 12FPS)를 생성하고, 실제 FFmpeg로 360장의 최종 60FPS 영상을 합성한 뒤 입력 작업자별 360/360/72장과 최종 360장, 전체 EOF 완료를 검사
- 이 테스트는 기존에 단순 파일 기반 FFmpeg 이미지 비교와 달리 **실제 Named Pipe 및 독립 spawned Qt 작업자**를 통과한다.
- 작은 파일 기반 FFmpeg 7.1.5 및 Linux FIFO 시뮬레이션만으로는 원래의 120초 Windows 정체를 재현하지 못했다. 따라서 CI와 사용자 실환경 A/B 검증 전에는 해결했다고 단정할 수 없다.

## 재측정 방법

**일반 사용자는 추가 환경변수 없이 최신 프로그램을 사용하면 기존 안전한 60FPS 입력으로 동작한다.**

실험용 저FPS를 사용하려면 문제가 있는 파일을 덮어쓰지 않는 별도 출력 이름으로 아래처럼 측정한다.

```powershell
$env:PLAYLIST_CANVAS_ENABLE_LOW_FPS_OVERLAY_PIPE = "1"
Remove-Item Env:PLAYLIST_CANVAS_DISABLE_LOW_FPS_OVERLAY_PIPE -ErrorAction SilentlyContinue
python tools/export_bottleneck_profile.py --output build/lowfps-eof-test.json -- python main.py
```

한 번의 내보내기 후 앱을 종료하고 JSON의 `summary.canvas_streams`를 확인한다.

판정 기준:

1. `layer:1.pipe_fps == 12` 및 `written_frames == 2679`
2. `base` 및 `layer:0`의 `written_frames == 13395`
3. 세 작업자 모두 `worker_completed == true`와 `eof_requested == true`
4. `export.renderer_seconds` 1회, `export.capture_session_seconds`가 추가로 나타나지 않아야 함
5. 두 방식의 실제 MP4 프레임 수·길이·해상도·오디오 및 샘플 픽셀 비교

**주의:** 성공 조건을 모두 만족하고 사용자 환경에서 반복 측정이 완료되기 전까지 저FPS 옵션의 기본 활성화는 금지한다.
