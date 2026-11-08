# 내보내기 다음 최적화: 프레임을 만들지 않는 Canvas 상태 작업 제거

## 실제 사용자 자료의 출발점

2026-11-08 `stream-investigation.json`의 같은 영상(1920×1080, 60FPS, 최종 출력 13,395장)을 기준으로:

| 스트림 | Canvas 샘플 상태 제출 | 전송된 CFR 출력 장수 | Qt scene render 시간 | 부모 job Queue.put 누적 시간 |
|---|---:|---:|---:|---:|
| base | 17,412 | 13,395 | 110.73초 | 9.64초 |
| layer:0 | **35,642** | **13,395** | **180.76초** | **228.54초** |
| layer:1 | 2,903 | 13,395 | 7.09초 | 0.04초 |

기존 `ExportCanvasCapturer.capture_stream`는 출력을 만들지 않는 sub-frame 상태에서 QImage 렌더링을 생략했지만, 그 상태가 **부모 → 작업자 multiprocessing.Queue와 작업자 → 파이프 파이프라인을 여전히 통과**했다.

`layer:0`의 이론적 최대 전달 상태 제거 개수는 `35,642 - 13,395 = 22,247`개, 약 **62.4%**다. 실제 전송 상태 수는 샘플 길이, 첫 프레임 및 드문 한 상태에서 여러 장의 출력 프레임이 나오는 경우에 따라 달라질 수 있다. 이는 **출력 프레임 삭제가 아니라 출력에 기여하지 않는 작업 지시 삭제**다.

## 변경점

### 1. 누적 출력 프레임 번호를 보존한 lazy 일정 변환

`app/renderer/cfr_sample_pruning.py`에 `iter_cfr_emitting_samples`를 추가했다. 원래의 병합 상태 스트림을 시간순으로 읽어 `max(1, floor(total_seconds * fps + 0.5))`가 늘어나지 않는 샘플을 제외한다.

이때 단순히 시간만 합치면 부동소수점 합산 순서가 달라져 **6.95초 @ 10FPS처럼 반 프레임 경계에서 원본과 다른 프레임을 선택**할 수 있다. 따라서 실제로 새 프레임을 출력할 각 샘플에 `cfr_target_frame_count`를 기록한다. 워커의 `PipedCanvasStream` 쓰기 스레드는 이 정수값을 그대로 사용하여 원본과 정확히 같은 프레임 번호에서 해당 샘플의 이미지를 사용한다.

- 출력을 만들지 않는 중간 샘플의 duration은 **다음 출력 샘플**에 합친다.
- 맨 끝의 출력 없는 duration은 **마지막 출력 샘플**에 합친다.
- 가사, 영상, 애니메이션 등의 `elapsed_seconds` 및 `timeline_seconds`는 원래 샘플 값을 그대로 둔다.
- 샘플 duration의 총합과 최종 CFR 출력 프레임 수를 보존한다.
- `PipedCanvasStream`의 기본 동작은 `cfr_target_frame_count=None`일 때 완전히 기존과 동일하다.

### 2. 좁은 적용 범위

- **독립적으로 생성된(spawn) 병렬 Canvas 작업자의 라이브 FFmpeg 파이프**에서만 적용한다.
- 편집 화면·미리보기·PNG 내보내기 fallback·직렬 Canvas 파이프·임시 중간 파일 인코더 경로는 변경하지 않는다.
- 이미 고정 이미지 PNG로 처리하는 invariant 스트림은 기존과 동일하다.
- FFmpeg 합성 필터·NVENC 설정·색상·투명도·좌표·GPU 처리·영상 해상도/FPS 및 오디오 코드는 변경하지 않는다.

### 3. 이전 동작으로 즉시 되돌리기

PowerShell에서 원래의 상태 전송을 사용하려면:

```powershell
$env:PLAYLIST_CANVAS_DISABLE_CFR_SAMPLE_PRUNING = "1"
python tools/export_bottleneck_profile.py --output build/pruning-before.json -- python main.py
```

새 최적화를 활성화하려면:

```powershell
Remove-Item Env:PLAYLIST_CANVAS_DISABLE_CFR_SAMPLE_PRUNING -ErrorAction SilentlyContinue
python tools/export_bottleneck_profile.py --output build/pruning-after.json -- python main.py
```

두 번 모두 동일 프로젝트, 동일 FPS/해상도/음악, 동일 FFmpeg/NVENC/전원 설정, 동일 내보내기 타임라인에서 실행하고 완료된 MP4를 각각 보존해야 한다. 실행 후 에디터를 닫아 JSON이 기록되게 한다.

## 결과 확인

- 새 로그: `Canvas CFR zero-output state pruning: enabled=True`이면 실제 병렬 워커에 활성화된 것이다.
- 종료 로그 `Canvas CFR sample pruning: {...}`에서 `input_samples`, `forwarded_samples` 비교
- `build/pruning-after.json`의 `summary.canvas_streams[...].cfr_pruning`에 스트림별 스캔/전송 수 기록
- `parent_job_queue.submit_count`와 `put_wall_seconds`, 워커의 `export.frame_generation_seconds`와 전체 `export.completed_wall_seconds`로 **실제 속도 변화를 측정**한다.

`layer:1`은 2,903개의 상태로 13,395장 QImage를 반복 전송한다. 이번 개선은 **작업 큐에 보내는 불필요한 메타데이터/상태 수만 줄인다**. 원시 RGBA/BGR0 프레임을 매번 FFmpeg에 보내는 계약은 유지하므로 `layer:1`의 약 111 GB 전송량은 줄지 않는다. 그 영역은 타임스탬프가 있는 VFR/lossless 압축 비디오 입력을 도입하는 별도의 위험도 높은 작업으로 취급해야 한다.

## 회귀 테스트

- `tests/test_cfr_sample_pruning.py`에서 10/24/29/30/60/120 FPS, 수백 개의 임의 sub-frame 일정과 정확한 반 프레임 반올림 경계를 검사
- 작업자 IPC 샘플 포장/해제 후 정수 프레임 타깃 보존 검사
- QImage 2×2 RGBA 원시 바이트를 실제 `PipedCanvasStream._write_frame`으로 쓰고, 최적화 전/후 **전체 출력 프레임 바이트가 정확히 같은지** 비교
- 기존 Canvas/FFmpeg 통합 테스트를 그대로 유지하고, 신규 테스트는 Windows CI에서 FFmpeg를 다운로드하기 전 실행

**최적화 효과는 사용자 실제 PC에서 측정해야 확정할 수 있다.** 62.4%는 `layer:0`의 메타데이터 전송 작업 수 감소 잠재치이지 전체 내보내기 시간 단축률이 아니다.
