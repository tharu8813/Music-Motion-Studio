# Music Motion Studio 문서 안내

이 폴더는 **현재 유지보수에 필요한 설명, 구현 계약, 검증 지침, 릴리즈 자료**를 중심으로 관리합니다. 날짜가 붙은 문서와 측정 결과는 해당 시점의 기록이며, 최신 코드 동작을 보장하지 않습니다.

## 먼저 읽을 문서

| 주제 | 문서 |
| --- | --- |
| 사용자 안내·실행 | [저장소 README](../README.md), [패키징 안내](../PACKAGING.md) |
| Studio UI | [공통 디자인 기준](STUDIO_DESIGN.md) |
| 가사 검색·편집 | [가사 검색](LYRICS_SEARCH.md), [TTML 수정 안전성](architecture/2026-10-28-ttml-edit-safety.md), [가사 번역](architecture/2026-11-01-lyrics-translation.md) |
| 키프레임·모션 | [키프레임 시스템](architecture/2026-11-02-keyframe-system.md), [모션 편집기 통합 기록](architecture/2026-11-03-motion-editor-completion.md) |
| AutoMix | [음향 기반 전환](automix-acoustic-search.md), [음악 컨텍스트](automix-music-context.md), [재생 구조](architecture/2026-10-15-automix-playback.md) |
| GPU 내보내기 | [프레임 전송 계약](NATIVE_FRAME_TRANSPORT.md), [프로젝트 런타임 계약](NATIVE_PROJECT_RUNTIME.md), [Rust GPU 엔진 기술 기록](architecture/rust-gpu-export-core.md), [Affine v2 계약](architecture/native-affine-v2.md) |
| 성능·검증 | [프로세스 단위 GPU 계측](PROCESS_SCOPED_GPU_PROFILING.md), [아키텍처 성능 기록](architecture/performance.md), [출력 시각적 일치 검증](architecture/2026-11-12-native-visual-fidelity.md), [릴리즈 안정화 점검](architecture/2026-11-12-release-stabilization-audit.md) |
| 릴리즈·현지화 | [릴리즈 노트 작성 원칙](RELEASE_NOTES_GUIDELINES.md), [버전별 릴리즈 노트](releases/), [릴리즈 노트 데이터](release-notes/README.md), [언어팩](language-packs/README.md) |

## 오래된 기록과 작업 지침

2026-11-14 문서 정리에서는 완료된 단계별 AutoMix 프롬프트, 오래된 에이전트 인수인계, 중복된 기능별 미세 수정 일지, 해당 일지에만 딸린 일회성 benchmark 산출물을 작업 트리에서 정리했습니다. 삭제는 Git 이력에서 되돌릴 수 있으며 구현 코드·테스트·릴리즈 이력은 삭제하지 않았습니다.

정리 직전 기준 커밋: `d178babf4534667282a6a8c3471f0ca94a136fd8`. 필요한 과거 자료는 다음과 같이 확인할 수 있습니다.

```bash
git show d178babf4534667282a6a8c3471f0ca94a136fd8:docs/AGENT_HANDOFF.md
git ls-tree -r --name-only d178babf4534667282a6a8c3471f0ca94a136fd8 docs/
```

**보존 기준**

- 실제 코드나 도구가 참조하는 근거(예: AutoMix 의존성 평가, 청취 검증 절차), 구현 계약과 회귀 방지용 검증 방법은 유지합니다.
- 릴리즈 노트, 언어팩 예제, README 이미지, 중요한 설계 결정은 유지합니다.
- 단발성 명령 실행 결과, 이미 완료된 단계별 지시서, 과거 브랜치/환경에만 유효한 인수인계는 기본적으로 Git 히스토리와 PR 기록을 이용합니다.
- 새 측정 데이터는 재현 명령, 측정 조건, 해석, 유지할 이유를 함께 기록하고 다른 문서에서 참조할 때만 저장소에 남깁니다.
- 오래된 설계 기록에 현재 동작을 단정하는 문구를 추가하지 마세요. 최신 상태는 코드·테스트·CI·실제 Windows 검증을 다시 확인해야 합니다.
