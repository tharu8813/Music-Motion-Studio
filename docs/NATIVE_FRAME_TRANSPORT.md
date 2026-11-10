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
