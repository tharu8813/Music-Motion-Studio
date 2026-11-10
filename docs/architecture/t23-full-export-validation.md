# t23 전체 길이 GPU/FFmpeg 검증 기록

기준: `0b7e3f8` (t22 UI 통합 병합). 2026-11-10, Kit 작업 트리.

## 확인한 사실과 제한

- `scripts/rust_prerequisites.py`를 기본 Python 및 기존 프로젝트 venv로 실행했다. 둘 다 `rustc --version`에서 access denied / os error 5로 실패했다. 소스 앱 실행, Rust 빌드, RTX 전체 길이 내보내기는 실행하지 않았다. 이전 20초 테스트도 반복하지 않았다.
- 읽기 전용 `nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader`는 `NVIDIA GeForce RTX 5060, 621.01, 8151 MiB`를 반환했다. GPU 부재가 아니라 Rust 실행 권한 조건 때문에 실제 내보내기를 진행하지 못했다.
- 기본 Python 3.15에는 PySide6가 없어 첫 테스트 호출은 import 실패했다. 기존 `C:\Users\wlqor\Desktop\playlist_project\.venv\Scripts\python.exe`로 하드웨어를 mock한 9개 모듈 80개 테스트를 실행: 통과, 2개 skip. 테스트가 출력하는 예제 A/B 수치는 fixture이며 실측 성능이 아니다.
- 추가 회귀 후 finalizer/runtime/full-project 3개 모듈 28개 테스트 통과, 1개 skip. 3시간 길이의 소수 프레임 곡 경계와 마지막 PTS 계산, AAC priming 허용 및 종료 오차 누적 거절을 검사했다. 실제 3시간 렌더링이나 기존 RTX smoke 재실행을 의미하지 않는다. `compileall`, 문서 내 inline Python의 AST 파싱, `git diff --check`도 통과했다.
- 전체 `unittest discover -s tests -t .`도 실행: **2,715개, 780.160초, failures=11 / errors=23 / skipped=90**. 전체 통과가 아니다. 오류에는 librosa/Numba cache locator 권한, Windows 자격 증명 암호화 등이 있고 실패에는 한국어 문구를 기대하는 번역 UI, 성능 모니터 항목, 미리보기 idle timer 등이 있다. 당시 baseline 미검증이었으며, 이후 의심 모듈만 집중 비교했다(아래 기록). 모든 실패가 기존 문제라고 단정하지 않는다.
- 그중 FFmpeg 기능 회귀 4개는 `object.__new__` fixture가 t22의 `prevent_output_overwrite` 필드 초기화를 생략해서 발생했다. 실제 생성자의 기본값과 같이 fixture를 초기화했다. 수정 후 `tests.test_functional_regressions` **113개 전부 통과**. 수정 후 전체 모음을 다시 실행한 것은 아니며 위 전체 집계에서 임의로 오류 수를 빼지 않는다.
- GPU는 `transition_mode=none`만 지원한다. crossfade/AutoMix는 사전 검사에서 거절하고 기존 FFmpeg로 전환한다. GPU 전환 지원 완료로 간주하면 안 된다.
- 일반 Canvas 키프레임/가사/다곡 시계와 동적 오디오 소스 계약을 검사한다. 비디오 및 동적 소스의 custom keyframe/rotation은 거절한다. 실제 전체 길이 영상의 픽셀 정확성과 전체 시청·청취는 미검증이다.
- 프레임 전달은 bounded pull/ACK 구조이고 테스트는 취소/조기 종료/오류 정리를 검사한다. 실제 긴 프로젝트의 RSS, 디스크 최대 사용량, RTX 처리 시간은 미측정이다.
- 제품 수정: 최종 MP4의 시작 시각이 유한한지 검사하고 영상은 0초(1µs 허용), 오디오는 AAC priming 허용 범위 ±50ms인지 검사한다. 오디오 종료 시각도 프로젝트 길이와 비교한다. 이전에는 길이가 같고 시작만 밀린 파일도 완료 처리될 수 있었다. 잘못된/누락된 timestamp는 게시하지 않고 staging을 정리한다.
- native NVENC는 P3/HIGH_QUALITY 드라이버 기본값, B-frame/Lookahead 비활성이다. UI의 FFmpeg CRF/preset은 적용되지 않는다. **같은 UI 옵션만으로 동일 인코딩 품질을 주장할 수 없다.** 따라서 아래 A/B는 정확성 및 비용 수집용이며 품질 동등 성능 비교나 속도 향상 수치로 쓰지 않는다. 실측 근거가 없어 최적화 변경을 추가하지 않았다.

## 리뷰 후 집중 확인과 실패 분류

전체 모음은 재실행하지 않았다. 아래 결과는 이전 전체 집계의 오류 수를 고쳐 쓴 것이 아니라 별도 실행 기록이다. 수정 후 GPU/FFmpeg 관련 11개 모듈 **201개, 9.498초, OK(skipped=2)**. 아래 실기 회귀 명령과 같은 모듈 목록이며 RTX 인코더는 mock했다.

의심 모듈 baseline은 `git show 0b7e3f8:<path>`로 제품 finalizer와 테스트 소스를 읽어 메모리 모듈로 실행했다. branch 전환, main 작업 트리 수정, Rust/GPU 실행은 하지 않았다. baseline/current 모두 같은 순서로 `test_functional_regressions`, `test_native_project_runtime`, `test_gpu_export_ui`, `test_native_gpu_production_finalizer`, `test_preview_presentation`, `test_performance_monitor`를 실행했다.

| 집중 검사 | 결과 | 해석 |
|---|---|---|
| baseline 0b7e3f8, 171개 | errors=5, failures=6, skipped=1 | FFmpeg fixture 4개, 빈 GPU frame fixture 1개 오류 재현. idle 타이머 1개 및 monitor cursor subtest 5개 실패 |
| 수정 후 같은 모듈, 174개 | errors=0, failures=6, skipped=1 | fixture 오류 5개 해결, 추가 GPU 회귀 3개 통과. 실패 ID와 메시지는 baseline의 UI 6개와 동일 |
| idle 타이머 + preset 단독, 2개 | OK | 모듈 묶음/이벤트 순서에 따라 실패가 달라진다. GPU finalizer 호출과 관계없는 경로지만 flaky 원인 해결을 주장하지 않음 |
| 분석·인증·번역 8개 모듈, 148개 | errors=19, failures=1, skipped=4 | Numba cache 13개, Windows 자격 증명 보호 6개 오류 재현. 계정 migration 실패 재현. 한국어 기대 문구 실패 8개는 이 순서에서는 미재현 |

위 결과에서 **남아 있는 GPU finalizer/UI routing/FFmpeg 회귀는 발견하지 못했다.** 실제 RTX 출력 정확성·A/V 청취·성능·메모리/디스크 실측은 여전히 미검증이다. 아래의 baseline 미확인 항목 전체를 기존 문제로 단정하지 않는다.

전체 실행의 **오류 23개**를 실패 ID 기준으로 분류했다. 각 항목의 메서드명 앞에는 `test_`가 붙는다.

| 분류 / 개수 | 모듈·메서드 | 확인 상태 |
|---|---|---|
| GPU UI 통합 영향, 4 | `test_functional_regressions`: `directly_encoded_canvas_is_muxed_without_second_video_encode`, `final_video_copies_prepared_aac_without_reencoding`, `one_explicit_frame_can_cover_a_multi_track_playlist`, `prepared_canvas_video_bypasses_png_concat_input` | baseline 재현, fixture 필드 초기화로 해결, 수정 후 모듈 113개 통과 |
| Numba cache locator, 7 | `test_automix_basic_analyzer`: `bpm_detected_for_synthetic_clicks`의 120/128/150 BPM subtest, `downbeats_are_provisional_and_bpm_only_quality`, `key_and_energy_are_populated_for_a_tonal_signal`, `no_vocal_activity_is_guessed_from_the_voice_band`, `rhythm_added_later_is_the_rhythm_computed_up_front` | 집중 재현, GPU finalizer 실행 전 분석 의존성 오류. baseline 미확인 |
| Numba cache locator, 5 | `test_automix_beat_this_onnx`: `accented_track_longer_than_one_chunk_is_a_reliable_four_four`, `short_track_fits_one_padded_chunk`, `a_stem_far_below_the_mix_is_not_singing`, `separates_only_the_head_and_tail_regions`, `shipped_model_runs_on_a_short_track` | 집중 재현, 동일 cache 오류. baseline 미확인 |
| Numba cache locator, 1 | `test_automix_intelligence`: `measurements_are_extracted_without_structure_model_and_cached` | 집중 재현, 동일 cache 오류. baseline 미확인 |
| Windows 자격 증명 보호, 3 | `test_chatgpt_auth`: `cancel_after_refresh_prevents_model_request`, `dynamic_registration_pkce_verified_identity_and_stable_host`, `refresh_uses_issued_client_and_saves_rotated_token` | 집중 재현, `Cannot protect or read credentials`. baseline 미확인 |
| Windows 자격 증명 보호, 2 | `test_lyrics_translation`: `persisted_credentials_are_windows_encrypted`, `translation_settings_cannot_remove_persistent_chatgpt_session` | 집중 재현, 동일 보호 오류. baseline 미확인 |
| Windows 자격 증명 보호, 1 | `test_openai_account`: `account_form_exposes_status_without_tokens` | 집중 재현, 동일 보호 오류. baseline 미확인 |

전체 실행의 **실패 11개**도 별도로 분류했다.

| 분류 / 개수 | 모듈·메서드 | 확인 상태 |
|---|---|---|
| 한국어 기대 문구, 6 | `test_local_llm`: `languages_filter_models_and_inline_install_preserves_selected_languages`, `manager_can_use_already_installed_catalog_model_without_redownloading`, `manager_delete_is_basic_action_with_confirmation_and_no_automatic_fallback`, `manager_missing_preference_does_not_silently_select_different_model`, `manager_shows_tradeoffs_downloads_and_selects_installed_model`, `three_modes_and_saved_local_llm_are_restored_with_installed_combo` | 집중 실행에서 통과. 전체 실행은 영어 문구를 반환. baseline/전체 순서 원인 미확인 |
| 한국어 기대 문구, 2 | `test_local_translation`: `incomplete_local_rows_are_visible_and_preserve_saved_translations_on_apply`, `live_results_remain_unapplied_and_cancel_restores_prior_review` | 집중 실행에서 통과. baseline/전체 순서 원인 미확인 |
| 계정 migration, 1 | `test_openai_account`: `legacy_session_is_migrated_and_readable_after_process_restart` | 집중 재현, `remember=False`. baseline 미확인 |
| monitor preset, 1 | `test_performance_monitor`: `preset_tab_and_every_preset` | 단독 및 baseline/current 모듈 묶음에서 통과. 전체 실행의 항목/문구 상태 차이 원인 미확인 |
| 미리보기 idle, 1 | `test_preview_presentation`: `idle_timer_fires_while_cursor_is_stationary` | baseline/current 묶음에서 동일 재현, 단독 통과. t23 GPU 변경에 따른 신규 실패는 아님 |

baseline/current 모듈 묶음에서 추가로 나온 monitor cursor 5개 실패는 원래 전체 실패 11개에 더하거나 빼지 않았다. 둘 다 `(1,0), (0,-1), (0,1), (1,1), (1,-1)`에서 ArrowCursor가 남는 동일 결과다. 이 UI 순서 의존성과 분석/인증 환경 오류를 해결했다고 주장하지 않는다.

## 사용자 PC: 전제 검사와 전체 길이 UI 실행

PowerShell에서 아래 블록을 순서대로 실행한다. 병합된 저장소 루트 `C:\Users\wlqor\Desktop\playlist_project` 기준이므로 작업 worktree 정리 후에도 사용할 수 있다. 기존 프로젝트 venv를 사용하며 파일 선택창에서 실제 전체 프로젝트를 선택한다(프로젝트 원본 위치를 추측하지 않음). 내보내기 파일은 새 이름을 사용한다. 기존 smoke 파일을 덮어쓰지 않는다.

자동 회귀 재실행 명령(이 환경에서는 mock 계약 테스트로 실행했으며, 개발 PC에서는 먼저 아래 prerequisites 검사를 통과해야 한다):

```powershell
Set-Location 'C:\Users\wlqor\Desktop\playlist_project'
$python = 'C:\Users\wlqor\Desktop\playlist_project\.venv\Scripts\python.exe'
& $python scripts/rust_prerequisites.py
if ($LASTEXITCODE -ne 0) { throw 'Rust/native prerequisites 실패' }
& $python -m unittest tests.test_native_gpu_production_finalizer tests.test_native_gpu_stream tests.test_native_project_runtime tests.test_gpu_export_ui tests.test_native_gpu_full_export tests.test_native_canvas_project_probe tests.test_native_canvas_timeline_probe tests.test_native_canvas_real_lyrics_probe tests.test_native_canvas_dynamic_overlays tests.test_export_ab_verify tests.test_functional_regressions
if ($LASTEXITCODE -ne 0) { throw '관련 회귀 실패' }
```

```powershell
Set-Location 'C:\Users\wlqor\Desktop\playlist_project'
$python = 'C:\Users\wlqor\Desktop\playlist_project\.venv\Scripts\python.exe'
& $python scripts/rust_prerequisites.py
if ($LASTEXITCODE -ne 0) { throw 'Rust MSVC/native prerequisites 실패. 아래 복구 후 이 블록부터 다시 실행하세요.' }
$ffmpeg = (Get-Command ffmpeg.exe -ErrorAction Stop).Source
$ffprobe = (Get-Command ffprobe.exe -ErrorAction Stop).Source
Add-Type -AssemblyName System.Windows.Forms
$picker = New-Object System.Windows.Forms.OpenFileDialog
$picker.InitialDirectory = 'C:\Users\wlqor\Desktop'
$picker.Filter = 'Music Motion project (*.pvsproj)|*.pvsproj'
if ($picker.ShowDialog() -ne 'OK') { throw '프로젝트 선택 취소' }
$project = $picker.FileName
New-Item -ItemType Directory -Force build/t23 | Out-Null
& $python tools/export_bottleneck_profile.py --output build/t23/ffmpeg-profile.json -- $python main.py $project
if ($LASTEXITCODE -ne 0) { throw 'FFmpeg UI 실행 실패' }
& $python tools/export_bottleneck_profile.py --output build/t23/gpu-profile.json -- $python main.py $project
if ($LASTEXITCODE -ne 0) { throw 'GPU UI 실행 실패' }
```

첫 UI에서는 FFmpeg 엔진, 두 번째에서는 Rust GPU 엔진을 선택한다. 각각 **전체 길이** MP4를 `C:\Users\wlqor\Desktop\playlist_project\build\t23\ffmpeg-full.mp4`, `C:\Users\wlqor\Desktop\playlist_project\build\t23\gpu-full.mp4`로 저장하고 완료 후 앱을 닫는다. 동일 프로젝트, 60fps, 원본 Canvas 해상도, AAC 192k를 사용하고 실제 인코더 설정을 함께 기록한다. 대기/편집 시간은 줄이고 다른 GPU 작업은 종료한다. GPU 지원 불가 안내가 나오면 해당 실행은 FFmpeg 대체이며 GPU 성공 기록에서 제외한다. 전환 없음 프로젝트와 crossfade/AutoMix 프로젝트는 별도 실행한다. 후자는 현재 FFmpeg 유지 확인용이다.

전제 검사 실패 시 원인을 먼저 해결한다. Rust MSVC/Visual Studio C++ Build Tools가 설치되어 있어야 한다. 권한 오류를 해결한 일반 개발 터미널에서 다음을 실행한 뒤 위 검사를 다시 한다. 개발 환경 조건을 우회하지 않는다.

```powershell
& 'C:\Users\wlqor\Desktop\playlist_project\.venv\Scripts\python.exe' scripts/setup_rust_native.py
if ($LASTEXITCODE -ne 0) { throw 'native 설치 실패' }
& 'C:\Users\wlqor\Desktop\playlist_project\.venv\Scripts\python.exe' scripts/rust_prerequisites.py
if ($LASTEXITCODE -ne 0) { throw 'prerequisites 실패' }
cargo build --release --manifest-path native/gpu_export_core/Cargo.toml --bin d3d11_qt_canvas_stream_export
if ($LASTEXITCODE -ne 0) { throw 'GPU executable 빌드 실패' }
```

## 기존 A/B 도구와 전체 프레임/챕터/PTS 비교

위 PowerShell 세션에서 계속 실행한다. A/B 도구의 3개 샘플만으로 경계 정확성을 승인하지 않는다. 손실 인코딩 차이로 hash가 다르면 review가 정상적으로 가능하다.

```powershell
& $python tools/export_ab_verify.py --baseline-profile build/t23/ffmpeg-profile.json --optimized-profile build/t23/gpu-profile.json --baseline-video build/t23/ffmpeg-full.mp4 --optimized-video build/t23/gpu-full.mp4 --ffmpeg $ffmpeg --ffprobe $ffprobe --output build/t23/ab.json --markdown build/t23/ab.md
& $ffprobe -v error -count_frames -show_streams -show_chapters -show_format -of json build/t23/ffmpeg-full.mp4 | Set-Content -Encoding utf8 build/t23/ffmpeg-media.json
& $ffprobe -v error -count_frames -show_streams -show_chapters -show_format -of json build/t23/gpu-full.mp4 | Set-Content -Encoding utf8 build/t23/gpu-media.json
& $ffmpeg -v error -i build/t23/ffmpeg-full.mp4 -map 0:v:0 -map 0:a:0 -f null -
if ($LASTEXITCODE -ne 0) { throw 'FFmpeg 기준 파일 전체 decode 실패' }
& $ffmpeg -v error -i build/t23/gpu-full.mp4 -map 0:v:0 -map 0:a:0 -f null -
if ($LASTEXITCODE -ne 0) { throw 'GPU 파일 전체 decode 실패' }
@'
import json, math, subprocess, sys
from pathlib import Path
from fractions import Fraction
probe, ffmpeg = sys.argv[1:]
root = Path('build/t23')
media = [json.loads((root / f'{name}-media.json').read_text(encoding='utf-8-sig')) for name in ('ffmpeg', 'gpu')]
videos = [next(s for s in m['streams'] if s['codec_type']=='video') for m in media]
fps = Fraction(videos[0]['avg_frame_rate'])
assert fps == 60 and Fraction(videos[1]['avg_frame_rate']) == fps
assert videos[0]['nb_read_frames'] == videos[1]['nb_read_frames']
assert [(v['width'],v['height']) for v in videos][0] == [(v['width'],v['height']) for v in videos][1]
chapters = [[(c['tags'].get('title'),float(c['start_time']),float(c['end_time'])) for c in m.get('chapters',[])] for m in media]
assert chapters[0] == chapters[1], 'chapter titles/times differ'
for name,m,v in zip(('ffmpeg','gpu'),media,videos):
    a = next(s for s in m['streams'] if s['codec_type']=='audio')
    assert abs(float(v['start_time'])) <= 1e-6
    assert abs(float(a['start_time'])) <= .05
    assert abs(float(v['duration'])-float(a['duration'])) <= .12
    # Stream all PTS in constant memory; no full-movie packet list.
    p = subprocess.Popen([probe,'-v','error','-select_streams','v:0','-show_entries','frame=best_effort_timestamp_time','-of','csv=p=0',str(root/f'{name}-full.mp4')],stdout=subprocess.PIPE,text=True)
    count=0; maximum=0.0
    for line in p.stdout:
        token=line.strip().split(',')[0]
        if not token: continue
        pts=float(token)
        assert math.isfinite(pts)
        maximum=max(maximum,abs(pts-count/float(fps)))
        count+=1
    assert p.wait()==0 and count==int(v['nb_read_frames'])
    assert maximum <= .00001, (name,'cumulative CFR drift',maximum)
    print(name,'frames',count,'max CFR drift',maximum,'A/V ends',float(v['start_time'])+float(v['duration']),float(a['start_time'])+float(a['duration']))
# Same exact presentation frame: every chapter boundary +/-1 and late movie.
total=int(videos[0]['nb_read_frames'])
indices={0,total-1,total//2,max(0,total-60)}
for _,start,_ in chapters[0]:
    n=round(start*float(fps)); indices.update(i for i in (n-1,n,n+1) if 0<=i<total)
for n in sorted(indices):
    for name in ('ffmpeg','gpu'):
        subprocess.run([ffmpeg,'-v','error','-i',str(root/f'{name}-full.mp4'),'-vf',f'select=eq(n\\,{n})','-frames:v','1','-fps_mode','vfr',str(root/f'{name}-frame-{n}.png')],check=True)
print('Metadata/PTS checks passed. Review PNG pairs and listen to the FULL output separately.')
'@ | & $python - $ffprobe $ffmpeg
if ($LASTEXITCODE -ne 0) { throw '전체 PTS/챕터/영상 샘플 검사 실패' }
```

기대 조건: 프레임 수는 전체 프로젝트 길이×60의 올림값, 영상 길이는 프레임 수/60, 챕터 제목·구간 동일, 영상 PTS 누적 오차 ≤10µs, A/V 시작 0초 근처, 끝 차이 ≤120ms. AAC packet priming의 음수 PTS만으로 실패 판정하지 않는다. 이 검사는 음향 이벤트의 누적 싱크를 판정하지 않는다. 모든 곡 경계, 가사 단어 강조, 키프레임 극점, 후반부를 PNG 쌍과 영상으로 확인하고 전체 음성을 청취해 시작/경계/후반부 실제 싱크를 기록해야 한다.

## 안정성·비용 기록

성공 파일은 보존하고 다른 새 파일명으로 준비/인코딩/최종화 중 취소, unsupported 프로젝트, native exe 누락 오류를 각각 확인한다. 취소 후 완료 알림/MP4 게시가 없어야 하며 재실행이 가능해야 한다. 기존 파일이 생긴 경우 덮어쓰지 않아야 한다. 오류 후 FFmpeg 재시도는 사용자 선택으로 진행하고 그 결과를 GPU 결과로 기록하지 않는다.

`ab.json`의 completed wall time, audio prepare, renderer/mux, CPU core seconds, peak process-tree RSS를 기록한다. inclusive 단계는 합산하지 않는다. native 단계가 없는 항목은 미측정으로 남긴다. 디스크는 UI 저장 공간 표시 및 작업 관리자/리소스 모니터로 temp/output 최대값을 기록한다. 긴 프로젝트 전후 같은 시점의 RSS와 scratch 잔존 여부를 비교한다. 지원되는 동일 프로젝트에서 전체 길이 실행이 끝나고 인코딩 품질 동등 조건이 확보되기 전에는 병목이나 최적화 효과를 단정하지 않는다.

이 작업 환경에는 GitHub remote/게시 도구가 없다. 제품 변경과 검증 기록을 리드에게 review로 전달하며 PR 생성 및 main 병합은 리드의 저장소 절차가 필요하다.
