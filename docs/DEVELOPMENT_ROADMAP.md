# Music Motion Studio 안정화 및 성능 개선 로드맵

> 기준: 2026-11-28 KST, main `e00b401be4efc60bee5a2ef5cbcef092ff8cf9c6`  
> 목표: 신규 기능 확대보다 기존 기능의 정확성, 데이터 보호, 성능, 유지보수성 및 배포 신뢰성을 개선한다. 버전 번호와 배포일은 확정하지 않는다.

이 문서는 전체 개발 우선순위와 완료 조건의 기준이다. 세부 측정·검증 진행 기록은 [전체 리뷰 #136](https://github.com/tharu8813/Music-Motion-Studio/issues/136), [미리보기 로드맵 #159](https://github.com/tharu8813/Music-Motion-Studio/issues/159) 및 개별 PR에 기록한다. 이 문서와 기존 이슈가 서로 다른 역할을 가지므로 동일한 작업을 중복 구현하지 않는다.

## 1. 기준 상태

- 기준 트리: 1,012개 파일, Python 테스트 파일 276개(테스트 케이스 수가 아님).
- [기준 main Windows 배포 CI](https://github.com/tharu8813/Music-Motion-Studio/actions/runs/45535880132): 성공. 실제 소비자 Windows 11 및 RTX 하드웨어의 전체 작업 검증을 대체하지 않는다.
- 정적 코드, 기존 계측 및 CI 로그에 기반한 점검이다. 전체 파일의 줄 단위 감사 또는 실제 사용자 PC 재현은 수행하지 않았다.
- 체크박스는 검증되지 않은 작업을 뜻한다. 병합된 변경도 실기기 확인이 필요한 경우 해당 검증 항목은 별도로 유지한다.

### 열린 PR 정리 (Phase 0 / P0)

2026-11-28 KST 통합 진행 기록 (확인한 main: `afb835860666ace7ec88e019c57bc7befaa331bd`). 변경 시 최신 HEAD와 필수 CI를 다시 조회한다.

| PR | 내용 | 현재 기록 | 후속 작업 |
| --- | --- | --- | --- |
| [#251](https://github.com/tharu8813/Music-Motion-Studio/pull/251) | Cascade 짧은 가사 및 후반 이동 지연 | 4/4 성공, 병합 `b72c236` | 실제 Windows 재생 및 추가 시간축 경계 검증 |
| [#252](https://github.com/tharu8813/Music-Motion-Studio/pull/252) | 로드맵 및 문서 정리 | 3/3 성공, 병합 `4a1a3d6` | 진행 기록 유지 |
| [#249](https://github.com/tharu8813/Music-Motion-Studio/pull/249) | 재생 필수 오디오 우선, 선택 분석 지연 | 3/3 성공, 병합 `a81dae5` | 반응형 장면의 실제 준비 속도 계측 |
| [#250](https://github.com/tharu8813/Music-Motion-Studio/pull/250) | Gapless 단계 및 true-peak limiter 계측 | 4/4 성공, 병합 `bb22929` | 측정 결과를 다음 후보 선정에 활용 |
| [#238](https://github.com/tharu8813/Music-Motion-Studio/pull/238) | 로컬 LLM 설치 확인 | 3/3 성공, 병합 `350fa1b` | 로컬 모델 설치·선택 후속 회귀 유지 |
| [#239](https://github.com/tharu8813/Music-Motion-Studio/pull/239) | 로컬 NMT 모델 명시 선택 보존 | 9/9 성공, 병합 `afb8358` | 모델 설치·선택·언어 방향 후속 검증 |
| [#254](https://github.com/tharu8813/Music-Motion-Studio/pull/254) | Windows CI 병렬화·캐시·FFmpeg 기준 고정·M4A 시간축 | 13/13 성공, 병합 `b986548` | 문서 경량 경로 및 main CI 확인 완료, #255/#256 추적 |

#238의 [기존 실패 로그](https://github.com/tharu8813/Music-Motion-Studio/actions/runs/44570421557/job/135723733613)에서 `test_lmstudio_unchanged_installed_list_is_not_download_success`가 기존 모델을 반환했다. 후속 변경에서 초기 조회 순서, 검증된 목록 비교 및 사용자 입력과 Qt 자동 선택의 구분을 개선했다. [후속 CI](https://github.com/tharu8813/Music-Motion-Studio/actions/runs/46145292695) 3/3 성공 후 병합했다.

Copilot의 리뷰 한도 부족 안내는 승인으로 간주하지 않았다. 병합한 PR은 패치 자체를 검토하고 정확한 HEAD의 검사 결과 및 충돌 여부를 확인했다. 실제 사용자 Windows/RTX 수용 검수는 별개다.

- [ ] 모든 열린 PR의 변경 범위·충돌·리뷰·최신 HEAD 필수 CI를 확인한다. #238 → #239 순서를 준수한다.
- [ ] CI 실패·대기·누락 상태에서는 병합하지 않는다. 기존 main 변경을 보존하고 병합 후 결과를 확인한다.

### CI 검증 최적화 기록

- [x] #254: 6개 격리 Python shard, Rust 캐시 및 새 wheel 설치, FFmpeg 9.0 날짜 고정·SHA-256 검증, 문서 변경 분류와 최종 검증 게이트 구현. [최종 PR CI](https://github.com/tharu8813/Music-Motion-Studio/actions/runs/46151198844) 13/13 성공. 이전 540초 대비 190초(약 65% 감소)는 Python 테스트 자체 비교이며 전체 CI 시간이 아니다. 최종 PR 전체 Windows 검증은 13:45:32~13:52:01 UTC, 약 6분 29초.
- [x] [#253 문서 전용 CI](https://github.com/tharu8813/Music-Motion-Studio/actions/runs/46151893594): 분류 및 최종 게이트 성공, Windows 빌드/격리 테스트는 의도대로 생략. [#254 병합 후 main CI](https://github.com/tharu8813/Music-Motion-Studio/actions/runs/46151827098) 성공. main push·수동 실행·코드 혼합 변경은 전체 검증 유지. 브랜치 보호 규칙 조회 권한이 없어 게이트가 필수 규칙으로 지정됐다고 가정하지 않는다.
- [ ] Rust 캐시 적중 및 실제 빌드 시간 비교, 중복 빌드 제거 가능성 평가. 검증 축소 없이 실행 시간을 줄인다.
- [ ] [#255](https://github.com/tharu8813/Music-Motion-Studio/issues/255): FFmpeg 9.1 입력 큐 옵션 호환성 해결. 고정 9.0은 CI 기준이며 9.1 해결이 아니다.
- [ ] [#256](https://github.com/tharu8813/Music-Motion-Studio/issues/256): M4A 편집 오프셋 중복 적용 제거 후 길이·탐색 검사 성공. AAC-LC 시작 2,048샘플의 디코더 간 파형 차이는 별도 측정 대상으로 유지. 전체 PCM 길이·전체 음원 MAE·동일 디코더 부분/전체 일치 및 이후 구간 MAE 기준은 유지한다.

## 2. 단계별 계획

### Phase 1 / P0: 가사 및 시간축 안정화

- [x] #251 코드·자동 검증 및 병합 ([병합 커밋](https://github.com/tharu8813/Music-Motion-Studio/commit/b72c236e903e0156d8c347fb9aa8a6f0f5c22677)). 100ms TTML, 연속 짧은 cue, 수동/자동 tail 및 preview/export 회귀 통과. 실제 사용자 프로젝트 검증은 미완료.
- [ ] TTML/LRC/SRT/VTT, 100ms 이하 cue, 연속 짧은 cue, 반복 문구, 중첩·동일 시작/종료, 공백, 다중 줄·번역·줄바꿈을 합성 fixture로 검증한다.
- [ ] 오디오 시간축, 가사 활성 구간, 스크롤, 강조 해제 및 단어 효과의 시계를 분리한다. 마지막 cue와 다음 곡 전환에서 갑작스러운 위치 점프가 없어야 한다.
- [ ] seek/reverse seek, pause/resume, 품질 변경, AutoMix/Gapless 경계에서 같은 시각은 같은 상태를 반환해야 한다.
- [ ] preview/FFmpeg/Rust GPU의 동일 시간·출력 설정에서 가사 상태·픽셀/PTS를 비교한다. 엔진별 미지원 기능을 조용히 누락하지 않는다.

주요 경계: `app/preview/frame_state.py`, `app/preview/canvas_snapshot.py`, `app/canvas/renderers/lyrics_renderer.py`, `app/animation/lyrics.py`, `app/timeline/`, `app/renderer/export_timeline.py`.

완료 조건: 신규 회귀 및 기존 가사/시간축/렌더링 검사 성공, 실제 Windows에서 문제 구간 재생 및 전체 곡 전환 확인. 사용자 프로젝트·음원·가사 전문은 저장소에 넣지 않는다.

### Phase 2 / P0: 프로젝트 데이터 보호 및 복구

2026-11-29 KST 진행 기록 (main `01043d0848a1125788f492227fd833594e241c59`). 아래 표의 CI는 병합 전 정확한 PR HEAD 기준이다. 실기기 수용 검수는 완료되지 않았다.

| PR | 구현 및 자동 검증 | 병합 상태 |
| --- | --- | --- |
| [#257](https://github.com/tharu8813/Music-Motion-Studio/pull/257) | 프로젝트/복구 임시 파일 fsync, 마지막 정상 파일 보존, 저장 중 미디어 변경 감지. [CI 9/9](https://github.com/tharu8813/Music-Motion-Studio/actions/runs/46153539494) | `8c2da2d` |
| [#258](https://github.com/tharu8813/Music-Motion-Studio/pull/258) | 협력 편집기 저장 잠금, 파일 revision 비교, 충돌 시 별도 저장/취소, 편집 및 복구 데이터 보존. [CI 9/9](https://github.com/tharu8813/Music-Motion-Studio/actions/runs/46156127317) | `08dd42d` |
| [#259](https://github.com/tharu8813/Music-Motion-Studio/pull/259) | 복구 JSON 32MiB 읽기/쓰기 한도, 손상 격리, 원본 revision 및 업데이트 handoff 보존. [CI 9/9](https://github.com/tharu8813/Music-Motion-Studio/actions/runs/46157038340) | `012ed77` |
| [#260](https://github.com/tharu8813/Music-Motion-Studio/pull/260) | 여러 복구 세션 선택, 취소·거절 시 파일 보존, 단일 자동 복구 유지. [CI 9/9](https://github.com/tharu8813/Music-Motion-Studio/actions/runs/46157875595) | `abc5fa5` |
| [#261](https://github.com/tharu8813/Music-Motion-Studio/pull/261) | 읽을 수 없는 복구 파일 경로 및 원인 표시, 정상 복구 지속. [CI 9/9](https://github.com/tharu8813/Music-Motion-Studio/actions/runs/46158758179) | `97d73c4` |
| [#262](https://github.com/tharu8813/Music-Motion-Studio/pull/262) | 정상 ZIP entry의 부분 복구, reference 프로젝트 사본·보고서 생성, 원본 및 기존 폴더 보호. [CI 9/9](https://github.com/tharu8813/Music-Motion-Studio/actions/runs/46159697800) | `01043d0` |
| [#263](https://github.com/tharu8813/Music-Motion-Studio/pull/263) | 실패 화면에서 부분 복구 작업자·취소·결과 안내 연결. 최초 CI의 Qt exec 테스트 모킹 오류 수정 및 종료 race 방어. | `53f9a06` 재검증 필요, 미병합 |

- [x] 임시 파일 flush/fsync와 실패 시 이전 정상 파일 보존의 자동 검사, 저장 중 미디어 변경 검증.
- [x] 협력 편집기 저장 잠금 및 원본 revision 비교, 충돌 후 복사 저장/취소 회귀.
- [x] 복구 파일 크기/JSON 검증, 원본 revision 및 업데이트 인계, 복구 선택과 손상 진단, 정상 ZIP entry 부분 복구 서비스.
- [ ] #263의 최신 HEAD CI 및 병합, 실제 대용량 복구·취소 및 복구 폴더 참조 유지 검증.
- [ ] 실제 Windows 두 프로세스에서 동일 프로젝트 및 복구 세션을 열고 저장/복구 경쟁, 잠긴 파일·읽기 전용·디스크 부족·강제 종료·임시 파일 정리 검수. 특히 활성 복구 세션 소유권 및 복구 목록 선택 도중 파일 변경/삭제 경계는 별도 확인한다.
- [ ] 구버전 프로젝트/프리셋/언어팩과 embed/reference, 누락 미디어 재연결, 자동 저장 세대 및 worker 수명에 대한 실제 사용자 흐름 검수.

제한: fsync는 모든 파일 시스템/전원 장애에서 디렉터리 메타데이터 보존을 보장하지 않는다. revision은 메타데이터 비교이며 비협력 외부 프로그램에 대한 원자적 compare-and-swap은 아니다. 32MiB 초과 기존 복구 파일은 삭제하지 않으나 일반 복구로 열 수 없다. 부분 복구는 읽을 수 있는 ZIP 중앙 디렉터리와 CRC-valid entry를 전제로 하며 손상 manifest를 재구성하지 않는다. 복구 프로젝트는 reference 모드로 생성되어 미디어 폴더를 함께 보관해야 한다.

Phase 3의 독립적인 계측 및 성능 검토는 병행할 수 있다. Phase 2 전체 완료와 실제 사용자 환경 검수는 남은 체크박스 확인 후에만 선언한다.

경계: `app/services/project_service.py`, `autosave_service.py`, `project_session.py`, `app/controllers/project_controller.py`, `autosave_controller.py`.

완료 조건: 실패 주입 후 기존 파일/편집 내용 보존, 다중 창 충돌 회귀, Windows 실제 저장·복구 검수.

### Phase 3 / P1: 미리보기 및 오디오 준비 최적화

- [x] #249/#250 병합. 재생 필수 준비 중 선택 분석 지연 및 준비 완료/실패 폴백 시 재개, 단계별 계측 도구 반영. 실제 성능 개선 여부는 별도.
- [ ] 기존 #243~#248 포함 전체 오디오 준비·캐시 정확성, 실제 프로젝트 및 장기 동작 검증.
- [ ] cold/warm 준비 시간, 첫 재생 가능 시점, 캐시 적중 및 무효화 조건을 동일 fixture로 측정한다. 정규화·오디오 샘플·Gapless 경계의 정확성을 보존한다.
- [ ] 절전/균형/최상 품질의 정책을 유지하며 최신 프레임 우선, CPU paint/texture upload/디코더 압력과 A/V 오차를 진단한다.
- [ ] 다중 영상/VFR·회전·프록시·탐색·전체화면/Alt+Tab·GPU 실패 후 CPU 폴백을 검증한다.
- [ ] 60초 이상 장기 재생 및 반복 개폐에서 RSS, 작업자·프로세스, 텍스처·캐시 해제 여부를 검사한다. 단일 RSS 증가만으로 누수를 단정하지 않는다.

#### #250 Windows 합성 벤치마크 (2026-11-25)

[실행 로그](https://github.com/tharu8813/Music-Motion-Studio/actions/runs/45536823520/job/138945364643): 8곡 × 60초, WAV/FLAC/MP3/M4A, 각각 cold/warm 3쌍. 실제 첫 소리가 아니라 연속 FLAC으로 Play가 활성화되는 시간의 중앙값이다.

| fixture | cold 준비 | warm 준비 | loudness | FLAC encode |
| --- | ---: | ---: | ---: | ---: |
| Pink noise (linear) | 7.658초 | 0.379초 | 2.221초 | 1.016초 |
| Peaky (limiter) | 10.461초 | 0.342초 | 2.063초 | 3.901초 |

각 fixture의 cold/warm 디코딩 PCM SHA-256 및 재생 길이가 일치하고 warm에서 렌더가 재실행되지 않았다. limiter가 포함된 FLAC 단계의 비용이 큰 관찰값이지만 서로 다른 합성 입력이므로 limiter만의 인과 효과로 단정하지 않는다. #250은 계측 추가이며 런타임 가속이 아니다. 다음 후보는 동일 입력에서 limiter/인코딩 정책을 비교하고 정규화·true peak·PCM 정확성을 유지해야 한다.

경계: `app/dialogs/export_preview_dialog.py`, `app/controllers/preview_controller.py`, `app/preview/`, `app/video/`. 세부 단계와 기준은 [#159](https://github.com/tharu8813/Music-Motion-Studio/issues/159)를 따른다.

완료 조건: 같은 품질·환경 A-B-B-A, A/V와 프레임 P95·RSS 등 측정, 실제 Windows 장기 재생 및 오류 복구 성공. 미리보기 품질이 최종 출력 설정을 바꾸면 안 된다.

### Phase 4 / P1: FFmpeg 및 Rust GPU 내보내기 안정성·성능

기존 RTX 5060 기록(2026-11-14, 1080p60/30초/1,800프레임): CLI 44.557초, native 42.09fps, Qt capture 29.002초(상태 준비 8.700초/paint 19.291초), SHA-256 CPU 19.524초, Rust 패킷 디코딩 14.255초, NVENC 3.833초. 중첩 타이머이므로 합산 금지. 최신 main 측정값이 아니다.

- [ ] 대표 프로젝트 2종 이상에서 동일 조건으로 기준/후보 A-B-B-A를 측정한다.
- [ ] Qt 가사·텍스트 raster/state 비용, 안전한 glyph/레이어 캐시, 해시·전송 비용, static layer 재사용을 우선 계측한다. 캐시 정확성·메모리 한도를 보존한다.
- [ ] 장기 GPU 텍스트 렌더링은 CPU 병목 및 지원 기능의 근거가 있을 때 별도 후보로 개발한다. 검증되지 않은 GPU 효과로 기존 구현을 대체하지 않는다.
- [ ] FFmpeg/Rust GPU의 실제 전체 프로젝트 출력, 준비/인코딩 중 취소, 실패·재시도, 출력 파일 충돌·임시 파일·프로세스 정리를 검증한다.
- [ ] 프레임 수·PTS·디코딩된 영상 및 PCM 오디오, 시각적 효과·Z 순서·글꼴·가사·파티클·동적 영상 누락 여부를 비교한다.

기존 alpha trim 실험은 전송량 22.61% 감소에도 wall time 15.68% 증가하여 자동 모드 승격을 거부했다. [판정 기록](architecture/2026-11-14-p1-abba-verdicts.md), [측정 원본 설명](architecture/2026-11-14-alpha-trim-abba.md), [프레임 전송 계약](NATIVE_FRAME_TRANSPORT.md)을 보존한다.

완료 조건: 화질/시간축 동등성과 성능 개선을 별도 판정하고, 실제 RTX 환경 및 두 엔진의 내보내기/취소 검수 성공. 속도 개선이 입증되지 않은 후보는 기본값으로 승격하지 않는다.

### Phase 5 / P1: AI 가사 번역 안정화

- [ ] #238/#239 처리 후 Ollama/LM Studio/NMT의 모델 설치·삭제·선택·언어 방향·서버 재연결을 검증한다. 다른 모델을 성공으로 오인하거나 임의 선택하지 않는다.
- [ ] 2단계 번역의 문맥 유지·반복 가사·힙합/슬랭·중의적 표현을 고정 평가 fixture에서 비교한다. 기존 승인 번역을 임의 덮어쓰지 않는다.
- [ ] 출력 잘림/finish_reason, 잘못된 번역 불필요 응답, 긴 입력, 제한된 재시도·취소·부분 성공·네트워크 복구를 검증한다.
- [ ] 기존 Windows DPAPI 저장 경계, API 인증 정보 및 크래시/로그 비밀 정보 노출 여부를 검사한다.

완료 조건: 모델 선택·설치 상태와 실제 결과 일치, 취소/실패 시 기존 번역 보존, 로컬/클라우드 모드 회귀 및 평가 결과 기록. 단일 모델 출력만으로 번역 품질 향상을 단정하지 않는다.

### Phase 6 / P1: 코드 책임 분리

기준 파일 크기(십진 KB): main_window 270.3, export_preview_dialog 216.0, source_inspector 140.4, ffmpeg_renderer 118.0, export_controller 111.0, source_item 104.2, canvas_snapshot 100.8. 크기는 우선 조사 지표이지 버그/성능 자체의 증거가 아니다.

- [ ] 기존 controller/service 경계를 활용해 미리보기 오디오 준비·작업자 관리·프로젝트/UI adapter·내보내기 세션을 작은 단위로 분리한다.
- [ ] 시간축·가사 상태·animation 계산 등 순수 도메인 책임을 UI와 분리한다. 저장 스키마·공개 계약·동작을 유지한다.
- [ ] Qt 객체/스레드 소유권, signal 연결·해제, 작업 취소 및 오류 처리의 중복을 줄인다.
- [ ] 기존 테스트와 의존성 검사를 활용하고 각 작은 PR에서 동작 등가성을 확인한다. 대형 일괄 리라이트/불필요한 추상화를 피한다.

### Phase 7 / P2: UI/UX 완성도

- [ ] 기존 기능과 실제 화면을 먼저 확인하여 중복 구현을 피한다.
- [ ] 속성 검색·최근 설정·기본값 복원·다중 소스 편집, 가사 타이밍 경고·중첩 표시·원본/번역 비교·구간 반복 재생의 필요성을 검토한다.
- [ ] 미리보기 준비 단계·실제 품질/프레임 정보, 내보내기 사전 검사·단계별 진행·해결 가능한 오류 안내를 개선한다.
- [ ] 복구 선택, 최근 프로젝트 상태, 미사용 콘텐츠/저장 공간 안내, 한국어·영어 현지화 및 키보드 접근성을 검토한다.

완료 조건: 사용 흐름별 실제 화면 검증과 기존 프로젝트/프리셋 호환성 유지. UI 구조 및 애니메이션 변경은 렌더링 시간축과 독립한다.

### Phase 8 / 배포 게이트 (P0, 모든 단계에 적용)

- [ ] 필수 Windows 빌드/격리 Python/관련 native·Rust·실제 미디어 CI가 최신 HEAD에서 성공. 관련 리뷰와 충돌 검토 완료.
- [ ] 실제 Windows 11 일반 사용자 환경에서 개발 도구 없이 새 설치/이전 공개 버전 업그레이드/제거, .pvsproj 파일 연결, 설정·프리셋·복구 보존을 확인한다.
- [ ] 자동 업데이트 다운로드 SHA-256/크기 검증, 취소·오류·재실행·작업 복구 및 중복 설치를 검증한다. 코드 서명/공급망 인증의 필요성은 별도 평가한다.
- [ ] RTX 실제 PC에서 FFmpeg 및 Rust GPU 1080p60 전체 출력/중간 취소, ffprobe 결과와 음성·화면 검수.
- [ ] 모델 파일·라이선스·무결성·언어/도움말 리소스를 보존한다. [패키징 안내](../PACKAGING.md)와 [릴리즈 노트 원칙](RELEASE_NOTES_GUIDELINES.md)을 따른다.
- [ ] 릴리즈 승인 전 기준 SHA, OS/GPU/드라이버, 입력 조건, 명령, CI/실측 결과, 제한 사항을 남긴다. 자동 CI 성공과 소비자 PC 수용 검수를 구분한다.

## 3. 진행 관리 및 작업 원칙

P0는 사용자 데이터·동작 정확성·배포를 막는 위험 또는 선행 작업, P1은 주요 안정성/성능/유지보수, P2는 편의 및 후속 개선이다. 모든 항목이 확인된 버그라는 뜻은 아니다. 각 작업은 확인된 문제/개선 가설/미검증 요구를 구분한다.

1. 최신 main, 관련 열린 PR·이슈, 정확한 HEAD의 검사 및 리뷰를 확인한다.
2. 하나의 책임 단위로 수정하고 재현 가능한 테스트를 추가한다. 사용자 데이터·인증 정보·미디어를 커밋하지 않는다.
3. CI가 실패·진행·누락이면 원인을 기록하고 병합하지 않는다. 관련 테스트 및 실기기 검증이 필요하면 별도로 남긴다.
4. 완료된 항목은 해당 PR/병합 SHA/검증 링크와 함께 체크한다. 측정 도구 추가나 테스트 작성만으로 실제 성능 개선을 완료 처리하지 않는다.
5. 날짜별 실험 보고서는 중요한 계약·재현 근거만 유지하고, 최신 상태는 이 문서와 추적 이슈에서 관리한다.

관련 문서: [문서 안내](README.md), [설계·측정 이력](architecture/README.md), [전체 리뷰 #136](https://github.com/tharu8813/Music-Motion-Studio/issues/136), [미리보기 #159](https://github.com/tharu8813/Music-Motion-Studio/issues/159).
