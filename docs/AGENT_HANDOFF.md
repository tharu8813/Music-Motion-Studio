# 다음 에이전트에게 복사할 GPU 작업 인수인계 프롬프트

> 후속 작업 현황: t28 원본은 `b4f052c`로 보존했고, 현재 별도 `codex/gpu-export-completion` 브랜치에 통합했습니다. v2 계약·GPU affine 구현은 [현행 계약](architecture/native-affine-v2.md)을 참고하세요. 아래 t36 기준 상태·remote 없음·v2 제안 설명은 역사적 기록이며 현재 완료 판정은 후속 검증 보고를 따릅니다. main 병합·배포는 수행하지 않았습니다.

아래 내용을 다음 에이전트의 첫 프롬프트로 그대로 전달하세요. 팀 메모리나 작업보드 접근 없이도 이 파일과 연결 문서로 시작할 수 있도록 작성했습니다. 경로와 상태는 2026-11-10 확인 기준이며, 실행 전에 현재 상태를 다시 확인하세요.

---

당신은 Music Motion Studio의 GPU 내보내기 작업을 이어받는 개발 에이전트입니다. 목적은 기존 작업과 사용자 산출물을 안전하게 보존하고, 미완료 조건을 검증하며 순서대로 마무리하는 것입니다. **GPU 엔진 전체 완료, 전체 내보내기 성능 향상, schema v2 구현 완료를 전제로 삼지 마세요.** 이번 인수인계 작성 자체는 새 GPU 구현이나 배포를 승인하지 않습니다.

## 1. 저장소와 기준점

- 저장소 main worktree: `C:/Users/wlqor/Desktop/playlist_project`
- 이 문서를 작성한 worktree: `C:/Users/wlqor/.agentcraft/codex/worktrees/playlist-project/juniper-t36`
- 작성 branch: `agentcraft/juniper/t36-md`
- 작성 직전 이 branch와 main의 HEAD: **`1fcfdedd69aca743b4439fd375351dd754a14c4f`** (`1fcfded`). 이 문서 자신의 후속 commit/merge는 이 SHA에 포함되지 않습니다.
- 구현 기준은 t27/t29가 병합된 `b3304196dea1a4581a0be799184a84287b54802e`입니다. 그 뒤 t34 문서 `94ef63f`가 `9fdfc69`로, t35 문서 `0020dc5`가 `1fcfded`로 승인 병합되었습니다. merge commit 메시지에는 각각 승인 d127/d131이 기록되어 있습니다.
- 이 환경에는 remote가 없습니다. `fetch/pull/push`를 실행하지 마세요. 담당 branch/worktree 안에서만 작업하고 다른 worktree로 이동하거나 다른 branch로 checkout하지 마세요. 최신 main 반영이 필요한 경우에도 먼저 미커밋 변경 보존과 부여된 승인 범위를 확인하세요.
- 루트 [AGENTS.md](../AGENTS.md) 및 작업 경로의 추가 지침을 읽고 따르세요. 이번 작업은 릴리즈 노트 작성이 아닙니다. 릴리즈 노트 변경, GitHub Release 게시·수정, 태그·첨부 변경, 배포를 하지 마세요.
- 기존 코드 그래프는 **`7cde4ca8` 기준의 오래된 자료**입니다. 현재 코드와 일치한다고 가정하지 말고 `rg`, 실제 파일, commit diff로 재확인하세요. 그래프가 로컬에 없어도 진행할 수 있습니다.

## 2. 완료와 보류를 구분하세요

작업보드의 done은 모든 제품 수용 조건이 검증되었다는 뜻이 아닙니다.

| 작업 | 확인된 상태 | 남은 조건 |
| --- | --- | --- |
| t20~t22 | 앱 런타임 프레임 생산, 스트림·최종화, UI/FFmpeg 대체 경로의 기존 구현이 main에 있음 | 최신 binary/cache와 결합된 생명주기 검증은 t30 |
| t23 | 코드·집중 테스트 및 검증 문서가 main에 있음. 보드상 done | 실제 RTX 전체 길이 실행·전체 A/B는 미완료. 전체 suite 실패도 아래 참조 |
| t27 | binary 전송 병합 완료: `974e70d`, merge `12b2c4b` | binary 전송이 CPU raster 제거나 전체 속도 향상을 뜻하지 않음 |
| t29 | 타임라인·소스 호환성 보강 병합 완료: `4a0f222`, merge `b330419` | VIDEO/crossfade/AutoMix 전체 지원 및 실제 GPU 전체 출력 비교는 미완료 |
| t28 | **blocked**. 별도 worktree의 미커밋 resident cache 구현이 이전에 보고됨 | 실제 diff/untracked 확보와 보존 commit부터 필요. main에는 cache 구현 없음 |
| t33 | **blocked**. GPU arbitrary affine/clip shader 미구현, v2는 제안 | t28 인계, 계약 합의, 구현·capture 연결·픽셀 검증 필요 |
| t30 | 미착수 | t27/t28/t29 결합 후 앱 내보내기 생명주기 통합 |
| t31 | 미착수 | t30 뒤 Windows 설치본 bundle 및 실제 실행 검증 |
| t32 | 미착수 | t30/t31 뒤 기존 엔진 대비 전체 품질·속도 검증 |
| t34/t35 | 보존 blocker와 main 현황을 정리한 문서 승인 병합 완료 | t34 완료는 t28 미커밋 구현의 보존 완료를 뜻하지 않음 |

작업보드에 남은 다른 저장소의 AgentCraft UI 현지화 항목(t5~t12 등)은 이번 playlist-project GPU 인수인계와 별도 범위입니다. 이를 GPU 완료 조건이나 새 작업 지시로 해석하지 마세요.

## 3. 반드시 보존할 위치

다음 SHA는 등록된 ref/메타데이터 기준입니다. **worktree가 clean이라는 증거가 아닙니다.** 원래 작업으로 정상 재배정된 담당자만 해당 worktree를 검사·수정하세요.

| 대상 | branch / 보존 commit | worktree |
| --- | --- | --- |
| t28 원본 | `agentcraft/kit/t28-gpu-cpu`, HEAD `80475358080a7c5a3946a64491f45475e3b4c277`. **cache 보존 commit 미확보** | `C:/Users/wlqor/.agentcraft/codex/worktrees/playlist-project/kit-t28` |
| t33 baseline | `agentcraft/kit/t33-t28-gpu-affine`, HEAD `b3304196dea1a4581a0be799184a84287b54802e` | `C:/Users/wlqor/.agentcraft/codex/worktrees/playlist-project/kit-t33` |
| t34 근거 | `agentcraft/kit/t34-t28-t33`, 문서 commit `94ef63f`, main merge `9fdfc69` | 작성 당시 `C:/Users/wlqor/.agentcraft/codex/worktrees/playlist-project/kit-t34` (현재 등록 여부 재확인) |
| t35 근거 | `agentcraft/wren/t35-main-gpu`, 문서 commit `0020dc5`, main merge `1fcfded` | 현재 등록 여부는 `git worktree list`로 확인 |
| t36 프롬프트 | `agentcraft/juniper/t36-md`, 작성 base `1fcfded` | 위 juniper-t36 경로 |

t28의 기존 ref만 전달하면 미커밋 구현은 전달되지 않습니다. 리드의 전달 사항에 따르면 리드 sandbox에서 kit-t28 `status` 확인은 **dubious ownership으로 실패**했습니다. t36이 직접 재현한 결과는 아닙니다. 실패를 clean으로 해석하거나 global Git 설정(`safe.directory` 등)을 바꿔 우회하지 마세요. 정상 재배정/허용된 접근이 필요하면 blocker로 보고하세요.

main의 untracked 사용자 산출물 `native-stream-check-01.*`, `qt-giggles-second-song-v1/` 및 그 밖의 프로젝트·영상·측정 파일을 삭제·덮어쓰지 마세요. 이 문서 작성 branch의 초기 status가 비어 있었다는 사실은 main이나 kit-t28의 상태를 보증하지 않습니다. `git clean`, 강제 reset, 일괄 stage, worktree 제거를 하지 마세요. 기존 산출물과 겹치지 않는 새 출력 경로를 사용하세요.

## 4. 먼저 읽을 근거와 코드

1. [t28 보존 및 t33 blocker](handoff/gpu-cache-preservation.md): 보존 절차, 충돌 예상 지점, 이전 실측의 한계.
2. [main GPU 현황과 다음 단계](handoff/gpu-status-and-next-steps.md): 구현 범위, 실행 명령 예시, t30~t32 수용 조건. 이 문서의 base는 b330419이며 t34 미병합이라는 당시 서술은 현재 main에서는 해소되었습니다. 구현 보존 blocker는 여전히 남습니다.
3. [binary 전송 계약](NATIVE_FRAME_TRANSPORT.md), [t29 지원/검증 계약](architecture/t29-native-source-compatibility.md), [t29 테스트 원본 JSON](architecture/benchmarks/t29-compatibility-tests.json).
4. [t23 전체 검증 기록](architecture/t23-full-export-validation.md), [기존 UI 검증 기록](GPU_EXPORT_UI_VALIDATION.md), [README](../README.md). 과거 manifest/UI 미연결 제한은 현재 코드·t27/t29 문서와 대조하세요.

현재 production 흐름은 `app/renderer/native_project_export.py` → `native_project_frames.py`/`native_dynamic_layers.py` → `native_gpu_stream.py` → `native/gpu_export_core/src/bin/d3d11_qt_canvas_stream_export.rs` → `native_gpu_export.py` 최종화입니다. UI 연결은 `app/controllers/export_controller.py`에서 확인하세요. Qt raster, dynamic paint, 오디오 분석·준비 등 CPU 작업이 남아 있습니다.

t28 이전 기록의 새 파일은 `app/renderer/native_layer_cache.py`, `native/gpu_export_core/src/resident_layer_cache.rs`, `docs/architecture/t28-resident-layer-cache.md`입니다. **현재 main에는 없으며 존재/실제 내용은 원본 담당자가 확인해야 합니다.** t27 stream exporter와 t29 native_project_frames 변경이 겹치므로 양쪽 기능을 보존하며 검토하세요.

## 5. 첫 확인 명령

아래는 **재개 지침이며 이번 t36 실행·성공 기록이 아닙니다.** 자신에게 배정된 worktree 루트의 PowerShell에서 시작하세요. main이나 타인 worktree를 대상으로 명령을 우회 실행하지 마세요.

```powershell
Get-Location
git branch --show-current
git rev-parse HEAD
git rev-parse main
git status --short
git diff --stat
git ls-files --others --exclude-standard
git log -8 --oneline
git worktree list
Get-Content -Encoding utf8 AGENTS.md
Get-Content -Encoding utf8 docs/AGENT_HANDOFF.md
Get-Content -Encoding utf8 docs/handoff/gpu-cache-preservation.md
Get-Content -Encoding utf8 docs/handoff/gpu-status-and-next-steps.md
rg --files app | rg 'export_controller|native_project_frames|native_gpu_stream'
```

출력과 문서가 다르면 현재 상태를 우선하여 차이를 기록하세요. t28 원본 담당자는 같은 확인 뒤 `git diff`로 전체 변경을 검토하고, 사용자 산출물을 제외한 구현·테스트·문서만 개별 stage하여 `git diff --cached`를 확인한 다음 보존 commit을 만드세요. 보존 SHA, 포함/제외 파일, 측정 원본 위치, 실패/skip를 문서에 남기세요. 허용된 자기 branch 보존 commit과 main 통합 승인은 별개입니다.

## 6. Rust 환경과 재검증

소스 실행·디버깅·Windows 설치본 빌드 호스트에는 **Rust MSVC rustc/cargo와 소스 버전에 맞는 `music_motion_native`**가 필수입니다. 해당 worktree의 올바른 Python 환경을 선택한 뒤 아래 순서로 설치/점검하세요. 기존 타인 worktree의 venv는 변경하지 마세요.

```powershell
python scripts/setup_rust_native.py
if ($LASTEXITCODE -ne 0) { throw 'Rust/native setup 실패' }
python scripts/rust_prerequisites.py
if ($LASTEXITCODE -ne 0) { throw 'Rust/native prerequisite 실패' }
cargo test --manifest-path native/gpu_export_core/Cargo.toml --all-targets
if ($LASTEXITCODE -ne 0) { throw 'Rust 테스트 실패' }
python scripts/run_tests.py native_frame_transport native_gpu_stream native_gpu_production_finalizer native_project_runtime native_dynamic_motion native_motion_production_parity native_project_ownership gpu_export_ui --report work/gpu-handoff-focused-tests.json
if ($LASTEXITCODE -ne 0) { throw 'Python 테스트 실패' }
```

기존 report가 있으면 새 이름을 선택하세요. 필요한 의존성·권한이 없으면 해당 실패를 기록하고 허용된 환경을 확보하세요. Rust 부재나 native 불일치를 Python fallback으로 숨겨 개발/패키징을 진행하지 마세요. 위 집중 테스트 외에도 실제 보존 diff에 맞는 cache/affine 테스트가 필요합니다.

**PyInstaller 최종 프로그램 사용자 PC에는 Rust가 없어도 실행되어야 합니다.** 이 구분을 t31에서 실제 설치본으로 검증하세요. FFmpeg/ffprobe는 오디오 준비, MP4 mux/검증 및 기존 엔진 대체 경로 등에 여전히 필요하며 제거하지 마세요.

## 7. 실제 결과와 미검증 범위

아래는 연결 문서에 보존된 **이전 실행 보고**입니다. t36에서는 Git 메타데이터, 문서, 경로와 명령을 검토했으며 Rust 설치·테스트·GPU 실행·성능 측정을 재실행하지 않았습니다.

| 근거 | 이전 결과 | 해석 한계 |
| --- | --- | --- |
| t27 commit/기존 보고 | Rust all-targets 33 PASS, Python 관련 45개 실행/1 SKIP. RTX 5060 binary smoke 320×180/30fps/30 frames, 6,925,560 bytes, write→ACK 누적 0.4030966초, BGRA disk 0, NV12 readbacks 0 | smoke 원본 JSON 정확한 경로 미확보. 64×64는 NVENC STATUS 8로 거절됨. 전체 playlist/10분 A/B 아님 |
| t29 committed JSON | 12모듈/142개 실행, 1 SKIP, 약 24초. production FFmpeg transform과 Qt/native packet 24 timestamp 비교 | 허용오차 기반 검사이며 bitwise 일치나 실제 D3D11/NVENC 전체 영상 비교 아님 |
| t28 원본 worktree의 `work/t28-resident-production-02/comparison.json` 보고 | 320×180/60 frames/3 layers, paints/uploads 180→62, allocations 180→3, hits 118. CPU end retained 0/peak 460800 bytes, GPU input peak 691200 bytes, released 3 | debug stream-only 2.985초. 파일 현재 존재/내용과 전체 CLI 미확보. 전체 export 속도 향상 근거 아님 |
| t28 별도 resident/fresh 보고 | 128×128/60 frames NV12 byte parity, alpha 변경/Z reorder 포함, uploads 180→4, allocations 180→3. Python 56 PASS/1 SKIP, Rust lib 29+stream 3 등 보고 | 최종 총수 임의 합산 금지. main 통합 후 검증 아님. 원본 로그/명령 확인 필요 |
| t33 baseline 보고 | Rust 33 PASS(27 lib+6 bin), native 0.3.0 setup/prerequisite PASS | baseline 환경 확인이며 새 GPU affine 수용 검증 아님 |
| t23 검증 문서 | 집중 11모듈/201개, 2 SKIP는 mock 기반. 당시 전체 2,715개에서 failures=11/errors=23/skipped=90 | 수정 후 전체 재실행 없음. Rust access denied로 실제 RTX 전체 길이 실행 못함. 모든 실패를 baseline 탓으로 단정하지 말 것 |

## 8. 재개 우선순위와 완료 조건

1. **t28 보존 → t28/t33 affine 및 v2 계약**: 원본 작업 재배정과 실제 보존 commit부터 확보하세요. 이후 승인된 통합 범위에서 t27 binary 입력과 t29 timestamp/ownership 처리를 보존하세요. v1은 CPU에서 full-frame raster한 BGRA 레이어이며 변환 필드가 없습니다. v1에 identity 외 변환을 조용히 허용하지 마세요.
   - v2 제안은 `schema=2`, `format=mms_canvas_bgra_layers_v2`, `inverse_transform=[a,b,c,d,tx,ty]`(output pixel center → source pixel center, pixel 단위), `clip=[left,top,right,bottom]`(output half-open), 생략 시 identity/full output입니다. MMSB binary-v1 envelope와 manifest CLI 호환성, opacity 처리는 보존하는 방향입니다.
   - 과거 메시지에는 clip을 `[x,y,w,h]`로 쓴 초기 제안도 있습니다. 위 half-open 제안과 혼용하지 말고 필드·수학식·좌표/샘플링·검증/거절 규칙·담당 경계를 계약 문서에서 확정하세요. **어느 제안도 합의·구현 완료가 아닙니다.** shader/capture/production 연결, 픽셀 parity/readback 근거가 확보되기 전 blocker를 해제하지 마세요.
2. **t30**: 최신 binary/cache/소스 지원 조건을 앱 snapshot/GUI frame source, preflight, 오디오→encode→mux 진행률과 전체 wall timer에 통합하세요. 취소/창 닫기/장치 오류, worker/thread/texture 정리, 출력 충돌, FFmpeg 재시도까지 검증하세요. VIDEO 및 crossfade/AutoMix 미지원은 전체 내용을 유지하는 기존 FFmpeg 경로와 사용자 안내로 처리하고 누락/검은 프레임을 성공으로 만들지 마세요.
3. **t31**: release worker bundle과 탐색 경로, DLL/driver/D3D11/NVENC, FFmpeg 설정을 실제 설치본에서 확인하세요. Rust 없는 대상 PC와 GPU 미지원 오류/대체 경로도 확인하세요. build 성공이나 파일 존재만으로 PASS하지 마세요.
4. **t32**: 동일 PC/프로젝트/해상도/FPS/오디오 및 동등 화질 조건으로 준비 시작부터 최종 mux/게시 완료까지 wall time을 A/B 측정하세요. NVENC P3/HIGH_QUALITY와 FFmpeg CRF/preset은 자동 동등하지 않습니다. 동등 화질 미확보 시 비용 수집으로만 보고하세요. 전체 frame/PTS/길이, 곡 경계/가사/한글·겹침/모션/Z/알파/색, 시청·청취/A/V sync, 10분 이상과 전체 playlist, CPU/RSS/VRAM/디스크 peak, 취소/장치 오류/정리를 확인하세요.

새 실행은 자기 worktree의 새 `work/` 하위 경로에 commit, 환경, 전체 명령, JSON/log, 영상, ffprobe/decode 및 시청·청취 판정을 함께 보존하세요. 임시 staging의 `complete.json` 등은 정리 전에 필요한 근거를 확보하세요. 각 보고에 **이번 직접 실행 / 이전 보고 / 미검증**을 구분하고 blocker와 다음 행동을 남기세요.

## 9. 승인 경계

현재 허용 범위는 인수인계 문서 작성·검토와 근거의 읽기 확인입니다. 후속 구현은 새로 배정된 범위 안에서 진행하세요. main merge, push, publish, deploy는 사용자 명시 승인 전 금지입니다. 자기 worktree 밖 수정, 타인 미커밋 변경의 임의 복사/병합, 사용자 산출물 삭제·덮어쓰기, global Git 설정 변경으로 접근 오류 우회도 하지 마세요. 승인 제한으로 확인하지 못하면 시도/오류/미검증을 그대로 남기세요.

다음 보고는 보존 SHA/경로, 완료·보류 표, 실제 명령/결과, 미검증, 다음 우선순위가 포함된 검토 가능한 diff로 제출하세요. 메모리나 팀 도구에만 남기지 마세요.
