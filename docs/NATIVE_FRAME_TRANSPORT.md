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
