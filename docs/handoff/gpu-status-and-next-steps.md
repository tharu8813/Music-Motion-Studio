# GPU 작업 현황과 다음 실행 순서

2026-11-10, Wren t35. 기준은 `main b330419`이며 이 작업의 branch는
`agentcraft/wren/t35-main-gpu`이다. 다음 에이전트가 완료 범위를 오해하지 않고
작업을 재개하도록 작성한 기술 인수인계 자료다. 릴리즈 노트가 아니다.

## 먼저 유지할 경계

- 현재 목표는 진행 변경 보존과 인수인계다. 새 GPU 엔진 개발, 배포, 장시간 검증을 시작하지 않는다.
- Wlqor 승인 전 merge/push/publish/deploy 금지. 자기 worktree에서만 작업한다.
  이 환경에는 remote가 없으며 fetch/pull/push를 실행하지 않는다.
- 사용자 산출물 `native-stream-check-01.*`, `qt-giggles-second-song-v1/`를
  삭제하거나 덮어쓰지 않는다. 이번 worktree의 초기 `git status --short`는
  출력이 없었다. 이 파일들이 다른 작업 위치에 없다는 뜻은 아니다.
- **전체 GPU 엔진 완료, 전체 A/B 속도 향상, 설치본 검증 완료는 아직 주장할 수 없다.**
  작업 보드의 done/review와 제품 수용 조건 통과는 구분한다.

## 이번에 직접 확인한 것과 이전 보고

이번 t35에서 실행한 것은 `git status --short`, `git log -8 --oneline`,
`git show --stat 974e70d`, `git show --stat 4a0f222`, `git show --stat cb4c17e`,
`rg` 검색과 관련 소스/문서/JSON 읽기다. HEAD는 `b330419`이고 t27/t29 병합
기록과 해당 구현을 확인했다. `graphify-out/graph.json`은 없었다.
읽기 전용 조사 범위를 지키기 위해 그래프 생성/설치는 하지 않았다.

이번에는 Rust prerequisite, setup, 빌드, Python/Rust 테스트, GPU 실행,
설치본 실행, 시청·청취 또는 A/B를 **재실행하지 않았다**. 아래 실행 수치들은
명시된 기존 문서·커밋·공유 메모의 이전 보고이며 t35 신규 실측이 아니다.

| 상태/주장 | main 코드와 기존 근거 | 남은 확인/다음 순서 |
| --- | --- | --- |
| t27 binary 전송 반영 완료 | `974e70d` → `12b2c4b`. `native_gpu_stream.py`가 `--input-format binary-v1` 사용. `native_frame_transport.py`, Rust `canvas_stream.rs`, [전송 계약](../NATIVE_FRAME_TRANSPORT.md) | 새 Rust worker 재빌드 후 t30에서 캐시/취소/오류와 함께 회귀 확인. 전체 성능은 t32 |
| t29 소스·타임라인 호환성 보강 반영 완료 | `4a0f222` → `b330419`. `native_dynamic_motion.py`, `native_project_transitions.py`, `native_project_video.py`, 공통 `keyframe_render.py` 수정. [지원표/검증 계약](../architecture/t29-native-source-compatibility.md) | VIDEO/crossfade/AutoMix 전체 지원은 미완료. 실제 GPU 픽셀·A/V·긴 작업은 t32 |
| 기존 UI/최종화 연결은 main에 있음 | t22 `7bd65b0`/`0b7e3f8`, t23 `cb4c17e`/`8047535`. `export_controller.py`의 native_gpu 분기, `native_project_export.py`의 GUI frame source, `native_gpu_export.py` 최종화 | 연결 존재만으로 t30 완료 아님. t28까지 합쳐 최신 전송/캐시/지원 조건을 생명주기에 통합 검증 |
| t28 blocked, main에는 캐시 구현 없음 | 현재 main의 `native_project_frames.py`에 resident cache 연결 없음. 공유 메모 `shared/t28-resident-layer-cache-implementation-and-meas`는 Kit 별도 worktree의 미커밋 구현을 보고 | t34 보존 자료로 실제 diff/산출물 위치 확인·정상 인계부터. 임의 merge 금지 |
| t33 blocked, GPU affine/clip 미완료 | 공유 메모 `shared/t33-prerequisite-and-cache-handoff-blocker`: t33은 b330419 출발, t28 캐시 미인계. schema v2는 제안 | t28 보존/인계 및 프로토콜 합의 후 재개. affine/clip 구현·production 연결·픽셀 비교 별도 필요 |
| t30 → t31 → t32 미완료 | 작업 보드 t30 deps=t27/t28/t29, t31 deps=t30, t32 deps=t30/t31. 아래 단계별 수용 조건 참조 | 이 순서를 지키고 각 검증의 실제 실행 로그를 새로 남길 것 |

## 이전 실행 근거의 정확한 범위

1. **t27 Juniper 이전 보고**: 커밋 `974e70d` 설명 및 공유 메모
   `shared/t27-binary-frame-transport-review`에 Rust all-targets 33 pass,
   Python 관련 45개 실행/1 skip, RTX 5060 binary smoke 320×180/30fps/30프레임,
   6,925,560 bytes, write→ACK 누적 0.4030966초, BGRA disk 0,
   NV12 readbacks 0이라고 기록되어 있다. 64×64는 NVENC 초기화 STATUS 8로 거절됐다.
   smoke 원본 JSON의 정확한 경로는 이 커밋/메모에 명시되지 않아 이번에 확인하지
   못했다. 필요하면 Juniper에게 인계받는다. 전체 playlist/10분/A/B 근거가 아니다.
2. **t29 이전 실행 기록**: committed
   [t29-compatibility-tests.json](../architecture/benchmarks/t29-compatibility-tests.json)과
   지원 계약에 관련 12모듈/142개 실행, 1 skip, 약 24초가 기록되어 있다.
   production FFmpeg transform 출력과 Qt/native packet의 24 timestamp 비교이며
   source.scale=1.5, 비정방 소스, 이동/회전/크기/opacity, custom in/out,
   output scale 1/.5 등을 검사했다. bitwise 일치가 아니라 문서의 위치/색 허용오차
   검사다. 실제 D3D11/NVENC 전체 출력 비교를 대신하지 않는다.
3. **t28 Kit 이전 보고**: 별도 branch `agentcraft/kit/t28-gpu-cpu`의
   `work/t28-resident-production-02/comparison.json`에서 320×180/60프레임/3레이어,
   paints/uploads 180→62, allocations 180→3, hits 118, CPU retained 종료 0,
   CPU peak 460800 bytes, GPU input peak 691200 bytes, released 3으로 보고됐다.
   2.985초는 debug stream-only이며 전체 A/B가 아니다. 별도 128×128/60프레임
   resident/fresh NV12 byte parity(알파 변경/Z 재정렬 포함), uploads 180→4 보고도 있다.
   Python 56개/1 skip, Rust lib 29+stream 3 및 후속 cache ownership 검사 보고는
   main 검증 결과가 아니다. 원본 경로는 해당 Kit 작업트리 기준이며 t35에서
   다른 worktree를 직접 열어 검증하지 않았다. t34 문서에서 보존 위치를 확정한다.
4. **t33 Kit 이전 보고**: 해당 worktree `.venv`에서 setup/native 0.3.0과
   prerequisite 통과, Rust all-targets 33 pass(27 lib+6 bin). 이는 baseline
   검사이며 신규 GPU affine 수용 검사가 아니다. 캐시 인계와 프로토콜 합의가 blocker다.
   t34 최신 인계 보고의 문서는 `docs/handoff/gpu-cache-preservation.md`다
   (Kit branch에서 review, 이 branch에는 아직 병합되지 않음).
   Kit는 own-worktree-only 제약으로 원래 kit-t28의 미커밋 diff/status 확인이나
   보존 commit을 하지 못했다고 명시했다. metadata HEAD8047535만 확인했으며
   위 comparison.json 실파일도 미검증이다. 따라서 **보존 완료로 단정하지 말고
   원래 t28 작업에 재배정하여 diff/산출물 확보부터 해야 한다.**
5. **t23 이전 검증**: [t23-full-export-validation.md](../architecture/t23-full-export-validation.md).
   Rust access denied로 실제 RTX 전체 길이 실행은 하지 않았다. 최종 관련 집중 검사
   11모듈/201개/2 skip은 mock 기반이다. 전체 suite 당시 2,715개,
   failures=11/errors=23/skipped=90이며 수정 후 전체 재실행은 없었다.
   모든 실패가 baseline 문제라고 단정하지 않는다. 기존 20초 smoke도 재실행하지 않았다.

## 현재 지원과 남은 CPU 작업

production 경로는 `native_project_export.export_project_document` →
`native_project_frames`/`native_dynamic_layers` → `native_gpu_stream` →
`d3d11_qt_canvas_stream_export` → `native_gpu_export.finalize_native_gpu_mp4`다.
Qt Canvas 래스터화·동적 소스 페인팅·오디오 분석은 CPU에 남아 있다.
binary 전송은 프레임 BGRA/manifest 디스크 왕복을 없앴지만 CPU 메모리 전송과
업로드·해시 검증까지 없앤 것은 아니다. 캐시/GPU affine 완료로 읽지 않는다.

- t29의 production `dynamic-audio` 정책은 이미지/로고/워터마크, 텍스트/시간,
  도형/진행 막대/표지/배경, 가사/트랙 목록/현재 곡, visualizer/waveform/level meter/
  particle의 기존 painter/분석과 motion binding을 재사용한다.
- visible source 1..64, packet layer 최대 8, 짝수 64×64..3840×2160,
  scale .25..1, production FPS 1..120 조건. 하위 Rust CLI의 넓은 범위를
  앱 지원 범위로 확대 해석하지 않는다. 64×64는 validation 허용과 실제 NVENC
  성공을 구분해야 한다.
- VIDEO는 정확한 CFR decode/seek/repeat/speed/mask/filter 공급이 없어 거절한다.
  crossfade/AutoMix는 오디오와 프레임이 같은 transition plan을 공유하는 통합이
  없어 거절한다. helper의 경계 평가 검사는 전체 export 지원이 아니다.
- 미지원 소스를 누락하거나 검정/preview 프레임으로 GPU 성공을 만들지 않는다.
  프로젝트 전체 FFmpeg 경로와 사용자 안내를 유지하고 t30에서 재검증한다.
- Kit의 t28 보고상 static 변환/효과는 CPU에서 한 번 굽고 dynamic 페인팅은 CPU다.
  `inverse_transform`/`clip`, `mms_canvas_bgra_layers_v2`는 제안이며 합의/구현 완료가
  아니다. 기존 MMSB binary-v1 envelope 및 manifest CLI 호환도 보존해야 한다.

과거 [GPU_EXPORT_UI_VALIDATION.md](../GPU_EXPORT_UI_VALIDATION.md)는 t22 당시
환경 blocker와 제한을 기록한다. 이후 t29에서 일부 dynamic motion 제한이 해소됐다.
[rust-gpu-export-core.md](../architecture/rust-gpu-export-core.md)의 뒤쪽 manifest 전송,
UI 미연결/custom motion 미지원 설명도 역사적 기록이다. 현재 전송/지원 범위는
코드, t27 계약, t29 계약을 우선하며 과거 문서를 최신 검증 완료로 인용하지 않는다.

## Rust와 FFmpeg 전제

소스 실행·디버깅·Windows 빌드 호스트에는 **Rust MSVC rustc/cargo와 소스 버전에
맞는 `music_motion_native`**가 필수다. `native/export_analysis/Cargo.toml`의 버전과
필수 API를 `scripts/rust_prerequisites.py`가 확인한다. main 기준 이전 보고 버전은
0.3.0이다. 실패를 Python 경로로 조용히 우회하지 않는다.

반면 **PyInstaller 최종 프로그램은 사용자 PC에 Rust가 없어도 실행되어야 한다.**
`native_project_export.py`는 frozen 환경에서 개발 prerequisite를 생략하고
`_MEIPASS/native/` 또는 실행 파일 옆 `native/`의 worker를 찾는다.
`playlist_canvas.spec`의 prerequisite/worker 포함 검사는 빌드 호스트용이다.
이 코드가 있다고 실제 설치본 검증이 끝난 것은 아니다(t31).

FFmpeg/ffprobe는 유지한다. `native_project_export.py`의 AAC 오디오 준비와
`native_gpu_export.py`의 video stream-copy MP4 mux/완료 검증, 기존 엔진 fallback이
현재 필요하다. [README](../../README.md)의 MP4 export, 내보내기 품질 믹스 미리보기,
박자 기반 AutoMix 분석·미리듣기/EQ 미리듣기 조건도 유지한다. 기본 crossfade
미리보기는 FFmpeg 없이 가능하다. README상 FFmpeg는 설치 프로그램에 포함되지
않으므로 사용자 설정/외부 설치 조건을 제거하지 않는다.

## 다음 에이전트 실행 순서와 완료 판정

1. **t34 보존 확인 → t28/t33 재개 준비**: Kit의 보존 문서, diff, 산출물 경로와
   untracked 목록을 받는다. t28 미커밋 구현을 main에 있다고 가정하지 않는다.
   현재 diff/실파일은 미확보이므로 원래 t28 작업 재배정부터 조율한다.
   정상 인계와 필요한 승인을 거쳐 cache 구현/기존 binary 전송을 함께 보존한다.
   GPU affine/clip 프로토콜 합의, 실제 구현·production 연결·픽셀 동등성 근거가
   없으면 t28/t33 blocker를 해제하지 않는다.
2. **t30 통합**: 기존 UI/오케스트레이터를 확장해 최신 snapshot/GUI frame source,
   binary/cache, preflight, audio→encode→mux 진행률/총 wall timer, 취소/창 닫기,
   worker/thread/texture 종료, 오류 후 FFmpeg 재시도와 출력 충돌 방지를 검증한다.
   미지원 VIDEO/전환은 전체 내용이 FFmpeg에 보존되는지 확인한다.
   관련 mock 회귀와 짧은 실제 MP4 검사 결과를 구분해 남긴다.
3. **t31 설치본**: t30 후 release worker를 Python 코드와 함께 빌드·bundle한다.
   Rust 없는 대상 PC의 실제 설치본에서 worker 탐색, D3D11/NVENC/드라이버/DLL,
   FFmpeg 설정, export/취소/fallback을 확인한다. 지원 GPU 없는 경우도 오류/대체를
   기록한다. PyInstaller 빌드 성공이나 `Test-Path`만으로 설치본 PASS 금지.
4. **t32 최종 검증**: t30/t31 후 동일 PC/프로젝트/해상도/FPS/코덱/오디오 및
   동등 품질 조건을 명시해 준비 시작~최종 mux/게시 완료 wall time을 A/B 측정한다.
   native NVENC P3/HIGH_QUALITY와 FFmpeg CRF/preset은 자동 동등하지 않다.
   동등 품질을 확보하지 못하면 비용 수집으로만 표시하고 가속을 주장하지 않는다.
   전체 프레임 수/PTS/길이, 곡 경계/가사 단어·겹침/모션/Z/알파/색,
   실제 시청·청취/A/V sync, 10분 이상 및 전체 playlist, CPU/RSS/VRAM/디스크 peak,
   취소/장치 오류/정리를 확인한다. 못 한 항목은 명령·환경·blocker와 함께 미검증으로 남긴다.

## 재개 명령 (이번 작업에서 실행하지 않음)

자기 worktree 루트에서 사용한다. 기존 환경이 없을 때만 로컬 `.venv`를 만들고
해당 소스 의존성을 설치한다. 다른 작업트리 venv를 수정하지 않는다.
setup은 설치 작업이므로 read-only 조사를 마친 후 승인된 실행 범위에서 수행한다.
rustc access denied가 나면 그 실패를 기록하고 허용된 권한/환경을 확보한다.

```powershell
git status --short
git log -5 --oneline
# 필요 시 자기 worktree 로컬 환경 생성: python3.14 -m venv .venv
& ./.venv/Scripts/python.exe scripts/setup_rust_native.py
if ($LASTEXITCODE -ne 0) { throw 'Rust/native setup 실패' }
& ./.venv/Scripts/python.exe scripts/rust_prerequisites.py
if ($LASTEXITCODE -ne 0) { throw 'Rust/native prerequisite 실패' }
cargo test --manifest-path native/gpu_export_core/Cargo.toml --all-targets
if ($LASTEXITCODE -ne 0) { throw 'Rust 회귀 실패' }
& ./.venv/Scripts/python.exe scripts/run_tests.py native_frame_transport native_gpu_stream native_gpu_production_finalizer native_project_runtime native_dynamic_motion native_motion_production_parity native_project_ownership gpu_export_ui --report work/gpu-handoff-focused-tests.json
if ($LASTEXITCODE -ne 0) { throw 'Python 회귀 실패' }
cargo build --release --manifest-path native/gpu_export_core/Cargo.toml --bin d3d11_qt_canvas_stream_export
if ($LASTEXITCODE -ne 0) { throw '새 worker 빌드 실패' }
```

짧은 실제 export는 다음 형식이다. `<...>`는 실제 프로젝트와 **존재하지 않는 새
출력 경로**로 바꾼다. smoke는 전체 원본 프로젝트 검증이 아니다.

```powershell
& ./.venv/Scripts/python.exe -m tools.native_gpu_full_export --project '<실제 프로젝트.pvsproj>' --output '<새 경로/gpu-smoke.mp4>' --native-exe ./native/gpu_export_core/target/release/d3d11_qt_canvas_stream_export.exe --fps 60 --scale 0.5 --smoke-seconds 3
# t30/t31 준비 후 UI 수동 검사
& ./.venv/Scripts/python.exe main.py
# t31 빌드 호스트에서만
& ./.venv/Scripts/python.exe -m PyInstaller --noconfirm --clean playlist_canvas.spec
Test-Path 'dist/Music Motion Studio/_internal/native/d3d11_qt_canvas_stream_export.exe'
```

t32 프로파일/비교/ffprobe/전체 decode 명령은
[t23 재현 절차](../architecture/t23-full-export-validation.md)의
`tools/export_bottleneck_profile.py`, `tools/export_ab_verify.py`를 참고한다.
그 문서의 main 이동/git pull 명령은 이 worker 환경에서 실행하지 않는다.
`build/t23/*`는 제안된 향후 출력 경로이며 이번 실행 결과가 아니다.
새 측정은 자기 worktree의 새 `work/` 하위 디렉터리에 조건, 명령, commit,
profile JSON, GPU report, ffprobe JSON, 영상 및 청취/시청 판정을 함께 보존한다.
`native_gpu_stream.py`의 `complete.json`은 임시 staging 내 파일이므로 t30에서
보존 위치를 확인해 필요한 보고서를 정리 전에 확보해야 한다.

완료를 보고할 때는 각 항목에 `이번 직접 실행 / 이전 보고 / 미검증`을 붙이고
실제 근거 파일과 다음 행동을 적는다. t34와 이 자료를 병합한 최종 복사형
한국어 인수인계 프롬프트는 Juniper t36의 `docs/AGENT_HANDOFF.md`가 담당한다.
