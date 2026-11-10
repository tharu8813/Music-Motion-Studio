# GPU 프레임 전송 계약

앱의 `native_project_export → render_native_gpu_playlist → stream_native_canvas_h264`
경로는 작업자에 `--input-format binary-v1`을 전달한다. 프레임별 BGRA 파일과
manifest 파일을 만들지 않는다. 기존 진단 CLI는 옵션 생략 또는
`--input-format manifest`로 줄 단위 manifest 경로 입력을 계속 지원한다.
이전 작업자 바이너리는 새 옵션을 거부하므로 Python 코드와 함께 다시 빌드해야 한다.

바이너리 프레임은 little-endian `<4sIQIQ>` 28바이트 헤더, UTF-8 JSON,
Z 순서의 BGRA plane으로 구성한다. 헤더는 `MMSB`, 버전 `1`, 0부터 순차 증가하는
CFR 프레임 index/PTS(u64), JSON 바이트 수(u32), 전체 plane 바이트 수(u64)다.
JSON은 기존 `mms_canvas_bgra_layers_v1` 스키마이며 file 필드는 진단용 이름이고
실제 파일을 읽지 않는다. 각 plane의 길이, SHA-256, opacity, stream key,
Z 순서, 불투명 base 검증은 파일 입력과 같은 Rust packet 파서를 사용한다.

JSON은 최대 64 KiB, 레이어는 1..8개, 크기는 짝수 최대 3840x2160이다.
plane당 최대 33,177,600바이트, 프레임당 최대 265,420,800바이트다.
Rust는 헤더 한도를 검사한 뒤 JSON을 할당하고, 검증된 레이어 크기만 순차로
할당한다. Python은 plane 복사 없이 memoryview로 partial write를 반복한다.
전송 중 한 프레임만 진행하며 OS pipe의 bounded buffer와 제출 ACK로 backpressure를 건다.
이는 GPU texture/pool 및 Qt 캡처 자체 메모리를 포함한 전체 프로세스 한도가 아니다.

`MMS_FRAME_ACCEPTED index`는 기존과 같이 CPU packet 검증, D3D11 texture upload,
GPU 제출 뒤 출력된다. NVENC 완료는 기존 frame pool/PTS clock이 별도로 검증한다.
ACK 이전에 다음 프레임을 요청하지 않는다. 정확한 프레임 수 뒤 stdin EOF로 종료하며
조기 EOF, 잘린 헤더/plane, 잘못된 버전/길이/순서, 초과 프레임은 실패한다.
NV12 GPU→CPU readback 경로는 추가하지 않는다.

Python의 입력 pump는 막힌 write 중에도 취소/시간 초과/작업자 종료를 감시한다.
실패 시 작업자를 terminate/kill 후 기다리고 pump와 출력 reader를 정리하며
source iterator를 닫는다. ACK 제한 시간에는 쓰기와 ACK 대기를 모두 포함한다.
기존 출력 거부와 `.h264.part` 정리를 유지한다.

최종 보고서의 `frame_transport_bytes`는 헤더·JSON·BGRA를 포함한 실제 Python 쓰기
바이트 수, `frame_transport_seconds`는 프레임 쓰기 시작부터 ACK까지의 누적 시간이다.
Rust `frame_receive_seconds`는 입력 대기·검증·해시를 포함한다.
`bgra_disk_bytes=0`은 binary 경로만 해당한다. 이 수치는 전체 내보내기 속도 향상의
증거가 아니며 같은 프로젝트의 준비부터 mux 종료까지 별도 A/B 측정해야 한다.

## CPU/GPU 유휴 상태 진단 (2026-11)

개발 CLI `--report`에 저장되는 `native_report`는 다음 **벽시계 경과 시간**을 포함한다.
이 타이밍은 CPU 사용률이나 GPU 점유율이 아니라, 병목 단계의 위치를 분리하기 위한 계측이다.

| 필드 | 설명 |
| --- | --- |
| `frame_production_seconds` | Qt/Python Canvas producer의 `next(frame)` 소요 시간 누적 |
| `frame_production_max_seconds` | 가장 느린 단일 프레임 준비 시간 |
| `frame_pipe_write_seconds` | Python thread 생성부터 pipe 쓰기/완료까지 누적 (Rust 입력·검증에 따른 backpressure 포함) |
| `frame_ack_wait_seconds` | pipe 쓰기 종료부터 `MMS_FRAME_ACCEPTED` ACK를 받기까지 누적 |
| `frame_transport_seconds` | 기존 write+ACK 총 소요 시간 (새 2개 카운터와 거의 같음) |
| `native_worker_finalize_seconds` | 모든 프레임 전송 후 NVENC 완료/작업자 종료/보고서 읽기까지 |
| `native_stream_elapsed_seconds` | native subprocess 시작부터 complete report 수집까지 벽시계 시간 |
| `native_stream_wall_fps` | `frames_encoded / native_stream_elapsed_seconds`, 사용자가 관찰하는 end-to-end 전체 FPS와 다름 |

Rust 자체 `frame_receive_seconds`는 입력을 읽고 검증하는 시간이고 `elapsed_seconds`는 native worker의 전체 처리 시간이다.
`audio_prepare_seconds`, `completed_wall_seconds`는 Python export 서비스의 측정값이다.
CLI에서 약 5초 분량의 영상 프레임이 완료될 때 출력하는 observed FPS는 **실제 경과 시간 기준**
수치이므로 프로젝트 FPS와 다를 수 있다.

진단 규칙: producer 시간이 대부분이면 단일 Qt/Python 래스터화 경로를 조사한다.
pipe write 시간이 대부분이면 BGRA 바이트 수·SHA-256·pipe backpressure·Rust packet validation을 조사한다.
ACK 대기가 길면 D3D11 upload/submit, NV12 slot availability, NVENC sync를 조사한다.
마지막 finalize 시간이 길면 encoder drain/flush를 조사한다.

현재 전송은 **프레임마다 Python source → write → ACK의 직렬화**를 유지한다.
이 보고서만으로 다중 프레임 prefetch나 worker parallelism이 안전하다고 결정하지 않는다.
Qt는 GUI thread 수명 제약이 있고, 무제한 frame buffering은 4K에서 과도한 메모리/취소 지연을 유발한다.
Windows 작업 관리자의 GPU 전체 사용률은 NVENC의 `Video Encode` 엔진 사용량과 구분해 확인한다.

**진행 중인 작업은 이 변경으로 계측되지 않는다.** 보고서 필드를 확인하려면 다음 실행 전
코드를 갱신해야 한다. 이미 실행 중인 Python/Rust 프로세스를 강제 재시작하지 않는다.

## 실험적 1프레임 선행 생성 (2026-11)

Rust worker의 `MMSB binary-v1` 형식, 순차 PTS, 순차 ACK와 NVENC 출력은 바꾸지 않고,
Python에서 **프레임 N을 Rust로 보내고 ACK를 기다리는 동안 Qt 소유 스레드가 프레임 N+1을 생성**할 수 있다.
실행 전 PowerShell에서 다음 환경변수로 명시적으로 활성화한다.

```powershell
$env:PLAYLIST_CANVAS_NATIVE_PIPELINED = "1"
# 동일한 명령으로 A/B 테스트한 뒤 기존 동작으로 복귀:
Remove-Item Env:PLAYLIST_CANVAS_NATIVE_PIPELINED
```

기본값은 기존 직렬 처리다. 실험 모드에서도 writer는 단 하나이며,
이전 프레임 ACK를 수신하고 난 뒤에 다음 프레임의 binary write를 시작한다.
Qt scene, 가사, 소스는 항상 **기존 호출 스레드**에서만 갱신한다. Qt 객체를 writer 스레드로 옮기지 않는다.
한 번에 최대 2개 native frame packet만 살아 있고, 메모리 사용량은 기존 1개보다 늘어날 수 있다.
4K 화면에서 각 BGRA packet 크기를 고려하고, 취소·에러가 나면 subprocess를 종료한 다음 writer를 join하고
원본 frame iterator를 닫는다. 프로토콜 순서와 원본/최종 MP4 검증은 그대로 유지한다.

새 보고서의 `frame_pipeline_enabled=true`, `max_outstanding_frame_packets=2`,
`timing_scope=one_frame_qt_production_overlaps_native_write_and_ack`로 실험을 식별한다.
이 모드에서는 producer와 binary pipe/ACK 시간이 동시에 진행되므로 세 단계 누적시간을 더해 전체 경과 시간과
비교하면 안 된다. 서로 다른 실험을 비교할 때는 동일 프로젝트·구간·해상도·FPS를 고정하고
`native_stream_wall_fps`와 `native_stream_elapsed_seconds`를 먼저 비교한다.

**주의:** 이 방식은 검증용 opt-in이다. 하드웨어에서 시간·화질·가사 타이밍·취소를 비교하기 전에는
기본 엔진으로 승격하거나 전체 장시간 내보내기에서 속도 향상을 보장한다고 표현하지 않는다.

## 실험: 투명 Canvas Z 밴드의 부분 렌더링

`PLAYLIST_CANVAS_NATIVE_PARTIAL_BANDS=1`을 설정하면, 네이티브 GPU 내보내기의
**두 번째 이상 투명 Z 밴드**를 `CanvasSnapshot`의 기존 부분 렌더링 방식으로 칠한다.
시각 요소가 차지하는 면적이 전체 캔버스의 90% 미만이고 위치/크기 keyframe 등의
불확실한 요소가 없을 때만 투명 영역 주변을 먼저 그린다. 좌표를 최종 출력 픽셀
그리드에 정렬한 뒤 동일한 크기의 투명 BGRA 프레임으로 다시 합성하므로
Rust `MMSB binary-v1` 형식, GPU 레이어 순서 및 알파 혼합은 바뀌지 않는다.
기본값은 **비활성화**다.

```powershell
$env:PLAYLIST_CANVAS_NATIVE_PIPELINED = "1"
$env:PLAYLIST_CANVAS_NATIVE_PARTIAL_BANDS = "1"
# 단일 30초 RTX 프로젝트 smoke를 기존 환경변수 OFF 결과와 비교
# 작업 완료 뒤 기본값 복귀
Remove-Item Env:PLAYLIST_CANVAS_NATIVE_PARTIAL_BANDS
Remove-Item Env:PLAYLIST_CANVAS_NATIVE_PIPELINED
```

진단 보고서 `native_report.raster_cache` 필드:
- `partial_band_enabled`: 새 부분 렌더링 옵션 사용 여부
- `partial_band_captures`: 실제로 크롭 후 확대한 투명 레이어 페인트 수
- `scene_render_source_pixels`: Qt가 실제로 칠하도록 요청받은 출력 픽셀 수의 합계(근사)
- `full_frame_source_pixels`: 동일 프레임을 모두 전체 영역으로 렌더링할 때의 픽셀 수

`scene_render_source_pixels < full_frame_source_pixels`라도 GPU 전송 바이트 수는
**그대로 유지**된다. 이 실험은 CPU/Qt 렌더링 부하를 줄이는 것이며 GPU input
payload 압축/축소는 후속 별도 변경 대상이다. 정확한 효과는 동일 프로젝트, 60FPS,
해상도, 시작 구간에서 FPS와 `frame_production_seconds`를 비교한다. 빠르지
않거나 픽셀 불일치가 관찰되면 옵션을 끄고 원래 경로로 되돌린다.
