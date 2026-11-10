# 전체 프로젝트 GPU 런타임 계약

`app.renderer.native_project_export.export_project_document`는 `ProjectDocument`를
복사해 전체 타임라인을 내보내고 기존 `RenderResult`를 반환한다.
CLI `python -m tools.native_gpu_full_export`도 같은 구현을 사용한다.

## UI / worker

1. GUI에서 `GuiProjectFrameSource(project, fps=fps, scale=scale, cancel_event=cancel)`를 만든다.
2. worker에서 `export_project_document(project, output, fps=fps, scale=scale,
   frame_source=source, cancel_event=cancel, progress_callback=on_frames)`를 호출한다.
   프로젝트·설정·취소 Event는 일치해야 한다. `on_frames(done, total)`은 worker에서 호출된다.
3. 서비스가 worker에서 오디오 준비와 분석을 수행한다. Qt 장면은 생성하지 않는다.
4. worker의 `next(source)`마다 GUI가 프레임 하나를 만든다. 전체 프레임을 모으지 않는다.
   전달되는 것은 BGRA 바이트 패킷이며 GUI 소유 Qt 객체가 아니다.
   오디오 버퍼는 기존 PythonVisualizer의 전체 오디오/envelope 보관 방식을 따른다.
5. 취소는 같은 Event를 설정한다. worker는 대기 중 50ms 간격으로 취소를 확인하며,
   GUI 요청은 기본 90초 뒤 실패한다. 서비스는 종료 시 source를 닫는다.
   worker의 `close()`는 GUI에 정리를 예약하므로 `closed` 신호까지 source를 보존한다.
   GUI에서 worker를 blocking join하지 말고 이벤트 루프를 계속 처리해야 한다.

`produce_complete_project_frames`를 직접 쓰는 CLI는 QApplication GUI 스레드에서
생산 iterator를 소비하고 닫는다. 장면 생성 실패·취소·조기 종료에도 장면과 동적
iterator를 정리한다. 프로젝트 원본은 변경하지 않는다.

## 지원 검사와 실행 파일

전환은 `none`만 지원한다. 비디오, 미지원 음악 반응 소스, 동적 레이어 사용자 지정
키프레임/회전, 정적/동적 동일 Z, 패킷 레이어 수 초과, 없는 이미지, 잘못된 해상도는
생산 전에 오류로 처리한다. 정적 레이어만 있는 프로젝트도 지원한다.
미지원 기능을 생략하지 않으며 기존 출력 파일을 덮어쓰지 않는다.

개발 기본 경로: `native/gpu_export_core/target/release/d3d11_qt_canvas_stream_export.exe`.
패키징 앱은 `_MEIPASS/native/`와 앱 실행 파일 옆 `native/`에서 같은 이름을 찾는다.
패키징 시 이 실행 파일을 `native/`에 포함해야 한다. spec 연결은 UI 통합 단계다.
명시적 `native_exe`와 CLI `--native-exe`도 유지한다. PATH로 대체하지 않는다.

소스 실행/빌드 호스트는 Rust MSVC와 소스 버전에 맞는 확장 모듈이 필수다.
CLI 진입점은 `scripts.rust_prerequisites`를 검사하며 기존 UI 시작 검사도 유지한다.
최종 패키징 앱에는 Rust 설치가 필요하지 않다.

## 검증

```powershell
python scripts/rust_prerequisites.py
python scripts/run_tests.py native_project_runtime native_gpu_full_export native_canvas_dynamic_overlays native_canvas_project_probe native_gpu_stream native_gpu_export
```

환경 준비가 필요하면 `python scripts/setup_rust_native.py`를 먼저 실행한다.
새 `test_native_project_runtime`은 native/디코딩/프레임 캡처를 mock한 계약 검사다.
실제 전체 프로젝트 GPU 출력의 시청/청취 품질 검증과 구분한다.
기존 20초 RTX smoke는 반복하지 않는다.

### t20 실제 검사 결과 (2026-11-10)

이 변경을 **Tests PASS로 표시하지 않는다.** 새 계약 테스트 메서드는 11개이며,
실행되지 않았으므로 GUI bridge의 취소·timeout·GUI 정리 동작은 아직 검증되지 않았다.
이전 작업 보고에서 12개라고 적은 수는 11개로 정정한다.

| 검사 | 결과 | 근거 |
| --- | --- | --- |
| 변경된 Python 파일 8개 compileall | 통과 | 구문 컴파일 완료, 종료 코드 0 |
| 앱 런타임 모듈 5개 tools import AST 검사 | 통과 | tools에서 가져오는 ImportFrom 없음 |
| git diff 7c11374 HEAD --check | 통과 | 새 파일 포함 검사; 최초 발견한 EOF 빈 줄 3곳 수정 후 확인 |
| Rust 개발 환경 사전 점검 | 차단 | rustc --version 접근 거부, os error 5 |
| scripts/run_tests.py 관련 모듈 실행 | 차단 | Rust 사전 점검에서 종료, 테스트 미실행 |
| 격리된 tests.test_native_project_runtime 실행 | 차단 | PySide6 import 실패, 테스트 본문 미실행 |
| Python 3.14 대체 interpreter 확인 | 차단 | PySide6 미설치, Rust 점검도 같은 접근 거부 |

기본 interpreter는 Python 3.15이며 저장소의 지정 버전은 3.14.10이다.
확인된 Python 3.14 interpreter에도 PySide6가 없다. 환경이 준비되면 위 PowerShell
명령으로 다시 검증해야 한다. native 실제 실행·RTX smoke·전체 출력 품질 검증은
이번 검사 결과에 포함하지 않는다. Rust/PySide6 조건을 우회해 실행하지 않았다.
