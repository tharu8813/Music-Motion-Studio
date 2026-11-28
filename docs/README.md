# Music Motion Studio 문서 안내

현재 개발자가 먼저 찾아야 할 문서와 과거 검증 기록을 분리합니다. 날짜가 붙은 자료는 해당 시점의 기록이며 최신 코드의 동작을 보장하지 않습니다.

## 현재 개발 및 유지보수

| 주제 | 문서 |
| --- | --- |
| 개발 순서·완료 조건 | **[안정화 및 성능 개선 로드맵](DEVELOPMENT_ROADMAP.md)** |
| 사용자 안내·실행·패키징 | [저장소 README](../README.md), [패키징 안내](../PACKAGING.md), [개발 에이전트 지침](../AGENTS.md) |
| UI | [Studio 디자인 기준](STUDIO_DESIGN.md) |
| 가사 검색·편집 | [가사 검색](LYRICS_SEARCH.md), [TTML 수정 안전성](architecture/2026-10-28-ttml-edit-safety.md) |
| 가사 번역 | [번역 구조 기록](architecture/2026-11-01-lyrics-translation.md), [2단계 번역](architecture/2026-11-19-two-stage-lyrics-translation.md) |
| 키프레임·모션 | [키프레임 시스템](architecture/2026-11-02-keyframe-system.md), [모션 편집기 통합 기록](architecture/2026-11-03-motion-editor-completion.md) |
| AutoMix | [음향 기반 전환](automix-acoustic-search.md), [음악 컨텍스트](automix-music-context.md), [재생 구조](architecture/2026-10-15-automix-playback.md) |
| GPU 내보내기 | [프레임 전송 계약](NATIVE_FRAME_TRANSPORT.md), [프로젝트 런타임 계약](NATIVE_PROJECT_RUNTIME.md), [Affine v2 계약](architecture/native-affine-v2.md) |
| 성능·검증 | [GPU 계측](PROCESS_SCOPED_GPU_PROFILING.md), [설계·측정 이력](architecture/README.md), [벤치마크 판정 기준](architecture/2026-11-14-p1-abba-verdicts.md) |
| 오류·복구 | [크래시 리포트](CRASH_REPORTING.md), 로드맵 Phase 2 및 8 |
| 배포·릴리즈·현지화 | [릴리즈 노트 작성 원칙](RELEASE_NOTES_GUIDELINES.md), [버전별 릴리즈 노트](releases/), [릴리즈 노트 데이터](release-notes/README.md), [언어팩](language-packs/README.md) |

실시간 진행 상태는 [전체 리뷰 #136](https://github.com/tharu8813/Music-Motion-Studio/issues/136), [미리보기 #159](https://github.com/tharu8813/Music-Motion-Studio/issues/159), 열린 PR과 최신 CI에서 확인합니다. 오래된 일지의 체크 표시를 최신 검증 완료로 해석하지 않습니다.

## 역사적 자료

- [architecture/](architecture/README.md): 구현 결정, 날짜별 성능·회귀 검증, 통합 기록.
- [architecture/benchmarks/](architecture/benchmarks/): 재현 가능한 원시 계측 및 검증 결과.
- [automix-history/](automix-history/): 의존성 평가와 실제 청취·튜닝 절차.
- [releases/](releases/) 및 [release-notes/](release-notes/README.md): 배포 이력, 자동화 입력 및 사용자 변경 기록.

## 정리 기준과 복원

- 구현 계약, 실제 도구/코드 참조 자료, 재현 가능한 벤치마크 원본, 릴리즈 이력·이미지·언어팩·라이선스는 보존합니다.
- 단발성 작업 로그와 개인 PC/별도 저장소의 에이전트 환경 진단은 현재 제품 문서에서 제외합니다. 중요한 검증 조건과 결정은 통합 문서에서 추적합니다.
- 날짜별 자료를 수정할 때 과거 측정값을 최신 성능으로 바꾸거나 누락된 검증을 완료 처리하지 않습니다.
- 새 보고서는 기준 SHA, 재현 명령, 입력/환경 조건, 측정 결과, 해석 및 보존 이유를 포함해야 합니다.
- 파일 삭제는 Git 작업 트리에서의 정리입니다. Git 이력 삭제 또는 민감 정보의 영구 삭제를 의미하지 않습니다.

### 2026-11-28 정리

정리 직전 기준 커밋: [`e00b401`](https://github.com/tharu8813/Music-Motion-Studio/tree/e00b401be4efc60bee5a2ef5cbcef092ff8cf9c6/docs).

개인 환경에 한정된 세 문서를 제거했습니다. 외부 에이전트 제품의 설정과 연결 정책은 Music Motion Studio의 실행 계약이 아닙니다. 원본은 [환경 적용안](https://github.com/tharu8813/Music-Motion-Studio/blob/e00b401be4efc60bee5a2ef5cbcef092ff8cf9c6/docs/codex-environment-parity.md), [스킬 진단](https://github.com/tharu8813/Music-Motion-Studio/blob/e00b401be4efc60bee5a2ef5cbcef092ff8cf9c6/docs/codex-skill-parity.md), [도구 진단](https://github.com/tharu8813/Music-Motion-Studio/blob/e00b401be4efc60bee5a2ef5cbcef092ff8cf9c6/docs/codex-tool-parity.md)에서 필요할 때 확인합니다. 기존 읽기 전용 점검 스크립트와 테스트는 변경하지 않았습니다.

개별 최적화 작업 기록 네 개는 [설계·측정 이력](architecture/README.md#2026-11-14-개별-최적화-통합-기록)에 통합했습니다. 원본은 해당 커밋 링크에서 복구할 수 있습니다.

### 2026-11-14 이전 정리

완료된 단계별 AutoMix 프롬프트, 오래된 에이전트 인수인계, 중복된 기능별 미세 수정 일지 및 해당 일지에만 딸린 일회성 benchmark 산출물을 정리했습니다. 정리 직전 기준 커밋: `d178babf4534667282a6a8c3471f0ca94a136fd8`.

```bash
git show d178babf4534667282a6a8c3471f0ca94a136fd8:docs/AGENT_HANDOFF.md
git ls-tree -r --name-only d178babf4534667282a6a8c3471f0ca94a136fd8 docs/
```
