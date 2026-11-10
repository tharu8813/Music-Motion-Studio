# t21 GPU 스트림·MP4 최종화 변경 검토

완성 MP4를 선택한 파일에 바로 덮어쓰던 GPU 경로를 변경했다. 같은 목적지
파일시스템의 임시 MP4에서 영상 CFR 프레임 수·FPS·크기·길이와 AAC 존재·길이를
확인한 뒤 최종 파일을 원자적으로 교체한다. 취소·검증 실패 시 기존 파일을 보존한다.
일반 FFmpeg 내보내기 방식과 영상 재인코딩 없는 GPU mux는 유지한다.

FFprobe의 취소 불가능한 `subprocess.run(timeout=180)`을 polling으로 교체했다.
취소 시 자식 프로세스를 kill/reap하고 stdout/stderr를 각각 최대 64 KiB만 보관한다.
H264 전체 프레임 검사는 유지하며 시간 제한은 `max(180초, 영상 길이 × 2)`이다.
이 값은 정책이며 실제 장시간 4K 디코드 성능을 측정한 결과가 아니다.
공용 MP4 메타데이터 검사도 선택적 `cancel_event`를 받아 취소 가능하다.

스트림 ACK 큐는 2개, ACK 입력 조각은 256자, stderr는 마지막 4096자로 제한한다.
stderr 파일 spool을 제거했다. 프레임 파일은 ACK 후 삭제하는 기존 한 프레임
backpressure를 유지한다. EOF·잘못된 ACK·worker crash·ACK timeout·취소에서
iterator를 닫고 프로세스를 종료한다. 기존 `.h264.part`와 출력의 심볼릭 링크도
덮어쓰지 않는다. Rust는 report 생성 경합에 실패했을 때 다른 프로세스가 만든
report를 삭제하지 않도록 생성 소유권을 확인한다.

`render_native_gpu_playlist`의 기존 `progress_callback(done, total)`은 유지한다.
새 선택적 `mux_progress_callback(stage, fraction, message)`를 finalizer로 전달한다.
최종 Complete/1.0은 검증과 파일 교체 뒤 전달한다. Juniper/Wren에 API를 공유했다.

## 실행한 검사

- `python -m unittest discover -s tests -p test_cancellable_subprocess.py`: 3개 통과.
  실제 자식 프로세스의 대량 양방향 출력, 취소 후 reap, timeout, 비정상 종료 검사.
- `python -m unittest discover -s tests -p test_export_validation_service.py`: 4개 통과.
- 수정 Python 파일 compileall 및 `git diff --check`: 통과.
- 기본 Python 3.15 및 Python 3.14 모두 PySide6가 없어 GPU 스트림·최종화·전체
  CLI 테스트는 import 단계에서 실행 불가. 해당 테스트에 ACK 오류/EOF/crash,
  timeout/취소, iterator 정리, stderr 폭주, mux 진행률 전달, 잘못된 CFR/AAC
  길이와 취소에서 기존 파일 보존 회귀 사례를 추가했다.
- `python scripts/rust_prerequisites.py`: rustc 실행 액세스 거부로 실패.
  Rust 빌드·실행, RTX 렌더링은 수행하지 않았다. 기존 20초 smoke는 반복하지 않았다.

## 개발 PC 재검사

재리뷰 요청 때 아래 명령을 다시 실행했다. 앞의 두 unittest 명령은 각각 3개,
4개 통과했다. 세 번째는 PySide6 import 오류 3건으로 종료했으며 제품 테스트가
실행되거나 통과한 것으로 계산하지 않는다. Rust 전제 검사는 액세스 거부로 실패했다.
compileall과 diff 검사는 종료 코드 0이었다.

```powershell
python -m unittest discover -s tests -p test_cancellable_subprocess.py
python -m unittest discover -s tests -p test_export_validation_service.py
python -m unittest tests.test_native_gpu_stream tests.test_native_gpu_production_finalizer tests.test_native_gpu_full_export
python scripts/rust_prerequisites.py
python -m compileall -q app/renderer/native_gpu_export.py app/renderer/native_gpu_stream.py app/renderer/ffmpeg_renderer.py app/services/export_policy.py app/services/export_validation_service.py app/utils/cancellable_subprocess.py tests/test_native_gpu_production_finalizer.py tests/test_native_gpu_stream.py tests/test_cancellable_subprocess.py tests/test_export_validation_service.py
git diff --check
```

저장소 의존성이 설치된 Python 환경에서 다음 PowerShell 명령을 실행한다.
전제 검사 실패 시 테스트 실행을 멈추고 안내된 setup 절차로 환경을 수정한다.

```powershell
python scripts/rust_prerequisites.py
if ($LASTEXITCODE -ne 0) { throw 'Rust/native prerequisites failed' }
$env:PLAYLIST_CANVAS_TEST_FFMPEG = (Get-Command ffmpeg.exe).Source
python -m unittest tests.test_cancellable_subprocess tests.test_export_validation_service tests.test_native_gpu_stream tests.test_native_gpu_production_finalizer tests.test_native_gpu_full_export
```

위 검사는 GPU 하드웨어를 사용하지 않는다. 실제 FFmpeg 통합 검사는 합성 H264와
AAC를 최종화해 30 CFR 프레임과 AAC 포함을 검사하며 GPU 성공을 주장하지 않는다.

## 실측하지 않은 한계

전체 프로젝트 GPU 렌더링, 메모리/디스크 실측, 시청·청취 품질 및 FFmpeg A/B 성능은
t23에서 확인해야 한다. H264 프레임 검사는 전체 디코드를 한 번 수행한다. 정확성을
대체할 검증 근거가 없어 이를 생략하지 않았다. MP4 검사는 메타데이터만 읽는다.
프레임 spool/진단 버퍼는 길이에 비례해 늘지 않지만 압축 H264·준비 오디오·최종
MP4 저장 공간은 영상 길이에 비례해 필요하다. 추가 staging은 복사 대신 rename한다.
임의 iterable의 `next()` 자체를 강제로 중단할 수는 없으므로 프레임 공급자는 취소와
시간 제한을 지켜야 한다. GUI bridge의 thread 소유권·취소는 t20의 계약이다.
직접 stream API를 사용하는 호출자는 private 출력 디렉터리를 소유/정리해야 한다.
제품 playlist wrapper는 임시 출력 디렉터리를 소유하며 실패 시 정리한다.

현재 worker에는 GitHub PR/merge 도구와 remote가 없다. 리드가 이 변경·검사 결과를
리뷰하고 PR/병합 절차를 진행해야 한다. 사용자 미추적 산출물은 수정하지 않았다.
