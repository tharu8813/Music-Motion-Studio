# t28 cache 보존 및 t33 재개 근거

작성 기준: 2026-11-10, t34. 이 문서는 다음 에이전트의 인수인계 자료이며 구현 완료 보고가 아니다. **t28 미커밋 cache 구현의 보존 commit은 아직 없다. 원래 kit-t28 작업으로 재배정하여 보존해야 한다.** 사용자 산출물과 untracked 파일을 삭제하지 않는다.

## 현재 확인한 위치와 Git 상태

`git worktree list --porcelain`, `git branch -v`, `git rev-parse HEAD`로 확인했다. 다른 worktree의 파일은 열거나 변경하지 않았다.

| 대상 | branch | worktree | 확인한 HEAD |
| --- | --- | --- | --- |
| t28 | `agentcraft/kit/t28-gpu-cpu` | `C:/Users/wlqor/.agentcraft/codex/worktrees/playlist-project/kit-t28` | `80475358080a7c5a3946a64491f45475e3b4c277` |
| t33 | `agentcraft/kit/t33-t28-gpu-affine` | `C:/Users/wlqor/.agentcraft/codex/worktrees/playlist-project/kit-t33` | `b3304196dea1a4581a0be799184a84287b54802e` |
| t34 | `agentcraft/kit/t34-t28-t33` | `C:/Users/wlqor/.agentcraft/codex/worktrees/playlist-project/kit-t34` | `b3304196dea1a4581a0be799184a84287b54802e` |
| main | `main` | `C:/Users/wlqor/Desktop/playlist_project` | `b3304196dea1a4581a0be799184a84287b54802e` |

t34 시작 시 `git status --short` 출력은 없었다. t34의 변경은 이 문서 추가뿐이다. `git diff --quiet agentcraft/kit/t33-t28-gpu-affine HEAD`는 종료 코드 0이었다. 이는 **커밋 간 동일성**이며 t33 작업 디렉터리의 현재 clean 상태를 증명하지 않는다.

main에는 t27 (`974e70d`, merge `12b2c4b`)과 t29 (`4a0f222`, merge `b330419`)가 들어 있다. t28 HEAD는 그보다 이전이다. t28 ref의 커밋만 전달하면 미커밋 cache 구현은 전달되지 않는다. 현재 t34 HEAD의 tracked tree에는 `app/renderer/native_layer_cache.py`와 `native/gpu_export_core/src/resident_layer_cache.rs`가 없다.

## 보존 blocker와 다음 담당자의 첫 작업

t34는 자기 worktree 안에서만 작업해야 한다. 따라서 kit-t28의 `git status`, 실제 diff, untracked 목록, 산출물을 직접 검사하거나 그곳에 commit할 수 없다. 접근 시도, 우회 복사, 삭제, 다른 디렉터리에서의 Git 실행, main merge는 하지 않았다. **정상 팀 흐름으로 Kit를 원래 t28 branch/worktree 작업에 재배정해야 한다.** t34 문서 작성 완료와 t28 구현 보존 완료를 구분해야 한다.

재배정된 담당자는 kit-t28 안에서 다음 순서로 진행한다.

1. `git branch --show-current`, `git rev-parse HEAD`, `git status --short`, `git diff --stat`, `git diff`, `git ls-files --others --exclude-standard`로 실제 목록을 확보한다. 이 문서의 이전 기록과 대조하고 누락을 보완한다.
2. 사용자 프로젝트·영상·측정 결과·untracked 산출물을 보존한다. 일괄 `git add .`, clean/reset/삭제를 하지 말고 구현·테스트·문서를 개별 검토하여 stage한다.
3. `git diff --cached --stat`와 `git diff --cached`를 검토한 뒤 원래 t28 branch에서 보존 commit을 만든다. commit SHA, 포함/제외 파일, 산출물 위치, 검증 상태를 이 문서에 갱신한다. 실패한 검증도 그대로 남긴다.
4. 그 다음 팀의 통합 절차와 승인 범위 안에서 t27/t29와 통합한다. 이 인수인계 작업은 main 임의 merge를 허용하지 않는다. 충돌을 해결하기 전 성공으로 표시하지 않는다.

### 이전 t28 기록에 등장하는 파일과 충돌 지점

출처: 공유 메모 `shared/t28-resident-layer-cache-implementation-and-meas`. 아래는 **전체 실제 변경 파일 목록이 아니다**. kit-t28 diff를 아직 확인하지 못했으므로 테스트 파일명과 추가 수정 파일은 미확정이다.

| 경로 | 이전 기록의 내용 / 통합 시 확인 |
| --- | --- |
| `app/renderer/native_layer_cache.py` | 새 CPU raster cache 모듈 |
| `native/gpu_export_core/src/resident_layer_cache.rs` | 새 native resident cache 모듈 |
| `native/gpu_export_core/src/bin/d3d11_qt_canvas_stream_export.rs` | import/new/update/resource/clear/report 변경. t27 texture block과 겹침. binary 입력과 resident cache 양쪽을 보존해야 함 |
| `app/renderer/native_project_frames.py` | import, scene.addItem 이후 cache 초기화/stack 분리, capture raster_cache 인자, counter/finally cleanup. t29 timestamp interleave와 함께 검토 |
| `docs/architecture/t28-resident-layer-cache.md` | 이전 기록의 구현 경계 문서. 현재 t34에는 없는 미보존 문서 |

`git diff --name-status agentcraft/kit/t28-gpu-cpu HEAD -- app/renderer native/gpu_export_core tests scripts`에서도 위 stream exporter와 native_project_frames가 t28 커밋 이후 main에서 변경되었음을 확인했다. 이는 충돌 위험의 근거이며 실제 merge 충돌을 실행·확인한 것은 아니다. lib 등록, Python capture 연동 및 테스트 변경의 정확한 범위는 t28 실제 diff에서 확인해야 한다.

## 측정과 테스트: 이전 기록과 이번 확인의 구분

다음 결과는 공유 메모의 **이전 작업 보고**다. t34에서 해당 파일이나 실행 로그를 다시 열지 않았고 재실행하지 않았다. 원래 실행의 전체 CLI가 메모에 없어 추정 명령을 성공 명령처럼 적지 않는다.

| 출처 / 대상 | 이전 결과 | 한계 |
| --- | --- | --- |
| t28 `work/t28-resident-production-02/comparison.json` | production Qt → D3D11 → NVENC, 320×180, 60 frames, 3 layers. paints 180→62, uploads 180→62, allocations 180→3, hits 118. CPU retained at end 0, peak 460800 bytes, GPU input peak 691200 bytes, released 3 | debug stream-only 2.985초. 전체 export A/B 속도 향상 근거가 아님. 경로는 kit-t28 기준이며 현재 파일 존재/내용 미검증 |
| t28 GPU resident/fresh 비교 | 128×128, 60 frames, NV12 전체 byte parity; dirty alpha와 Z reorder 포함. uploads 180→4, allocations 180→3 | 원래 CLI/로그는 재배정 후 확인 필요 |
| t28 Python 관련 테스트 | 56 PASS, 1 SKIP | 파일 목록과 전체 CLI 미확보, 현재 main 통합 후 결과 아님 |
| t28 Rust 테스트 | lib 29 + stream 3 PASS, 이후 ownership 테스트 추가/PASS, cache module 5 tests 기록 | 실행별 전체 CLI/로그와 최종 총수는 미확보. 임의 합산하지 않음 |
| t33 baseline33 | `cargo test --manifest-path native/gpu_export_core/Cargo.toml --all-targets`: 33 PASS (lib 27 + bins 6) | 기존 baseline 결과이며 새 affine acceptance가 아님 |
| t33 native 환경 | `.venv/Scripts/python scripts/setup_rust_native.py`로 native 0.3.0 설치, `.venv/Scripts/python scripts/rust_prerequisites.py` PASS | 이전 t33 기록. rustc sandbox 제한으로 승인된 tool 접근이 필요했음. t34 환경 검증 결과 아님 |
| t28 native 환경 | `.venv314` Python 3.14에서 `scripts/setup_rust_native.py`, `scripts/rust_prerequisites.py` 성공 기록, native 0.3.0 | 전체 원래 호출 CLI 미확보. rustc sandbox 제한이 있었음 |

t33 출처: 공유 메모 `shared/t33-prerequisite-and-cache-handoff-blocker`. 당시 source clean, cache 모듈 부재가 보고되었다. 이번 t34에서는 Git 메타데이터·tracked tree만 확인했고 앱 실행, Rust 설치, GPU smoke, 단위 테스트, 전체 A/B를 실행하지 않았다. 문서만 추가하는 작업이며 이전 테스트 결과를 현재 재검증 결과로 바꾸지 않는다.

## t33 affine 미구현 및 v2 제안 상태

production GPU arbitrary affine transform/clip shader는 구현되지 않았다. t28 기록상 static scale/rotation/effects는 CPU에서 한 번 bake하고 dynamic Qt/visualizer paint는 CPU에 남아 있다. CPU stack 분리는 최대 8 streams 및 equal-Z 보존을 의도한 구현 기록이며 실제 코드는 재확인해야 한다. 전체 payload 전달 및 중복 SHA256 CPU scan도 남아 있다.

Juniper의 `mms_canvas_bgra_layers_v2` / metadata schema 2는 **제안**이다. 합의·구현·검증 완료가 아니다. 제안 내용은 output pixel center에서 source center로 가는 pixel 단위 `inverse_transform[a,b,c,d,tx,ty]`, output half-open `clip[l,t,r,b]`, 기본 identity/full이다. 기존 MMSB binary-v1 envelope와 manifest CLI 호환성을 유지해야 한다. 다음 에이전트는 cache 보존과 protocol 합의를 먼저 처리한다. 이 작업에서 새 affine 구현으로 범위를 확장하지 않는다.

production affine/capture/pixel parity/readback acceptance와 full export A/B는 미완료다. baseline33 PASS나 stream-only 수치를 전체 GPU 엔진 완료·전체 속도 향상으로 표현하지 않는다.

## 재개 환경과 승인 경계

소스 실행·디버깅·Windows 설치본 빌드 호스트에는 Rust MSVC rustc/cargo 및 해당 소스에 맞는 `music_motion_native`가 필수다. 재개할 **해당 worktree 안에서** 적절한 Python 환경으로 아래를 실행하고 결과를 기록한다.

```powershell
python scripts/setup_rust_native.py
python scripts/rust_prerequisites.py
cargo test --manifest-path native/gpu_export_core/Cargo.toml --all-targets
```

Rust 미설치·native 불일치를 Python 경로로 조용히 우회하지 않는다. 실패/승인 제한은 blocker로 기록한다. 위 명령은 재개 지침이며 t34 실행 결과가 아니다. 패키징된 최종 프로그램 사용자의 PC에는 Rust가 필요하지 않다.

보존 commit 뒤 실제 diff 기준 관련 Python 테스트와 Rust 테스트를 수행하고 실패·skip·미검증을 기록한다. 사용자 산출물, 기존 측정 JSON·영상·프로젝트를 유지한다. 승인 전 merge/push/publish/deploy를 하지 않는다. 현재 남은 핵심 blocker는 **원래 t28 작업 재배정 및 미커밋 변경의 실제 보존 commit**이다.
