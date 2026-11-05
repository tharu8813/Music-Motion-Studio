# Windows Codex와 AgentCraft 도구 연결 진단

조사일: 2026-11-05. 대상: Windows의 Codex 데스크톱 설정과 현재 AgentCraft Kit 세션.

## 결론

현재 두 환경의 도구는 같지 않다. AgentCraft는 사용자 Codex 설정을 읽지만, 에이전트 스레드를 시작하거나 재개할 때 **모든 사용자 MCP와 플러그인을 명시적으로 끈다.** 브라우저·컴퓨터 조작·앱·이미지 생성 등의 기능도 별도로 끈다. 설정 파일을 공유하거나 복사하는 것만으로 해결되지 않는다.

공유 스킬의 지시문을 읽을 수 있는 것과 그 스킬이 요구하는 도구·앱 런타임을 사용할 수 있는 것은 별개다. 예를 들어 컴퓨터 조작 스킬은 `node_repl`이 필요하지만 현재 세션에는 해당 도구가 없다. 스킬 자체의 발견 경로와 비활성화 상태는 t2 조사 범위다.

이번 변경은 이 진단 문서뿐이다. 연결 구현은 t4에서 검토 가능한 적용안으로 이어간다. 외부 AgentCraft 소스, 사용자 전역 설정, 인증 정보와 보안 정책은 변경하지 않았다.

## 조사 범위와 안전한 근거

- `C:/Users/wlqor/.codex/config.toml`: 섹션·키 이름, 플러그인의 `enabled`, `node_repl`의 실행 파일 경로·빈 인자만 선별 출력했다. 환경 변수는 이름만 확인하고 값은 출력하지 않았다.
- `C:/Users/wlqor/.codex/plugins/cache`: 아래 활성 플러그인의 공개 manifest에서 이름·필드·상대 스킬 경로와 MCP 서버 구조만 확인했다. 캐시 존재를 활성화나 작동 증거로 간주하지 않았다.
- `C:/Users/wlqor/Desktop/agentcraft`: README, 설정 로더, Codex 엔진·RPC·도구·정책 코드, 관련 테스트와 시작 스크립트를 읽었다. 이 경로에서 `AGENTS.md`와 루트 `CLAUDE.md`는 발견되지 않았다. 현재 작업 지침은 세션에 전달된 AgentCraft 지침을 따랐다.
- 현재 세션의 도구 명세와 `ALL_TOOLS`를 비교했다. `node_repl`, 플러그인 전용 도구, 공식 OpenAI 문서 검색·조회 도구는 노출되지 않았다. 웹 검색은 수행하지 않았다.
- 토큰, `auth.json`, 세션 파일 내용·세션 DB는 읽거나 복사하지 않았다. 서버 실행, 플러그인 실행, 로그인, 네이티브 파이프 접속도 하지 않았다.

아래 판정은 로컬 코드와 현재 세션 관찰에 한정한다. Codex 데스크톱에서 각 도구를 실제 호출한 결과나 최신 공식 지원 범위를 확인한 것은 아니다.

## 현재 지원 상태

`지원`은 현재 세션에서 호출 가능함을 뜻한다. `미연결`은 설정·구성은 있으나 현재 세션에 연결되지 않음을 뜻한다. `호스트 전용`은 데스크톱 호스트 서비스나 UI가 필요한 경로로, 독립 Foreman에서 동등 지원이 확인되지 않은 경우다. `확인 불가`는 실행·인증·공식 문서 근거가 부족함을 뜻한다. 한 항목이 미연결이면서 호스트 의존일 수 있다.

| 항목 | 판정 | 현재 근거 / 한계 |
| --- | --- | --- |
| `exec_command`, `write_stdin`, `apply_patch`, `view_image` | 지원 | 현재 도구 명세에 존재. 명령·파일 변경은 기존 승인과 worktree 경계를 따른다. |
| AgentCraft `send_message`, `ask_user`, `read_memory`, `write_memory`, `report_status`, `list_tasks`, `update_task` | 지원 | 현재 세션에 노출. `agents/tools.ts:49`의 common 목록을 Codex dynamic tools로 전달한다. |
| `create_task`, `request_merge` | 호스트 역할 전용 | 같은 파일의 lead 목록. worker인 Kit의 누락은 오류가 아니다. |
| MCP 리소스 조회 도구 | 도구 인터페이스 지원 / 서버 연결 확인 불가 | 세션에 조회 명세가 있다는 사실만 확인. 이것을 `node_repl` 연결 증거로 볼 수 없다. |
| 공식 문서 검색, 웹 검색·앱 커넥터 | 미연결 | 공식 문서 도구 없음. 엔진은 `web_search: disabled`, `apps: false`로 설정한다. |
| 이미지 생성, 데스크톱 알림 hook, Codex 자체 멀티 에이전트 | 미연결 | `OFF_FEATURES`, `notify: []` 및 세션 지침. AgentCraft 팀 도구와 Codex 자체 멀티 에이전트는 다르다. |
| `mcp_servers.node_repl` | 미연결 + 호스트 의존 / 실행 확인 불가 | TOML 설정과 실행 파일 존재 확인. 현재 세션에 도구 없음. MCP 전체 비활성화가 직접 원인. 네이티브 파이프 관련 환경 키가 있어 실행 파일 존재만으로 독립 동작을 보장하지 못한다. |

### 설정에서 enabled=true인 플러그인 전체 목록

아래 11개가 전부다. marketplace의 설치 캐시에 있는 다른 플러그인은 이 목록에 포함하지 않았다.

| 플러그인 식별자 | 현재 상태 | 로컬 manifest로 확인한 구성 |
| --- | --- | --- |
| `codex-app-tools@openai-bundled` | 미연결 + 호스트 전용 경로 | `.mcp.json`의 `codex_app` 서버. 도구별 승인 설정·환경 전달 설정이 있다. AgentCraft가 이 데스크톱 서비스 역할을 제공한다는 근거 없음. |
| `browser@openai-bundled` | 미연결 / 호스트 실행 확인 불가 | `skills/`, hooks. browser 기능도 엔진에서 꺼진다. |
| `unified-computer-use@openai-bundled` | 미연결 + 호스트 의존 | `.mcp.json`의 `cua_repl` 서버, 데스크톱 Node 실행 파일과 네이티브 파이프 관련 환경 키. |
| `visualize@openai-bundled` | 미연결 / 호스트 표시 확인 불가 | `skills/`, interface, bundledContentVariant. 현재 세션에 전용 시각화 도구 없음. |
| `documents@openai-primary-runtime` | 미연결 / 런타임 확인 불가 | `skills/`, interface. 스킬은 workspace dependency loader/runtime을 요구하며 시스템 Node/Python으로 대체하지 말라고 명시한다. |
| `pdf@openai-primary-runtime` | 미연결 / 런타임 확인 불가 | `skills/`, interface. 캐시 존재만 확인했으며 생성·렌더링 실행은 검증하지 않았다. |
| `spreadsheets@openai-primary-runtime` | 미연결 / 앱·런타임 확인 불가 | `skills/`, `.app.json`, interface. 앱 연동도 별도 확인 필요. |
| `presentations@openai-primary-runtime` | 미연결 / 런타임 확인 불가 | `skills/`, interface. |
| `template-creator@openai-primary-runtime` | 미연결 / 런타임 확인 불가 | `skills/`, interface. |
| `chrome@openai-bundled` | 미연결 / 호스트 실행 확인 불가 | `skills/`, hooks. 브라우저·Chrome 세션 연결은 확인하지 않았다. |
| `computer-use@openai-bundled` | 미연결 + 호스트 의존 | `skills/`, hooks. 스킬이 새 `node_repl` 세션 초기화를 요구한다. computer_use 기능도 엔진에서 꺼진다. |

`node_repl` 설정의 구조는 `command`, `args`, `env_vars`, `startup_timeout_sec`, 하위 `env`다. `env`에는 `NODE_REPL_*`, `CODEX_HOME`, `CODEX_CLI_PATH`, `BROWSER_USE_*`, `SKY_CUA_NATIVE_PIPE`, `SKY_CUA_NATIVE_PIPE_DIRECTORY` 등의 키가 있다. 해당 값과 파이프 주소는 기록하지 않는다. 실행 파일은 Codex 앱의 버전별 `runtimes/cua_node/.../bin/node_repl.exe` 아래에 있으며 조사 시 존재했다. 이 경로를 새 설정에 고정하면 앱 업데이트 뒤 깨질 수 있다.

## 설정 로더와 차단 지점

기준 소스 루트: `C:/Users/wlqor/Desktop/agentcraft/`.

| 파일·위치 | 확인 사실 | 의미 |
| --- | --- | --- |
| `foreman/src/config.ts:245` | 기본값 → `<AGENTCRAFT_HOME>/config.json` → 환경 → CLI. 기본 home은 `~/.agentcraft`. Codex 옵션은 path/model/effort 계열만 로드한다(:314). | 기존 설정 키만 추가해 MCP/plugin을 허용할 수 없다. 알 수 없는 키가 실제 적용된다고 가정하면 안 된다. |
| `foreman/src/agents/codex/find.ts` | 명시 경로 → PATH → Codex 데스크톱 번들 CLI 순서로 발견한다. | 데스크톱과 Foreman이 서로 다른 CLI 빌드를 사용할 수 있다. 실제 선택된 경로·버전을 진단해야 한다. |
| `foreman/src/agents/codex/engine.ts:101` | Windows app-server의 `LOCALAPPDATA`를 AgentCraft dataDir 아래로 분리한다. `CODEX_HOME`은 여기서 재지정하지 않는다. | 사용자 설정을 못 찾는 것이 직접 원인은 아니다. 기존 런타임 ACL 충돌 회피를 없애는 수정은 피해야 한다. |
| 같은 파일 `:194`, `:206` | 턴별 app-server에서 `config/read {cwd}` 후 thread/start 또는 thread/resume에 config를 전달한다. | 작업 경로의 유효 설정을 읽지만 아래 override가 우선한다. |
| 같은 파일 `:29`, `:148` | 모든 유효 `mcp_servers`와 `plugins`에 `enabled:false`; apps/browser/computer/hooks/image 등의 feature도 false. | 플러그인·MCP 미노출의 직접 원인. |
| 같은 파일 `:208` | team tools만 `dynamicTools`로 등록한다. | 사용자 플러그인 목록과 AgentCraft 팀 도구 목록은 별도다. |
| 같은 파일 `:249` | command/file 승인 요청은 `spec.permission`으로 전달. `item/tool/call`은 등록된 dynamic tool만 실행. | 동적 도구 경로를 곧바로 외부 MCP 승인 경로로 간주하면 안 된다. |
| 같은 파일 `:286` | `mcpServer/elicitation/request`는 무조건 decline. 미처리 RPC 요청은 오류. | 사용자 입력·인증을 요구하는 MCP는 별도 호스트 대응이 필요하다. |
| `foreman/src/policy.ts:2063`, `:2139` | 팀 MCP는 신뢰, 외부 `mcp__*`는 승인 대상으로 분류한다. | 이 분류기가 존재해도 Codex MCP 호출이 실제 이 경로로 들어온다는 보장은 없다. 현재 Codex 엔진에서는 그 연결을 확인하지 못했다. |
| `foreman/src/agents/codex/stream.ts:121` | MCP 호출 로그 이벤트를 표시한다. | 로그 표시 기능은 승인·권한 통제 구현의 증거가 아니다. |
| `foreman/test/codex-backend.test.ts:149` | MCP/plugin/computer_use/web_search 비활성화를 기대하는 회귀 테스트. | 미연결이 우발적 오류가 아닌 기존 설계임을 뒷받침한다. 테스트 코드를 읽었으며 이 조사에서 실행하지 않았다. |
| `README.md:225`, `foreman/src/config.ts:397` | Codex 사용자 MCP·플러그인·앱·웹 검색을 제공하지 않는다고 설명한다. | 활성화 구현 시 설명도 함께 바꿔야 한다. |
| `start-agentcraft-codex.bat`, `tools/launch.ps1:166` | Codex backend로 launcher 실행. 이미 실행 중인 같은 프로필 Foreman은 재사용한다. | launcher 재실행만으로 새 코드·설정이 반영된다고 볼 수 없다. |

추가로 `checkAuth()`는 override 없는 app-server에서 `config/read`와 `account/read`를 사용한다. 따라서 threadConfig에서 끈다는 사실만으로 프로세스 전체 수명 동안 외부 런타임이 전혀 초기화되지 않는다고 단정할 수 없다. 이번 조사에서는 이 메서드나 app-server를 실행하지 않았다.

## 구현 가능한 최소 연결안

수정 대상은 플레이리스트 앱이 아니라 **AgentCraft 소유 저장소**다. 이 worktree에서 외부 저장소를 수정하지 않는다. 아래는 구현 제안이며 현재 제공되는 설정 API가 아니다.

1. 먼저 재현 가능한 상태 점검을 만든다. 실제 선택 CLI 경로·버전, 유효 설정의 MCP/plugin 식별자와 enabled, AgentCraft override 결과, 노출 도구 이름을 값 없는 보고서로 비교한다. 설정 내용을 통째로 저장하지 않으며 환경 값·인증·세션 DB를 검사하지 않는다. 디스크 설정, 정책상 허용, 도구 노출, 실제 호출 성공을 각각 표시한다.
2. 독립 실행 가능한 로컬 MCP부터 서버별 opt-in 연결을 설계한다. 사용자 설정에서 이미 활성화된 항목만 후보로 삼고, 비활성 항목과 새 플러그인을 자동 활성화하지 않는다. 기존 차단 기본값은 유지한다. wildcard 전체 허용이나 모든 `OFF_FEATURES` 제거는 하지 않는다.
3. 연결 전 Codex app-server가 해당 CLI 버전에서 제공하는 MCP 호출 승인·입력·로그 API를 확인한다. MCP 서버는 셸 sandbox 밖에서 파일·네트워크에 접근할 수 있으므로, 기존 command 승인만으로 통제된다고 가정하지 않는다. 도구별 승인·취소를 AgentCraft 정책/게임 UI와 연동할 수 없는 서버는 연결하지 않고 사유를 표시한다. `mcpServer/elicitation/request`는 인증 정보를 메모리에 기록하는 우회 수단으로 쓰지 않는다.
4. 플러그인은 스킬·hooks·MCP·apps 구성을 나눠 확인한다. 문서용 스킬만 필요한 경우 t2의 경로 공유 방안이 더 작은 수정일 수 있지만, 전용 런타임이 필요한 기능까지 지원한다고 표시해서는 안 된다. hooks/apps를 일괄 켜지 않는다.
5. `node_repl`과 `cua_repl`은 별도 호스트 검증 대상으로 둔다. 실행 파일·허용된 런타임 검색, 살아 있는 호스트 서비스와 공식 연결 규약을 검증한 뒤 노출 및 최소 무해 호출을 확인한다. 파이프 주소·trusted service·브라우저 프로필·인증을 복사하거나 ACL/네트워크 권한을 넓히지 않는다. 지원 호스트 규약을 확인할 수 없으면 이 기능은 Codex 데스크톱에서 계속 사용하도록 안내한다.

최소 변경 파일 묶음(AgentCraft 저장소 기준):

| 목적 | 파일 |
| --- | --- |
| opt-in 식별자 검증·설정 로드 | `foreman/src/config.ts`, `foreman/test/config.test.ts` |
| 사용자 활성 상태와 opt-in 교집합 적용, 기능별 override, RPC 승인·입력 처리 | `foreman/src/agents/codex/engine.ts` |
| 실제 API 확인 후 필요한 경우에만 외부 MCP 승인 분류 연결 | `foreman/src/policy.ts`, 관련 policy 테스트 |
| 기본 차단 유지·미등록 항목 거부·스레드 격리·승인 거부 회귀 | `foreman/test/codex-backend.test.ts`, `foreman/test/fixtures/fake-codex.mjs` |
| 지원 경계·점검 명령·재시작 안내 | `README.md`, 필요시 `tools/README.md` |

진단만 우선 구현한다면 상태 점검 스크립트와 해당 검증·문서만으로 시작할 수 있다. 위 연결 구현 전체를 완료했다는 의미는 아니다. 테스트는 fake app-server로 기본 차단, 비활성 항목 보존, 승인·취소·타 스레드 거부를 검증한 뒤 허용된 실제 서버에서만 수행한다.

## 재시작·인증·유지 관리

| 변경/기능 | 재시작 | 인증 |
| --- | --- | --- |
| 이번 진단 문서 | 불필요 | 불필요 |
| 향후 Foreman 소스·설정 로더 변경 | 작업을 정리한 뒤 해당 Foreman을 정상 종료·재시작. 기존 launcher는 실행 중인 프로필을 재사용하므로 이것만 재실행하면 부족할 수 있음 | 보통 별도 로그인 변경 없음. 기존 Codex 로그인 오류가 있을 때만 사용자 직접 `codex login` |
| 향후 MCP 설정 연결 | 새 app-server/턴에서 적용하고 실제 도구 목록 재확인. 프로세스 시작 시 설정도 검증해야 함 | 서버별로 다름. 현재 인증 상태 확인 안 함. OAuth가 필요하면 사용자 본인이 공식 흐름에서 로그인 |
| 데스크톱 browser/computer/node_repl 경로 | 앱 호스트 실행과 지원 연결 확인 필요. Foreman 재시작만으로 충분한지 확인 불가 | 브라우저/앱의 인증과 별개일 수 있음. 세션·쿠키·토큰 복사 금지 |
| Codex 앱 업데이트 | CLI 선택·런타임 존재·도구 노출을 다시 점검 | 업데이트만으로 재인증이 필요한지는 확인 불가 |

동등 상태를 유지하려면 앱 업데이트와 Foreman 변경 후 같은 점검을 반복하고, 새로 enabled가 된 플러그인은 자동 실행하지 않고 차이를 보고해야 한다. 목표는 가능한 기능의 검증된 연결이며, 호스트 UI·인증·승인 경로까지 검증하기 전에는 완전한 동일 환경이라고 표시하지 않는다.

## 이번 검증 결과

문서의 각 판정을 로컬 설정의 선별 정보, 11개 활성 플러그인 manifest, 엔진·정책·로더·기존 테스트, 세션 도구 명세와 대조했다. `node_repl.exe` 파일 존재도 확인했다. 변경은 Markdown 문서 추가뿐이며 앱 테스트·외부 저장소 테스트·실제 MCP 호출은 실행하지 않았다. 공식 문서 도구 부재로 호스트 연결 API의 공식 지원 여부는 확인하지 못했다.
