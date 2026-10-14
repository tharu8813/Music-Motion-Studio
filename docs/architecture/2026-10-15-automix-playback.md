# 크로스페이드와 박자 기반 재생 연결 — 2026-10-15

## 기존 재생 엔진과 이번 변경

이 프로젝트는 이미 `app/automix/`에서 비트·다운비트 분석, 전환 후보 선택,
박자 정렬, 템포 램프, 보컬 회피 및 FFmpeg 렌더를 수행한다.
`ExportPreviewDialog`는 `QMediaPlayer` 하나로 렌더된 믹스를 재생한다.
문제는 믹스가 아직 준비되지 않았거나 렌더가 실패했을 때였다.
이때는 원본 파일 하나씩만 재생하여, 크로스페이드 설정에도 곡 경계가 끊겼다.

이번 변경은 기존 분석 모델과 AutoMix 계획을 재사용한다. 새로운 분석 모델,
의존성, 프로젝트 저장 형식 또는 설정을 추가하지 않는다.

1. `app/timeline/crossfade.py`에 기존 내보내기용 고정 전환 계획을 옮겼다.
   미리보기와 내보내기가 이 계획을 함께 사용한다.
   기본 크로스페이드 곡선은 선형에서 equal-power로 바꿨다.
   `g_out = cos(πp/2)`, `g_in = sin(πp/2)`이며 중간 지점에서 각각 약 0.707이다.
   각 곡 길이의 절반까지 겹쳐 짧은 곡 세 개가 동시에 겹치는 것을 방지한다.
   사용자가 지정한 공백, 곡별 음량과 전환 없음 모드를 유지한다.
2. `app/preview/live_mix_player.py`의 두 Qt 플레이어가 원본을 함께 재생한다.
   다음 곡은 시작 3초 전부터 로딩하고, 준비 상태와 탐색 가능 여부를 확인한 뒤
   최신 재생 위치에 맞춰 시작한다. 일시정지, 드래그, 탐색, 닫기에 두 플레이어가
   함께 반응한다. 다음 파일이 아직 준비되지 않았으면 나가는 곡의 레벨을 유지한다.
   파일 오류는 기존 미리보기 오류 UI에 표시한다.
3. AutoMix 준비 중에도 고정 크로스페이드 계획으로 바로 재생한다.
   박자 기반 전환은 기존 분석 → `compile_automix` → 부분/최종 FFmpeg 믹스 경로를
   이용한다. 새 믹스는 기존 `swap_playhead` 정책으로 전환 구간 바깥에서 적용하며,
   계획이 달라졌으면 같은 곡의 같은 소스 위치로 재생 위치를 옮긴다.
   일반 크로스페이드 렌더 결과에도 이 안전한 교체 정책을 적용했다.
4. 원본 재생 경로도 `AudioRenderClip.source_at()`과 `rate_segments()`를 사용한다.
   따라서 부분 믹스 이후의 재생에 트림·재생 속도·템포 램프가 있더라도
   화면의 소스 시간과 다른 곡 위치를 재생하지 않는다.
   Qt가 지원하면 피치 보정을 활성화한다.

## REA로 확인한 사실

대상은 사용자가 지정한 설치 폴더의 **Cider 4.0.27.0**이다.
앱을 실행하거나 로그인하지 않고 정적으로 검사했다.

| REA 관찰 | 근거 | 범위와 한계 |
| --- | --- | --- |
| `C2Windows/Cider.dll`은 x86_64 managed PE, modern .NET/CIL이며 `.NETCoreApp,Version=v11.0` 메타데이터가 있다. | `inspect_managed_artifact` 원본 Evidence `ev_502852b0332ad8766ccd6390f266e37b2c9aa94906a9c6ec751b879c42d227da`; DLL SHA-256 `bc6353c400bfd47b80b9b556b67411607f5956c71e492c9c7c1278125109f57c` | 메타데이터 검사이며 CLR 실행, C# 디컴파일 또는 재생 동작 검증은 아니다. |
| SPA에 JavaScript 워커 `assets/nextPipeline.worker-Ci57Kui3.js`가 포함되며 80,078바이트의 소스를 정적 파싱했다. | `analyze_javascript_application` Evidence `ev_dc1426e5c55e8f50f3c37f76f71c92fd6201e0bf378445e92d029c5d0e74f354`; 워커 SHA-256 `227b8c407f0cea4bd43455c574bfb35946bf0261e371c6611572b6d2ddd7b282` | 정확히 이 워커만 분석했다. 나머지 24개 파일의 관계와 실행은 미확인이다. |
| 워커 함수 `Ae`에 `automixEnabled`, `currentProfile`, `nextProfile`, `bpm`, `bars`, `vocalGuard`, `tempoAlign`, `tempoRampRate`, `entryMs`, `exitMs`, `overlapMs` 등의 속성 슬롯·구조 분해 후보가 있다. | 위 Evidence의 semantic graph; 함수 노드 `jsrg_node_716de5109a2ee80c8d2696ebe32eaf2ff68fa18f9742beedc92353d169186dfc`; 워커 1행, 열 12220–13720 | AST 관찰이다. 각 속성이 런타임에서 항상 제공된다는 뜻은 아니며, 실제 박자 정렬 품질도 입증하지 않는다. |
| 워커 함수 `Wn`의 정적 함수 범위가 존재한다. | 위 semantic graph; 노드 `jsrg_node_e44566132a981c338b88caf7b284702eb1e2df2c7e9b31cca6ca79257d87d623`; 2행, 열 1043–1422 | 이 함수가 실제 호출되었다는 증거는 아니다. |

선택한 REA 관찰과 출처 좌표는
[`cider-4.0.27-rea-observations.json`](cider-4.0.27-rea-observations.json)에 보존했다.
전체 REA 번들은 로컬 `work/cider-4.0.27-playback-rea.json`에 있다.
REA의 `trace_application_feature`에서 `crossfade` 문자열은 매칭되지 않았다.
이는 분석 그래프의 제한이며 해당 기능이 없다는 증거로 사용하지 않았다.

## 설치 파일을 직접 읽어 확인한 내용 — REA 실행 관찰 아님

워커 소스에는 `compute-crossfade-plan`, `process-audio-analysis`,
`compute-automix-plan` 작업 분기가 있다. 이는 패키지 소스를 직접 읽은 결과이다.

`assets/index-Byzs6j24.js`에는 현재/다음 오디오 객체,
`buildEqualPowerCurves`, `startElementVolumeCrossfade` 등의 코드가 있다.
후자의 요소 볼륨 폴백은 cos 기반 페이드 값을 100ms 간격으로 갱신한다.
이 파일 SHA-256은
`992ca95080e3270bdc5280027bbfacc3f838633f1fcd9fd228cf1bf8a7d47671`이다.
이 큰 번들 전체는 REA 분석 대상에 포함하지 않았으며, 해당 경로의 실제 사용 여부,
DRM 스트림 전환, 오디오 장치 지연 및 Cider의 청취 품질은 검증하지 않았다.

## 참고 저장소에서 확인한 설계

[VertexSelection/automix-engine](https://github.com/VertexSelection/automix-engine)은
[`stem_transition.py`](https://github.com/VertexSelection/automix-engine/blob/main/stem_transition.py)와
[`cue_analysis.py`](https://github.com/VertexSelection/automix-engine/blob/main/cue_analysis.py)에서
큐 후보와 비트 그리드, 스템 분리 및 시간 변형을 조합한다.
32개 전환 박자와 6.48초 템포 복귀 구간을 두며 보컬 진입도 별도로 판단한다.
이는 참고 저장소의 소스 사실이며 Cider의 내부 구현을 설명하는 증거가 아니다.

이 프로젝트에는 이미 비트 그리드와 보컬 정보, 템포 램프 및 렌더 파이프라인이
있으므로 재사용했다. Demucs와 Rubber Band 스템 렌더러를 추가하거나 참고 저장소의
코드를 복사하지 않았다. 두 Qt 플레이어의 수명 관리, 3초 선로딩, 소스 시간 동기화,
기본 계획 공유 및 안전한 믹스 교체는 이 프로젝트를 위한 자체 설계이다.

## 검증과 사용

프로젝트 설정에서 **크로스페이드**와 전환 길이를 선택하면 기본 미리보기를
바로 사용할 수 있다. **AutoMix**를 선택하면 준비 중에는 기본 전환으로 재생하고,
FFmpeg와 분석 결과가 준비되면 박자 기반 믹스로 교체한다.

테스트는 `scripts/run_tests.py`의 프로세스 격리 방식으로 실행했다.
검증 기록은 [`automix-playback-verification.json`](automix-playback-verification.json)에 있다.
기본 전환·박자 계획·FFmpeg 렌더·부분 믹스 교체·화면 전환·음량/EQ·탐색 및
미리보기 수명 관리를 검사했다. 실제 Qt WAV 디코더도 음소거 상태로 두 파일의
로딩·소스 위치·동시 재생·일시정지를 확인했다.

남은 한계: Qt 원본 재생은 타이머/장치 스케줄링 기반이며 샘플 단위 박자 동기화를
보장하지 않는다. EQ·스타일 DSP·리미터 및 내보내기와 동일한 최종 음량 처리는
렌더된 믹스에서 적용한다. 박자 기반 분석과 정확한 렌더에는 FFmpeg가 필요하다.
실제 음악을 사용한 청취 튜닝, Cider의 실행 비교, 설치 파일 재빌드는 하지 않았다.
