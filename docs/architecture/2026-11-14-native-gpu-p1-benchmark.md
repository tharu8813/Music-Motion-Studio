# P1: Rust GPU A-B-B-A 측정 및 전체 출력 무결성 검사

이 도구는 **개발자용**, **동일 커밋/동일 Rust 실행 파일/동일 프로젝트**에서
30초 1080p60 클립 4개를 생성하는 자동 비교 검사다.
기존 GUI 내보내기의 Rust GPU 최적화 선택 옵션을 새로 만들지 않는다.
엔진의 실제 렌더링 동작은 변경하지 않는다.

## 실행 조건

- 현재 `main`을 업데이트한 뒤 추적 파일 변경 사항이 없는 작업 트리
- Windows 11, NVIDIA NVENC 지원 GPU, 해당 커밋으로 빌드한
  `native/gpu_export_core/target/release/d3d11_qt_canvas_stream_export.exe`
- 개발 환경의 Python/PySide6/Rust 확장 모듈, ffmpeg.exe, ffprobe.exe
- 첫 번째 활성 음악이 30초 이상인, 서로 다른 **대표 프로젝트 2종**.
  권장: (1) 정적+TTML+텍스트+가사+키프레임, (2) 파티클/반응형
  오버레이/비디오+복합 Z 레이어. GPU 경로에서 지원하지 않는 소스는
  작업을 실패시켜야 하며, 이를 빈 결과로 대신하지 않는다.
- 프로젝트 Canvas 크기에 맞춰 **출력 1920×1080**이 되는 scale 사용.
  예: Canvas 1280×720이면 `--scale 1.5`.

## 명령

```powershell
git switch main
git pull --ff-only
python scripts/rust_prerequisites.py
cargo build --release --manifest-path native/gpu_export_core/Cargo.toml --bin d3d11_qt_canvas_stream_export

python -m tools.native_gpu_p1_benchmark `
  --project "C:\\path\\to\\project1.pvsproj" `
  --native-exe ".\\native\\gpu_export_core\\target\\release\\d3d11_qt_canvas_stream_export.exe" `
  --scale 1.5 `
  --output-dir "build/p1-case1-20261114" `
  --ffmpeg "C:\\path\\to\\ffmpeg.exe" `
  --ffprobe "C:\\path\\to\\ffprobe.exe"
```

두 번째 프로젝트도 **새 출력 폴더**로 반복한다.

## 검증 방식

- A1/B1/B2/A2 각 실행은 `tools.native_gpu_full_export`로 첫 음악을
  정확히 30초로 잘라 **1800프레임과 오디오가 포함된 실제 MP4**를 만든다.
  **전체 원본 프로젝트 내보내기는 아니다.**
- A: 자동 프로필에서 공유 패킷 전송과 병렬 패킷 검증만 끈 기준.
  B: 앱 기본 `automatic` 설정.
- 출력 디렉터리가 이미 있으면 거절하여 이전 비교 기록을 보호.
- 4개 report의 프로젝트 SHA-256, 완료 상태, 인코딩 1800프레임과
  자동 프로필을 검증.
- `ffprobe -count_frames`로 출력 해상도/60fps/1800프레임/오디오/시간을
  확인하고 **모든 영상 프레임의 PTS 순서**를 해시로 비교.
- `ffmpeg -f hash`로 **영상 전체를 yuv420p 디코딩**한 SHA-256과
  **오디오 전체를 48kHz 2채널 PCM으로 디코딩**한 SHA-256 비교.
  RAM에 30초 분량의 비압축 프레임 배열을 적재하지 않는다.
- `p1-summary.json`은 A/B 평균 CLI 전체 시간과 단계별 시간, 결과 동일성을
  저장한다. 상태가 다른 콘텐츠 또는 PTS의 차이가 하나라도 있으면 FAIL.
  불완전한 실행은 `p1-error.json`에 기록되고 PASS로 표시하지 않는다.

Qt 캡처, CPU 단계, Rust 전송, NVENC 타이머는 중첩·병렬이므로
**단계 시간을 더한 값이 전체 내보내기 시간이라는 가정은 금지**한다.
`--gpu-profile-qt-paint`는 계측 오버헤드가 있어 이 A-B-B-A와
섞어 성능 향상률을 주장하면 안 된다. 텍스트 세부 계측은
[Qt paint 프로필](2026-11-12-native-qt-text-stage-profile.md)의
별도 진단에서 진행한다.

실제 GPU에서 두 프로젝트의 4회 실행 결과가 모두 완료되기 전에는
성능 개선이나 P1 완료를 주장하지 않는다.
