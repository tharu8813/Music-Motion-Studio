# GPU 내보내기 UI 검증

## t22 검사 상태 (재리뷰)

| 검사 | 결과 | 근거 |
| --- | --- | --- |
| Static: 변경 Python/spec 문법, `git diff --check` | PASS | 명령 종료 코드 0 |
| 파일 게시 메서드 단독 파일 시스템 검사 | PASS | 충돌 차단·새 파일 게시·기존 교체 동작 확인 |
| Qt 집중 suite (7개 모듈) | BLOCKED | Python 3.15의 `PySide6` import 실패; 테스트 본문 실행 0건 |
| Rust 개발 전제 검사 | BLOCKED | `rustc --version` 액세스 거부 |
| 설치본 빌드·실제 RTX 전체 길이·시청/청취 | BLOCKED | Qt/Rust 개발 환경 확보 후 실행 필요 |

`Tests ((none)): PASS` 같은 수집 도구 표시는 실제 Qt 테스트 통과를 뜻하지 않는다.
`export_video` 진입, 새 엔진 옵션, snapshot, Qt 스레드 종료, fallback 경로의
새 회귀 테스트는 작성됐지만 아직 실행되지 않았다. 작업 트리에는 `.venv`/`venv`가
없고 `python`/`python3` 모두 PySide6 없는 동일 Python 3.15를 가리킨다.
사용 가능한 Qt 인터프리터를 확보하면 아래 집중 suite를 반드시 실행해야 한다.

내보내기 설정의 `Render engine`에서 `Rust GPU · D3D11 + NVENC (Beta)`를 선택한다.
기본 엔진은 계속 FFmpeg이며, 저장된 NVENC 인코더 설정만으로 Rust 엔진이 활성화되지 않는다.
Rust 엔진은 Windows D3D11/NVIDIA NVENC에서 MP4/H.264를 만든다. Qt가 Canvas 입력을
그리며, 오디오 준비와 mux는 FFmpeg를 사용한다. FFmpeg CRF/preset은 Rust 인코더에
적용되지 않는다. 오디오 비트레이트, 출력 해상도와 FPS는 선택한 값을 사용한다.

현재 지원 조건은 Canvas 비율 유지, Scale 0.25–1, 짝수 해상도
64×64–3840×2160, 1–120 FPS, 전환 없음이다. 영상 요소, 미지원 오디오 반응,
일부 동적 요소의 회전·사용자 키프레임 등은 런타임 사전 검사에서 거절한다.
거절 이유를 표시한 뒤 기존 FFmpeg 경로로 계속한다. 부분 렌더로 대체하지 않는다.

## 개발 호스트 검사

소스와 일치하는 Rust MSVC/네이티브 확장이 설치된 프로젝트 환경에서 실행한다.
아래 `.venv`는 해당 호스트의 프로젝트 가상 환경이다.

```powershell
.\.venv\Scripts\python.exe scripts/rust_prerequisites.py
cargo build --release --manifest-path native/gpu_export_core/Cargo.toml --bin d3d11_qt_canvas_stream_export
.\.venv\Scripts\python.exe -m unittest tests.test_gpu_export_ui tests.test_native_project_runtime tests.test_native_gpu_stream tests.test_native_gpu_production_finalizer tests.test_main_window_export tests.test_export_audio_staging tests.test_export_total_wall_timer
```

GPU 없는 회귀 테스트는 GPU/FFmpeg 실행만 모킹하며 컨트롤러·worker 분기와
GUI close 신호의 수명 계약을 검사한다. 실제 GPU 품질·성능 검증을 대신하지 않는다.

## RTX에서 실제 UI 검사

```powershell
.\.venv\Scripts\python.exe main.py
```

1. `giggles.pvsproj`를 열고 이미 통과한 20초 smoke 대신 전체 길이를 새 MP4로
   내보낸다. Rust GPU 엔진, 60 FPS, Canvas 원본 해상도를 선택한다.
2. 오디오 준비, 프레임 인코딩, mux 중 UI 응답과 ETA를 확인한다. 각 단계에서
   취소와 창 닫기를 각각 검사한다. 작업 종료 전 창/worker를 강제 삭제하면 안 된다.
3. 지원하지 않는 영상 요소 또는 전환이 있는 프로젝트는 시작 전 이유를 보여 주고
   FFmpeg로 전체 내용을 보존해 내보내야 한다.
4. GPU 작업 중 오류는 작업과 GUI 소스가 모두 닫힌 다음 FFmpeg 재시도를 묻는다.
   취소는 재시도를 묻지 않는다. 출력이 이미 생겼으면 재시도를 차단한다.
5. 같은 프로젝트를 FFmpeg로 새 파일에 내보낸다. 전체 프레임 수·길이·오디오 싱크,
   곡 경계·가사·동적 요소·색상·겹침 순서를 비교한다. 최종 시청·청취가 필요하며
   메타데이터 검증 통과만으로 영상·음성 품질을 통과 처리하지 않는다.

## 패키징

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean playlist_canvas.spec
Test-Path 'dist\Music Motion Studio\_internal\native\d3d11_qt_canvas_stream_export.exe'
```

spec은 native 실행 파일이 없으면 빌드를 중단한다. 설치 프로그램은 기존대로
dist 폴더 전체를 포함한다. 배포 앱의 native 탐색은 `_MEIPASS/native/`와
실행 파일 옆 `native/`를 사용하며, 최종 사용자 PC에 Rust를 요구하지 않는다.

t22 작업 환경에서는 Python 3.15에 PySide6가 없어 Qt 테스트 수집이 실패했고,
Rust 점검은 `rustc` 실행 액세스 거부로 실패했다. 문법 검사와 `git diff --check`,
FFmpeg 파일 게시 메서드의 단독 파일 시스템 검사(새 파일, 충돌 차단, 기존 덮어쓰기)는
통과했다. 설치본 빌드·RTX 전체 길이 내보내기·시청/청취 결과는 아직 확인하지 않았다.
