# Windows Codex와 AgentCraft 스킬 환경 진단

조사 기준: 2026-11-05, Juniper의 현재 AgentCraft Codex 세션. 로컬 파일과 이 세션에 실제 제공된 스킬 목록을 비교했다. Windows Codex 앱의 실행 중 목록은 직접 조회하지 않았으므로 앱과의 완전 일치를 입증한 결과는 아니다.

## 결론

기존 스킬 폴더는 **이미 공유되고 있다**. 조사한 사용자·시스템 스킬 `SKILL.md` 27개 가운데 현재 제공 목록은 25개다. `hatch-pet`은 사용자 설정에서 명시적으로 비활성화되어 있고, `.system/review-agent`는 파일은 있지만 현재 제공 목록에는 없다. 후자의 누락 원인은 확인되지 않았다.

스킬 파일을 복사할 필요는 없다. 다만 스킬은 사용 지침이며 실행 도구를 설치하거나 연결하지 않는다. AgentCraft는 별도 정책으로 이미지 생성, 웹 검색, 사용자 MCP·플러그인 등을 끄므로, 목록에 같은 스킬이 있어도 Windows 앱과 같은 작업이 가능한 것은 아니다. 경로 공유, 활성화 상태, 세션 노출, 실행 의존성을 따로 점검해야 한다.

## 출처와 활성 상태

이 문서에서 `~`는 `C:/Users/wlqor`다. 아래 경로는 모두 실제 읽은 원본이며, 파일을 수정하지 않았다.

| 출처 | 디스크의 스킬 수 | 현재 제공 수 | 활성 상태와 유지 방법 |
| --- | ---: | ---: | --- |
| `~/.codex/skills/<이름>/SKILL.md` | 3 | 2 | graphify·ponytail 노출, hatch-pet 명시 비활성. 기존 폴더를 그대로 유지 |
| `~/.codex/skills/.system/<이름>/SKILL.md` | 5 | 4 | review-agent 미노출, 나머지 노출. 시스템 원본을 별도 복사하거나 임의 수정하지 않음 |
| `~/.agents/skills/<이름>/SKILL.md` | 19 | 19 | 전부 노출. 기존 설치·업데이트 위치를 계속 사용 |
| `~/.codex/config.toml`의 `[[skills.config]]` | 설정 1개 | 해당 스킬 미노출 | hatch-pet의 절대 `SKILL.md` 경로에 `enabled = false` |

설정 항목이 없는 스킬을 명시적으로 `enabled = true`인 것으로 표현하지 않았다. 이 경우 활성 상태의 근거는 현재 목록에 실제 노출되었다는 관찰이다. 조사한 세 폴더에서 이름 중복은 없었다. `design-taste-frontend`와 `design-taste-frontend-v1`은 서로 다른 이름의 버전이므로 중복 제거 대상이 아니다. 이 폴더들에는 링크 대상이 표시되지 않았으며, 추가 junction·수동 사본은 필요하지 않다.

### 전체 대조표

`C`는 `~/.codex/skills`, `S`는 `~/.codex/skills/.system`, `A`는 `~/.agents/skills`를 뜻한다. 각각 `<출처>/<이름>/SKILL.md`가 원본 경로다.

| 이름 | 출처 | 현재 제공 | 상태 / 주요 실행 조건 |
| --- | --- | --- | --- |
| graphify | C | 예 | Python·graphifyy 필요. 그래프 생성과 분석은 별도 실행이며 설치 여부 미검증 |
| hatch-pet | C | 아니오 | 설정에서 명시 비활성. 아래 별도 설명 참고 |
| ponytail | C | 예 | 지침 중심, 별도 전용 런타임 요구 없음 |
| imagegen | S | 예 | 내장 `image_gen` 또는 명시 동의한 CLI 경로 필요 |
| openai-docs | S | 예 | 공식 문서 검색·조회 도구 필요. 현재 AgentCraft는 웹 검색 금지 |
| review-agent | S | 아니오 | 명시 비활성 항목은 발견하지 못함. 원인 미확정 |
| skill-creator | S | 예 | 지침과 동봉 스크립트의 실행 조건을 실제 사용 시 확인 |
| skill-installer | S | 예 | 설치 작업에는 네트워크·대상 폴더 쓰기 권한이 별도 필요 |
| brandkit | A | 예 | 이미지 생성 도구 필요 |
| design-taste-frontend | A | 예 | 대상 프런트엔드의 기존 의존성 확인 필요 |
| design-taste-frontend-v1 | A | 예 | 대상 프런트엔드의 기존 의존성 확인 필요 |
| find-skills | A | 예 | 검색·설치 경로는 `npx skills`, Node/npm 및 네트워크 필요 |
| full-output-enforcement | A | 예 | 출력 지침 중심 |
| gpt-taste | A | 예 | UI 구현 지침, 사용 시 프로젝트의 GSAP 등 의존성 확인 |
| high-end-visual-design | A | 예 | UI 구현 지침 중심 |
| hyperframes | A | 예 | HyperFrames CLI 필요 |
| hyperframes-cli | A | 예 | 스킬 명시 조건: Node.js 22+와 FFmpeg |
| hyperframes-registry | A | 예 | HyperFrames CLI의 catalog/add 등 필요 |
| image-to-code | A | 예 | 선행 디자인 이미지 생성 도구 필요 |
| imagegen-frontend-mobile | A | 예 | 이미지 생성 도구 필요 |
| imagegen-frontend-web | A | 예 | 이미지 생성 도구 필요 |
| industrial-brutalist-ui | A | 예 | UI 구현 지침 중심 |
| media-use | A | 예 | HyperFrames CLI, 작업별 HeyGen 또는 로컬 도구. 로컬 음성 경로는 Python 3.10+ 및 추가 패키지 조건 |
| minimalist-ui | A | 예 | UI 구현 지침 중심 |
| redesign-existing-projects | A | 예 | 기존 UI 감사·구현 지침 중심 |
| reverse-engineer-anything | A | 예 | REA MCP 또는 지원 CLI와 작업별 분석 엔진 필요. 스킬 설치만으로 MCP 등록되지 않음 |
| stitch-design-taste | A | 예 | 지침 사용과 별개로 Stitch 도구 연결은 선택적 조건 |

위 실행 조건은 스킬 원문의 요구 사항이며 설치·인증·실행 성공을 확인한 표가 아니다. 지침 중심 스킬도 실제 프로젝트 작업에는 해당 프로젝트의 빌드 도구가 필요할 수 있다.

### hatch-pet은 왜 보이지 않는가

원본은 `~/.codex/skills/hatch-pet/SKILL.md`에 있다. `~/.codex/config.toml`에는 다음 의미의 설정이 존재한다.

```toml
[[skills.config]]
path = 'C:\Users\wlqor\.codex\skills\hatch-pet\SKILL.md'
enabled = false
```

따라서 구성상 명시적인 비활성화다. 누가 언제 어떤 이유로 비활성화했는지는 확인하지 않았다. Windows와 같은 설정을 유지하려면 이를 임의로 켜면 안 된다.

나중에 사용자가 활성화를 선택하더라도 원문은 `$imagegen`, `load_workspace_dependencies`가 반환하는 정확한 Python 실행 파일과 Pillow 포함 런타임을 요구한다. 번들 런타임이 없으면 일반 시스템 Python으로 대체하지 말라고 명시한다. 현재 세션에서는 `load_workspace_dependencies`와 내장 이미지 생성 도구가 제공되지 않는다. 단순히 `enabled = true`로 바꾸거나 스킬을 복사하는 것으로 실행 조건이 충족되지 않는다.

## AgentCraft가 발견을 위임하는 위치와 별도 제한

조사한 소유 저장소 후보는 `C:/Users/wlqor/Desktop/agentcraft`다. 루트 및 하위 `AGENTS.md`는 이번 재귀 파일 조사에서 발견하지 못했다. 실행 지침은 다음 코드와 문서에 있었다.

| 근거 파일 | 관찰한 동작 |
| --- | --- |
| `start-agentcraft-codex.bat` | 이 AgentCraft 경로의 `tools/launch.ps1 -Backend codex`로 실행하며 대상 앱 저장소는 별도 지정 |
| `foreman/src/agents/codex/find.ts` | 명시 Codex 경로 → PATH → Windows Codex 앱 번들 순으로 실행 파일 선택. 따라서 같은 스킬 폴더여도 CLI 버전은 다를 수 있음 |
| `foreman/src/agents/codex/rpc.ts`의 `AppServer` | 전달된 환경과 cwd로 `codex app-server` 실행 |
| `foreman/src/agents/codex/engine.ts`의 `runTurn` | cwd를 넣어 `config/read`, `thread/start` 또는 `thread/resume` 호출. 자체 SKILL.md 스캐너를 구현하지 않고 Codex에 발견을 위임 |
| 같은 파일의 `threadConfig` | `skills.config`를 덮어쓰지 않음. 사용자 MCP·플러그인은 별도로 비활성 처리 |
| 같은 파일의 `OFF_FEATURES` | `image_generation`, `skill_search`, `skill_mcp_dependency_install`, apps/plugins 등 비활성 |
| 같은 파일의 `notes` | 웹 검색·하위 에이전트 사용 금지 등 AgentCraft 실행 지침 추가 |
| 같은 파일의 `serverEnv` 및 `foreman/README.md` | Windows app-server의 LOCALAPPDATA만 별도 경로로 격리. CODEX_HOME을 별도 스킬 복사 폴더로 바꾸는 코드는 조사한 경로에 없음 |

현재 제공 목록의 출처 경로가 공유 폴더를 직접 가리키는 것이 공유 동작의 실측 근거다. Codex 실행 파일 내부의 스킬 탐색 구현은 조사하지 않았다. `.system/review-agent`가 제공되지 않는 정확한 원인도 AgentCraft 코드만으로 단정할 수 없다. 코드에 별도 이름 필터는 발견하지 못했으나, 호스트·Codex 버전·시스템 스킬 제공 정책 등은 추가 확인 대상이다.

`skill_search`가 꺼져 있어도 이번 세션에 로컬 스킬 25개가 제공되므로, 이를 전체 로컬 스킬 로딩 차단으로 해석하면 안 된다. 반대로 스킬 목록에 `imagegen`이나 REA가 있다는 사실로 대응 실행 도구가 연결되었다고 해석해서도 안 된다.

## 최소 연결안과 유지 방법

1. 기존 `~/.codex/skills`, `~/.agents/skills`, `~/.codex/config.toml`을 단일 원본으로 유지한다. 별도 AgentCraft 스킬 사본·동기화 작업·자동 활성화는 추가하지 않는다.
2. 후속 점검은 AgentCraft 소유 저장소에서 구현한다. `foreman/src/agents/codex/engine.ts`의 app-server 초기화와 `find.ts`의 실행 파일 선택을 기준으로 **실효 CODEX_HOME, 선택된 CLI 경로·버전, 스킬 원본 경로, 비활성 여부, 세션 제공 여부**만 허용 목록으로 출력한다. config 전체·환경 전체·인증 파일을 출력하지 않는다.
3. 필요하면 동일 cwd와 동일 실행 파일을 쓰는 app-server의 지원 스킬 목록 API를 먼저 확인하고 읽기 조회를 추가한다. 원본 파일 존재와 세션 제공 여부를 분리해 비교하고, hatch-pet은 정상적인 비활성 상태로 보고한다. review-agent는 이유 미확정으로 남긴다. 지원 API 스키마는 이번 조사에서 검증하지 않았다.
4. 버전 차이가 확인되면 기존 `--codex-path` / `AGENTCRAFT_CODEX_PATH` 설정 지점을 활용해 같은 Codex 실행 파일을 선택하는 검토안을 만든다. 공유 경로가 이미 같다면 새 경로 설정 기능을 만들 필요가 없다.
5. 도구 제한 변경은 스킬 공유와 별도 검토한다. 이미지 생성·플러그인·MCP를 일괄 활성화하거나 `OFF_FEATURES`를 통째로 제거하지 않는다. 작업별 호스트 지원·인증·런타임을 검증해야 한다. 이 부분은 t3의 도구 연결 진단과 함께 후속 구현에서 결정한다.
6. 원본 스킬을 기존 관리 방식으로 갱신한 뒤 새 AgentCraft 세션에서 목록과 실행 조건을 재확인한다. 살아 있는 스레드가 즉시 갱신되는지는 검증하지 않았다. 실행 경로·환경 연결이 바뀌면 Foreman 재시작 후 확인한다.

후속 구현 위치는 `C:/Users/wlqor/Desktop/agentcraft`의 검토 가능한 브랜치여야 한다. 이 playlist-project 앱의 기능 코드를 고쳐 해결할 사항은 아니다. 본 작업에서는 다른 저장소 수정, 사용자 전역 설정 변경, 패키지 설치, 런타임 다운로드, 비활성 기능 활성화를 하지 않았다.

## 검증 범위

세 출처의 직계 스킬 디렉터리에서 SKILL.md 존재와 이름을 대조하고, 현재 세션 제공 목록 25개와 비교했다. 관련 설정은 skills.config 항목만 근거로 기록했으며 민감 설정값·인증 정보는 문서에 포함하지 않았다. AgentCraft의 실행 파일 선택, 환경 전달, 스레드 설정과 지침 코드를 읽어 원인을 교차 확인했다.

문서만 추가하므로 앱 테스트나 스킬의 실제 생성·설치 작업은 실행하지 않았다. Windows 앱 실시간 목록, review-agent 미노출 이유, 개별 런타임 설치 상태와 실제 도구 인증은 미검증이다. 따라서 현재 결론은 **스킬 원본은 공유 중이고, 실행 기능은 아직 동일하지 않다**이다.
