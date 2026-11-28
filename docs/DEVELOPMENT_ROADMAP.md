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

2026-11-28 조회 결과이며 변경 시 최신 HEAD와 필수 CI를 다시 조회한다.

| PR | 내용 | 기준 시점 | 후속 작업 |
| --- | --- | --- | --- |
| [#251](https://github.com/tharu8813/Music-Motion-Studio/pull/251) | Cascade 짧은 가사 및 후반 이동 지연 | native packet 성공, Windows/분리 Python 진행 중 | 100ms TTML, 연속 가사, tail 및 preview/export 회귀 확인 |
| [#250](https://github.com/tharu8813/Music-Motion-Studio/pull/250) | Gapless 단계 및 true-peak limiter 계측 | 4/4 성공 | 측정 결과·리뷰 확인; 런타임 최적화로 간주하지 않음 |
| [#249](https://github.com/tharu8813/Music-Motion-Studio/pull/249) | 재생 필수 오디오 우선, 선택 분석 지연 | 3/3 성공 | 최신 main 재검증, 실제 준비 속도 계측 |
| [#239](https://github.com/tharu8813/Music-Motion-Studio/pull/239) | 로컬 NMT 모델 명시 선택 보존 | 3/3 성공 | #238 선행, 최신 main 반영 후 재검증 |
| [#238](https://github.com/tharu8813/Music-Motion-Studio/pull/238) | 로컬 LLM 설치 확인 | 2/3 성공 | LM Studio 기존 모델 선택 회귀 원인 수정 |

#238의 [실패 로그](https://github.com/tharu8813/Music-Motion-Studio/actions/runs/44570421557/job/135723733613): `test_lmstudio_unchanged_installed_list_is_not_download_success`에서 선택이 없어야 하나 `old-model.gguf`가 반환됨. 테스트 실패는 확인했으나 실제 UI의 원인과 올바른 선택 정책은 수정 시 다시 검토한다.

- [ ] 모든 열린 PR의 변경 범위·충돌·리뷰·최신 HEAD 필수 CI를 확인한다. #238 → #239 순서를 준수한다.
- [ ] CI 실패·대기·누락 상태에서는 병합하지 않는다. 기존 main 변경을 보존하고 병합 후 결과를 확인한다.

## 2. 단계별 계획

### Phase 1 / P0: 가사 및 시간축 안정화

- [ ] #251 검증 및 병합. 원본 타임스탬프/가사 데이터 수정 없이 해결한다.
- [ ] TTML/LRC/SRT/VTT, 100ms 이하 cue, 연속 짧은 cue, 반복 문구, 중첩·동일 시작/종료, 공백, 다중 줄·번역·줄바꿈을 합성 fixture로 검증한다.
- [ ] 오디오 시간축, 가사 활성 구간, 스크롤, 강조 해제 및 단어 효과의 시계를 분리한다. 마지막 cue와 다음 곡 전환에서 갑작스러운 위치 점프가 없어야 한다.
- [ ] seek/reverse seek, pause/resume, 품질 변경, AutoMix/Gapless 경계에서 같은 시각은 같은 상태를 반환해야 한다.
- [ ] preview/FFmpeg/Rust GPU의 동일 시간·출력 설정에서 가사 상태·픽셀/PTS를 비교한다. 엔진별 미지원 기능을 조용히 누락하지 않는다.

주요 경계: `app/preview/frame_state.py`, `app/preview/canvas_snapshot.py`, `app/canvas/renderers/lyrics_renderer.py`, `app/animation/lyrics.py`, `app/timeline/`, `app/renderer/export_timeline.py`.

완료 조건: 신규 회귀 및 기존 가사/시간축/렌더링 검사 성공, 실제 Windows에서 문제 구간 재생 및 전체 곡 전환 확인. 사용자 프로젝트·음원·가사 전문은 저장소에 넣지 않는다.

### Phase 2 / P0: 프로젝트 데이터 보호 및 복구

현재 임시 파일 작성 후 교체, 세션별 자동 복구, 변경 serial, ZIP manifest/썸네일 제한, 여유 디스크 검사와 캐시 CRC 검증이 구현되어 있다. 다음은 보완 후보이며 기존 데이터 손실이 확인되었다는 뜻이 아니다.

- [ ] 원자적 저장과 디스크 flush/fsync 내구성을 파일 시스템·Windows 동작에 맞춰 검증한다. 실패 시 마지막 정상 파일을 보존하고 오류를 사용자에게 표시한다.
- [ ] 동일 프로젝트를 여러 창/프로세스에서 편집할 때 외부 변경과 저장 충돌을 감지하고 덮어쓰기 전에 선택권을 제공한다.
- [ ] 복구 파일 목록/시간/프로젝트 식별 표시, 손상 복구·여러 세션·취소·복구 후 재저장 경계를 개선한다.
- [ ] 복구 JSON 용량 한도, 디스크 부족, 읽기 전용, 잠긴 파일, 저장 도중 미디어 변경, 비정상 종료, 임시 파일 정리를 검사한다.
- [ ] 구버전 프로젝트/프리셋/언어팩과 embed/reference, 누락 미디어 재연결, 자동 저장 세대 및 worker 수명을 유지한다.

경계: `app/services/project_service.py`, `autosave_service.py`, `project_session.py`, `app/controllers/project_controller.py`, `autosave_controller.py`.

완료 조건: 실패 주입 후 기존 파일/편집 내용 보존, 다중 창 충돌 회귀, Windows 실제 저장·복구 검수.

### Phase 3 / P1: 미리보기 및 오디오 준비 최적화

- [ ] #249/#250와 기존 #243~#248의 오디오 준비·캐시 작업을 정리한다. 재생 필수 준비와 선택적 파형/FFT/비트 분석을 분리한다.
- [ ] cold/warm 준비 시간, 첫 재생 가능 시점, 캐시 적중 및 무효화 조건을 동일 fixture로 측정한다. 정규화·오디오 샘플·Gapless 경계의 정확성을 보존한다.
- [ ] 절전/균형/최상 품질의 정책을 유지하며 최신 프레임 우선, CPU paint/texture upload/디코더 압력과 A/V 오차를 진단한다.
- [ ] 다중 영상/VFR·회전·프록시·탐색·전체화면/Alt+Tab·GPU 실패 후 CPU 폴백을 검증한다.
- [ ] 60초 이상 장기 재생 및 반복 개폐에서 RSS, 작업자·프로세스, 텍스처·캐시 해제 여부를 검사한다. 단일 RSS 증가만으로 누수를 단정하지 않는다.

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
