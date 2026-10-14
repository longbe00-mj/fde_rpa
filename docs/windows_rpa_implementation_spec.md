# Windows 로컬 RPA 실행기 — Codex 구현 명세

작성일: 2026-10-13  
상태: 구현 제안 / 실제 대상 앱 검증 전  
대상: Windows PC 한 대, 웹 + Windows 앱, 사용자 보조 실행 후 무인화

> 이 문서는 개발할 프로그램의 명세이다. 파일명, 클래스, 명령, API는 제안 인터페이스이며 이미 설치되어 있는 제품이나 완성된 실행 코드가 아니다. 대상 앱의 selector, 화면 좌표, 경로, 완료 판정은 현장 조사로 확정한다.

## 1. 확정 조건과 범위

| 항목 | 조건 |
|---|---|
| 대상 | 웹사이트와 Windows 데스크톱 앱, 한 Workflow 안에서 혼합 가능 |
| 실행 장치 | Windows PC 한 대 |
| 초기 운영 | 사용자가 업무를 선택하고 입력을 제공하여 실행. 로그인과 일부 예외는 직접 처리 |
| 후속 운영 | 검증된 업무부터 무인 실행. 로그인·활성 데스크톱 등의 실행 요건은 별도 충족 |
| 완료 판정 | 업무마다 다름. 공통 반환 형식과 업무별 verifier로 대응 |
| 실행 수 | 전체 앱에서 활성 Runner 최대 한 개. 분산 실행과 처리량 최적화 제외 |
| 업무 권한 | 역할 기반 접근 제어, 다중 사용자 승인 시스템, 멀티테넌트 제외 |
| 개발 | Codex를 코드 작성·리팩터링·테스트 도구로 사용 |

역할별 업무 권한 관리와 별개로, 프로세스 무결성 수준·사용자 세션·로그인 상태는 화면 입력의 동작 조건이므로 점검한다. Windows SendInput은 UIPI 제약을 받는다.[R09]

## 2. 설계 결정

1. Windows 네이티브 Python 프로세스가 실제 데스크톱 입력을 소유한다.
2. 초기에는 명시적인 Python 업무 플러그인을 실행한다. 모든 클릭을 LLM이 결정하게 하지 않는다.
3. 웹은 Playwright, Windows 컨트롤은 pywinauto/UI Automation을 우선 검토하고, 구조로 접근할 수 없는 영역은 PyAutoGUI로 실행한다.
4. PyAutoGUI는 주요 실행 어댑터 중 하나이지 업무 엔진 전체가 아니다.
5. 업무 코드, 대상 selector, 실행 어댑터, 검증 코드를 분리한다.
6. 로컬 UI/Controller와 Runner를 별도 프로세스로 둔다. 처음 CLI로 Runner를 검증한 다음 UI를 붙인다.
7. 영구 상태는 SQLite, 화면·파일 증거는 실행별 디렉터리에 저장한다. 외부 DB·메시지 브로커는 도입하지 않는다.
8. 수동 확인으로 끝난 업무와 기계 검증으로 끝난 업무를 구별한다.
9. 취소는 다음 동작을 중단하는 것이며 이미 전달된 입력이나 저장의 취소를 뜻하지 않는다.
10. Agent 확장은 고정된 업무 경계 안에서만 추가한다. LLM 없는 MVP는 Browser Agent/시각적 Computer Use의 전체 완성판이 아니다.

### 이번 단계에서 제외

클라우드 RPA 관리 서버, Redis/RabbitMQ, Kubernetes, 다중 워커, 역할별 업무 권한, BPMN/시각적 Workflow 편집기, 범용 녹화 제품, 런타임 Python 코드 생성, 실행 중 자기 코드 수정, LangGraph/Temporal 필수 도입, MCP/A2A 필수 연동.

## 3. 권장 구성

```text
Windows의 로그인된 사용자 세션

[로컬 실행 화면]
   업무 선택 / 입력 / 시작 / 일시정지 / 재개 / 중단 / 결과
             |
[Controller: FastAPI + 최소 HTML/JS]
   - localhost 전용
   - 활성 실행 한 개
   - Python Runner 프로세스 시작/상태 감시
   - 실행 상태 조회와 UI 이벤트 제공
             |
   stdin 명령 / stdout JSON 이벤트 / stderr 로그
             |
[Runner: Windows Python 프로세스]
   - 메인 실행 스레드: 엔진 + 모든 GUI/브라우저 조작
   - 제어 수신 스레드: 명령을 상태 플래그에 반영, GUI 호출 금지
   - 업무 플러그인 / RunContext
   - Playwright / UIA / PyAutoGUI / File 어댑터
   - 단계별 verifier와 재시도/대조 정책
             |
[SQLite 실행 상태] + [실행별 증거 파일]
```

모든 프로세스는 같은 PC에 있다. 위 분리는 분산 아키텍처가 아니라 UI 응답성, 실행 소유권, 중단과 장애 격리를 위한 제안이다.

### 프로세스·스레드 규칙

- Playwright 인스턴스와 UIA 객체는 사용하는 Runner 실행 스레드에서 생성·사용·정리한다. HTTP 요청 스레드나 임의 thread pool 사이에 넘기지 않는다.
- 초기 Runner는 Playwright 동기 API를 사용한다. Controller의 async 이벤트 루프와 분리한다.
- Playwright API는 thread-safe하지 않으며 Windows async 모드에는 이벤트 루프 제약이 있다. 공식 문서는 실행 중인 Playwright asyncio 호출의 취소도 지원하지 않는다고 설명한다.[R02]
- pywinauto는 COM 모델의 영향을 받는다. GUI 코드와 충돌하는 암묵적 COM 초기화를 피하고 선택한 모델과 초기화 위치를 문서화한다. 무조건 STA를 강제하지 않는다.[R07]
- stdout은 IPC 메시지 전용으로 사용하고 일반 로그는 stderr로 보낸다.
- 브라우저 창·프로필과 앱의 소유 여부를 기록한다. 사용자가 원래 열어 둔 앱을 실행 종료 과정에서 임의로 닫지 않는다.
- Controller 제어 채널이 끊어지면 다음 동작 경계에서 더 이상 입력하지 않도록 한다. 진행 중 호출의 결과가 불명확하면 대조 대상으로 남긴다.
- UI 브라우저 탭 닫기와 Runner 취소는 구분한다. 로컬 실행기는 명시적인 중단 기능과 사용자에게 보이는 실행 상태를 제공한다.

## 4. 프로젝트 구조

```text
windows-rpa/
  AGENTS.md
  pyproject.toml
  requirements.lock                 # 검증된 방식으로 생성할 의존성 잠금 파일
  README.md
  docs/
    windows_rpa_implementation_spec.md
    acceptance.md
  src/rpa/
    cli.py
    worker.py
    controller/
      app.py
      process_manager.py
      ipc.py
      static/
    core/
      engine.py
      contracts.py
      control.py
      run_context.py
      run_store.py
      errors.py
      registry.py
    platform/windows/
      preflight.py
      foreground.py
      display.py
      hotkeys.py
      instance_lock.py
    adapters/
      browser.py
      desktop_uia.py
      desktop_visual.py
      files.py
    workflows/
      demo_web_to_desktop/
        workflow.py
        inputs.schema.json
        selectors.yaml
        settings.yaml
        verifiers.py
        assets/
    vision/
      contracts.py                  # 확장 인터페이스; 초기 모델 구현은 선택
  fixtures/
    web/
    windows/
    cases/
  tests/
    unit/
    integration/
    live_windows/
  scripts/
    start.ps1
    test-unit.ps1
    test-live.ps1
```

실행 데이터는 소스와 분리된 사용자 데이터 디렉터리를 사용한다. 예: `%LOCALAPPDATA%\LocalRPA\`. 이 경로는 제품 구현에서 확정할 제안이다. 테스트 자료를 제외한 실사용 입력, 인증 프로필, 실행 DB, 화면 증거, 비밀값을 Git에 넣지 않는다.

## 5. 공통 계약

구체 타입은 Python dataclass 또는 Pydantic 등 하나의 검증 방식으로 일관되게 정의한다. UIA 핸들, Playwright Page/Locator, 열린 파일 객체는 직렬화 대상이 아니다.

### 5.1 WorkflowDefinition

- id, version, 설명
- 입력 스키마
- 환경 요구 사항
- 단계 목록/순서와 코드로 정의한 분기
- 최종 verifier
- 사용자 개입 정책
- 지원하는 실행 모드

초기에는 Python 코드에 Step을 등록한다. YAML은 입력 기본값·selector·환경 설정에 사용하고, 범용 스크립트 언어로 확장하지 않는다. 설정에서 임의 import, eval, exec를 허용하지 않는다.

### 5.2 StepDefinition

| 필드 | 목적 |
|---|---|
| id / version | 단계의 안정적 식별 |
| handler | 사전 등록된 업무 함수 |
| input_mapping | RunContext에서 필요한 데이터 참조 |
| precondition | 대상 화면·입력·업무 건 검증 |
| timeout | 단계 전체의 유한 시간 상한 |
| action_timeout | 개별 호출에 전달할 유한 시간 상한 |
| effect_kind | READ_ONLY / LOCAL_CHANGE / EXTERNAL_CHANGE |
| operation_key | 동일 논리 변경의 재시도에서도 유지되는 식별자 |
| verifier | 결과 검증 함수 |
| retry_policy | 오류·효과 상태에 따른 명시적 재시도 |
| reconcile | 외부 처리 여부를 재조회하는 함수 |
| manual_resolution | 사용자에게 필요한 작업과 재개 조건 |

자동 저장 폼은 fill 단계도 EXTERNAL_CHANGE가 될 수 있다. 파일 저장은 저장 위치·덮어쓰기 정책에 따라 LOCAL_CHANGE로 분류한다. 키 입력과 클릭이라는 도구 이름만 보고 부작용 여부를 판단하지 않는다.

### 5.3 RunContext

- run_id, workflow_id, workflow_version
- workflow_code_hash, config_hash, input_hash
- business_key
- validated_inputs
- 각 단계의 직렬화 가능한 출력
- 현재 단계와 마지막 검증된 checkpoint
- execution_status와 business_outcome
- logical_operation_id와 attempt_id
- 사용자 개입 기록
- 증거 참조, 생성한 파일 목록
- 실행 환경 요약과 소유한 프로세스/브라우저 정보

프로세스가 재시작되면 화면과 로그인도 복구됐다고 가정하지 않는다. 대상 앱 재연결, locator 재해석, 업무 건 확인을 거쳐 재개한다.

### 5.4 ActionResult와 CheckResult

ActionResult는 실행 요청을 했다는 관측이고 업무 성공 자체가 아니다.

```json
{
  "action_id": "enter_customer",
  "dispatch_state": "DISPATCHED",
  "outputs": {"customer_id": "DEMO-001"},
  "evidence_refs": ["artifacts/run-id/step-id/after.png"]
}
```

CheckResult의 제안 형식:

```json
{
  "status": "PASS",
  "verification_mode": "RULE",
  "business_key": "DEMO-001",
  "expected": {"customer_id": "DEMO-001"},
  "observed": {"customer_id": "DEMO-001", "record_id": "EXAMPLE-42"},
  "checked_at": "2026-10-13T10:00:00+09:00",
  "evidence": [
    {"type": "readback", "source": "desktop_result_query", "ref": "artifacts/run-id/check.json"}
  ],
  "reason": "조회된 업무 식별자와 저장값이 일치함"
}
```

값은 설명용 예시이다. `status`는 PASS / FAIL / UNKNOWN, `verification_mode`는 RULE / HUMAN으로 구분한다.

- PASS: 정의된 확인 조건을 만족.
- FAIL: 충분한 근거로 기대 결과와 불일치를 확인. 미처리나 안전한 재시도를 뜻하지 않음.
- UNKNOWN: 근거 부족, 조회 불가, 시간 초과 등으로 판정할 수 없음.
- HUMAN: 사용자가 확인한 사실·근거·시각을 기록. RULE 성공으로 바꾸지 않음.

## 6. 실행 상태와 업무 결과

실행 상태와 업무 결과를 별도 저장한다.

실행 상태 예시: CREATED, RUNNING, PAUSE_REQUESTED, PAUSED, WAITING_USER, CANCEL_REQUESTED, CANCELLED, FAILED, FINISHED.

업무 결과 예시: UNVERIFIED, AUTO_VERIFIED, HUMAN_CONFIRMED, MISMATCH, UNKNOWN.

예를 들어 취소 직전에 저장이 전달됐다면 `execution_status=CANCELLED`, `business_outcome=UNKNOWN`이 가능하다. 이 상태를 “아무 작업도 하지 않음”으로 표시하면 안 된다.

### 제어 동작

| 제어 | 명세 |
|---|---|
| 시작 | 입력 검증, 기존 활성 Runner 부재 확인, 환경 점검, 버전 고정 |
| 일시정지 | PAUSE_REQUESTED → 입력이 진행 중이지 않은 경계에서 PAUSED |
| 재개 | 대상 앱·화면·업무 건·입력 상태를 다시 확인하고 진행 |
| 사용자 요청 | WAITING_USER에 이유와 해결 조건 기록 |
| 취소 | 새 동작 전송을 중단. 이미 전송한 작업은 자동 취소되지 않음 |
| 강제 종료 | 최후 수단. 마지막 부작용 결과를 UNKNOWN으로 취급하고 재실행 전 대조 |

긴 작업 중단을 `time.sleep(30)` 뒤에만 검사하지 않는다. 취소 플래그와 단조 시계 기반 deadline을 사용하는 짧은 대기 또는 도구의 조건 대기·timeout으로 구성한다. 단, 실행 중인 외부 호출을 임의로 끊었을 때 결과가 확정되는 것은 아니다.

입력 중 내려간 키는 정상 중단 경로의 finally에서 해제한다. 프로세스 강제 종료 시에는 cleanup을 보장할 수 없으므로 UI에 이를 알리고 다음 실행 전 상태를 확인한다.

PyAutoGUI의 FAILSAFE는 기본적으로 유지한다. FailSafeException은 일반 실패 재시도의 대상으로 잡지 않는다. 전역 중단 키는 명령 신호만 만들고 업무 키 입력을 수집·저장하지 않는다.[R05]

## 7. 단계 실행 알고리즘

1. 명령·취소 상태를 확인한다.
2. 현재 단계의 입력과 사전조건을 검사한다.
3. 대상 앱·창·업무 식별자를 새로 확인한다.
4. 조작 대상을 현재 DOM/UIA/화면 관측에서 다시 찾는다.
5. 변경 단계라면 logical_operation_id, 입력 해시, 대상, 실행 의도를 영구 저장하고 저장 완료를 확인한다.
6. 취소 상태를 다시 확인하고 실제 동작을 전송한다.
7. 정해진 deadline까지 완료 조건을 기다린다.
8. 업무별 verifier로 결과를 검사한다.
9. 결과·증거·checkpoint를 영구 저장한다.
10. PASS이면 다음 단계, FAIL/UNKNOWN이면 정책에 따라 대조·사용자 확인·실패 처리한다.

5와 6 사이, 6과 9 사이의 프로세스 종료를 시험한다. 의도만 저장된 경우에도 외부 작업이 전송되지 않았다고 단정하지 않고 복구 때 보수적으로 대조한다.

### 재시도 원칙

조회나 요소 탐색은 정의된 일시 오류에 대해 제한된 재시도를 허용한다. 저장·제출·업로드 등은 timeout만으로 재시도하지 않는다. 재조회로 이미 완료됐으면 결과를 복원하고, 미반영을 충분히 확인했을 때만 다시 수행한다. 판정 불가능하면 사용자 확인 대상으로 남긴다.

## 8. 웹 어댑터

### 초기 기능

- 전용 프로필을 사용하는 headful 브라우저 실행
- 로그인 상태 검사와 WAITING_USER 전환
- URL 이동, 탭/팝업, iframe
- 요소 식별, 클릭, 입력, 값 추출
- 파일 업로드와 다운로드
- 표/필드 추출 결과의 구조화
- 업무 결과 readback
- 실패 trace 및 필요한 화면 증거

Playwright의 role/label/test ID locator와 actionability 검사, 조건 대기를 활용한다.[R01][R03] 기본 개인 Chrome 프로필 자동화는 지원되지 않을 수 있으므로 문서가 안내하는 별도 user_data_dir를 사용한다. 동일 프로필의 동시 사용도 피한다.[R04]

MVP에서 이미 열려 있는 사용자의 모든 탭을 무조건 제어하는 기능은 제외한다. RPA 전용 브라우저에서 사용자가 로그인한 뒤 Runner가 이어서 처리한다. 로그인 상태가 만료되면 재인증 필요 상태로 전환한다.

다운로드는 저장 파일명만 추측하지 않고 다운로드 이벤트를 받아 실행별 경로에 보존한다. 브라우저 context 종료에 따른 임시 다운로드 제거와 구분해야 한다.[R12]

DOM locator를 찾지 못하면 로그인 만료, 로딩, 권한 화면, 서버 오류, UI 변경을 분류한다. 즉시 좌표 클릭으로 전환하지 않는다.

## 9. Windows·PyAutoGUI 어댑터

### 9.1 대상 조사

실제 앱에서 process ID, 창 제목/클래스, control_type, AutomationId, 값 읽기/쓰기 지원을 확인한다. pywinauto는 win32/uia 백엔드를 제공하며 대상 기술에 맞는 조사와 선택이 필요하다.[R06] Microsoft Inspect 등으로 UIA 속성을 조사할 수 있다.[R10]

Codex는 이 정보를 근거로 selector를 생성하도록 한다. 존재하지 않는 AutomationId나 좌표를 그럴듯하게 만들지 않게 한다.

### 9.2 실행 기능

- 실행/연결: 기존 앱에 attach하거나 업무가 필요한 경우 새로 launch
- 창 식별: 소유 프로세스와 목표 화면으로 한정
- 컨트롤 탐색: UIA/Win32 selector
- 컨트롤 값 읽기/설정 또는 의미 기반 invoke
- 시각적 클릭: 대상 창 영역 안에서 이미지 기준점 탐색
- 포커스 입력: 매 입력 직전 현재 foreground와 기대 필드 확인
- 한글 입력: 구조적 값 입력 우선, 필요한 앱만 Unicode 입력/허용된 붙여넣기
- 입력 후 원본과 readback 비교
- 캡처: 필요한 창/영역만 저장

pywinauto 키보드 문서는 Windows Unicode 입력을 설명하지만 앱별 동작 차이는 실측한다.[R15]

### 9.3 좌표와 화면

초기 PyAutoGUI 지원 범위는 주 모니터, 합의한 DPI 배율과 해상도로 한정한다. PyAutoGUI 문서는 주 모니터 지원과 자체 OCR 미제공을 명시한다.[R05]

좌표는 화면 전체의 상수로 업무 코드에 넣지 않는다. 창 위치, 캡처 origin, 원본/리사이즈 크기, DPI 정보를 기록하고 상대 좌표를 해석한다. 지원하지 않는 모니터·배율이면 시작 전 안내하고 중단한다. 보정 없이 “어디서나 동작”을 주장하지 않는다.

이미지 탐색은 대상 창·폼으로 영역을 제한한다. `confidence` 사용에는 OpenCV가 필요하며 이 수치는 업무 성공 확률이 아니다.[R13]

### 9.4 사용자와의 협업

초기 계약은 “RPA 실행 구간에는 같은 데스크톱의 마우스·키보드를 함께 쓰지 않는다”로 정한다. 사용자가 계속 다른 앱을 자유롭게 쓰는 기능은 초기 지원 범위가 아니다. DOM/UIA 일부 조작이 포커스를 요구하지 않더라도 혼합 Workflow 전체의 무간섭 실행을 보장하지 않는다.

작업 중에는 상태를 표시하고, 사용자가 조작하려면 일시정지 후 PAUSED 확인 → 직접 작업 → 재개 순서로 사용한다. 창 포커스를 무한 반복해서 빼앗는 방식으로 복구하지 않는다. 예기치 않은 focus 변경을 감지하면 후속 입력을 중단하고 확인을 요청한다. 입력 직전 검사를 하더라도 사용자 조작과의 경합을 완전히 제거할 수 없으므로 사용자 협업 규칙이 필요하다.

## 10. 완료 판정 플러그인

| 업무 | 가능한 자동 검증 | 초기 수동 대안 |
|---|---|---|
| 파일 다운로드 | 이번 실행에서 생성된 파일, 읽기 성공, 스키마/기간/건수/내용 확인 | 파일 내용 확인 후 근거 기록 |
| 웹 등록 | 원천 업무 ID로 재조회, 결과 ID·주요 입력값 대조 | 등록 화면/조회 결과를 사용자가 확인 |
| Windows 문서 저장 | 저장 파일을 다시 열거나 파싱해 주요 값 비교 | 실제 문서 확인 |
| 앱 데이터 입력 | 저장 후 동일 업무 건을 재조회해 값 일치 확인 | 해당 건의 결과와 식별자를 확인 |
| 내용 검토/판단 | 정의된 규칙으로 판정 가능한 부분 자동화 | 판단 근거를 남기는 사용자 확인 |

성공 toast, 클릭 반환, 캡처 파일 존재만으로 모든 업무의 성공을 판정하지 않는다. 원천 데이터와 출력의 연결이 가능해야 한다.

무인화 대상은 필요한 검증이 자동으로 가능하고 UNKNOWN의 중단·통지 경로가 정해진 업무부터 선정한다. 모든 수동 확인을 임의로 PASS로 바꾸는 옵션을 두지 않는다.

## 11. 저장 구조

초기 SQLite 모델 제안:

- runs: 실행 ID, 업무/코드/입력/설정 버전, 실행 상태, 업무 결과, 현재 단계
- step_attempts: 단계, 시도, 시간, 오류, 반환값, 검증 상태
- operations: logical_operation_id, 업무 key, dispatch/확인 상태, 결과 식별자
- evidence: 실행/단계 참조, 종류, 상대 경로, 생성 시각, 필요 시 파일 해시
- user_interventions: 이유, 사용자 조치, 확인 근거, 변경된 입력, 시각

작은 트랜잭션으로 저장하고 외부 화면 작업을 수행하는 동안 DB 트랜잭션을 계속 열어 두지 않는다. SQLite 연결을 프로세스·스레드 간 무조건 공유하지 않는다. 선택한 쓰기 소유권과 잠금/timeout 정책을 테스트로 확정한다.

실행 중인 코드와 설정은 고정한다. Codex가 수정한 코드를 실행 중 프로세스가 자동 reload하지 않게 한다. 재개 시 version/hash가 달라졌다면 기존 코드로 재개하거나 새 실행으로 명시 전환한다. 이미 반영됐을 수 있는 업무 단계는 먼저 대조한다.

## 12. 로컬 UI·API 제안

API는 구현할 계약이며 기존 제품 API가 아니다.

| API | 목적 |
|---|---|
| GET /workflows | 등록된 업무와 입력 스키마 |
| POST /runs | 검증 후 단일 실행 시작 |
| GET /runs/{run_id} | 상태, 단계, 결과, 사용자 조치 필요 사항 |
| POST /runs/{run_id}/pause | 일시정지 요청 |
| POST /runs/{run_id}/resume | 사전조건 재검증을 포함한 재개 |
| POST /runs/{run_id}/cancel | 중단 요청 |
| POST /runs/{run_id}/resolve | 사용자 확인·수정 사항 제출 |
| GET /runs/{run_id}/events | 상태 이벤트 또는 polling용 변경 이력 |

초기 화면은 업무 목록, 입력 폼, 상태/단계, 제어 버튼, 사용자 조치, 결과/증거 정도로 구성한다. React/Vue와 시각적 편집기를 필수로 두지 않는다.

네트워크 역할 권한 제품은 제외하더라도 local endpoint는 localhost에만 bind한다. 실행별/앱 세션별 토큰과 Origin 검사 등으로 브라우저의 임의 외부 페이지가 명령을 보내는 것을 막는다. 토큰·인증 정보는 URL query/log에 남기지 않는다. 임의 shell 명령이나 Python 코드를 HTTP로 받지 않는다.

## 13. Agent 확장

### 단계 A: 실행 기반

고정 업무의 등록된 handler 실행, 필요한 데이터를 구조화해 전달, 검증·중단·재개. 이 단계는 전통 RPA에 해당한다.

### 단계 B: 제한된 Browser Agent / Computer Use

업무가 명시적으로 허용한 구간에서만 관찰과 판단을 허용한다.

```text
Observe → ProposeAction → Validate → Execute → Verify → Observe
```

- Browser Agent: 현재 DOM/접근성 정보에서 대상 탐색 또는 제한된 업무 경로 선택.
- Computer Use: 현재 캡처를 VLM이 해석하고 지원된 클릭/입력 명령을 제안.
- 두 기능 모두 기존 Runner와 같은 어댑터·중단·검증 경로로 실행.
- 모델은 실행 상태의 소유자가 아님.
- 모델 실패 시 사용자 확인으로 전환 가능해야 함.

### 모델 액션의 계약

observation_id, target_app/page, allowed_action, target, arguments, expected_postcondition을 포함한다. 시각 좌표는 어느 원본 캡처를 기준으로 하는지 명시한다. 화면/창 변경 시 오래된 좌표를 버린다.

허용 도메인·대상 앱·업무 기능·최대 단계·최대 반복·시간 상한을 적용한다. 화면·문서의 문자열이 업무 정의를 바꾸는 지시로 취급되지 않게 한다. 초기에는 복구 불가 쓰기 동작을 모델이 자율적으로 선택하지 않게 한다.

Agent가 제안한 selector 수정은 후보로 저장하고 테스트를 거쳐 새 버전으로 반영한다. 실행 중 selector를 무제한으로 바꾸며 클릭하지 않는다.

## 14. 사용자 보조에서 무인화로 전환

| 수준 | 동작 | 전환 조건 |
|---|---|---|
| A. 보조 | 사용자 시작, 로그인 지원, 필요한 단계 수동 확인 | 실행기와 업무 검증 확보 |
| B. 준무인 | 시작 후 정형 구간 자동, 예외만 WAITING_USER | 정상 완료·취소·복구가 안정화 |
| C. 무인 | 예약 시작, 자동 완료 판정, 예외 시 중단·기록/통지 | 로그인/세션 요건, 자동 검증, 복구 경로 확보 |

공통 엔진은 유지하고 trigger와 interaction policy를 변경한다. attended=false가 인증이나 잠금 해제를 자동 해결하지는 않는다.

Windows 서비스에서 데스크톱 입력을 직접 실행하는 구조를 기본으로 두지 않는다. Microsoft는 사용자 세션의 별도 애플리케이션과 통신하는 방식을 설명한다.[R08] 작업 스케줄러도 대화형 세션 토큰과 로그인 조건을 구분한다.[R11]

PyAutoGUI 중심 무인 작업은 필요한 활성 사용자 데스크톱을 확보해야 한다. 잠금/RDP 최소화/연결 해제는 실제 PC 정책과 조작 방식별로 검증한다.[R14] 잠금 화면 우회나 UAC 자동 승인은 구현 범위가 아니다. 로그인/사용자 입력을 요구하는 상태는 정상적인 WAITING_USER 또는 실행 불가 사유로 처리한다.

## 15. Codex 개발 운영

### 15.1 개발 도구와 제품 실행기의 분리

Codex는 소스 작성·테스트·리팩터링을 돕고, 사용자는 완성된 LocalRPA를 실행한다. RPA 실행마다 Codex CLI를 필수로 호출하지 않는다. 런타임 모델 API는 실제로 필요한 Agent 구간에서 별도 추가한다.

Codex는 Windows 네이티브 환경을 지원하지만 샌드박스 방식에 따라 UI 격리 환경이 다르다. 공식 Windows 문서는 legacy sandbox가 기본 private desktop을 사용하는 경우를 설명한다.[R16] 따라서 샌드박스 안에서 스크립트가 실행된 것과 사용자의 실제 업무 앱을 조작한 것을 구분해야 한다.

- Codex: unit test와 mock integration test를 우선 수행.
- 실제 화면 시험: 사용자가 허용한 Windows 사용자 세션과 테스트 앱에서 수행.
- 라이브 테스트가 제한되면 실행하지 못한 테스트와 이유를 보고.
- 해결을 위해 샌드박스를 전면 해제하는 것을 기본 지침으로 삼지 않음.
- 실제 대상 화면의 selector 수집 없이 “전체 동작 완료”라고 보고하지 않음.

### 15.2 AGENTS.md

저장소 루트에 이 패키지의 AGENTS.md를 둔다. Codex는 작업 시작 시 AGENTS.md를 읽으므로 프로젝트 규칙을 유지하는 데 사용할 수 있다.[R17] 이 문서는 `docs/windows_rpa_implementation_spec.md`에 두고 AGENTS.md에서 참조한다.

### 15.3 개발 순서와 완료 조건

| 순서 | 구현 범위 | 완료 조건 |
|---|---|---|
| 1 | 계약·상태·RunStore·FakeAdapter·CLI | GUI 없이 정상/실패/취소/결과 미확정 테스트 통과 |
| 2 | DesktopDriver | 테스트 Windows 앱의 대상 탐색·입력·확인·중단 |
| 3 | BrowserDriver | 테스트 웹 폼의 입력·추출·다운로드·검증 |
| 4 | 혼합 업무 | 웹 데이터 → RunContext → Windows 입력 → 결과 대조 |
| 5 | 사용자 보조 UI | 시작/일시정지/재개/중단/WAITING_USER를 끝까지 시험 |
| 6 | 장애 복구 | 제출 직후 장애, restart, 입력/설정 변경을 정확히 처리 |
| 7 | Agent 선택 구간 | 기존 정상 경로를 깨지 않고 탐색/예외 해석 확장 |
| 8 | 무인화 | 해당 업무의 자동 판정·세션 요건·예외 중단 충족 |

단계별로 테스트와 변경 파일을 검토한 뒤 다음 단계를 요청한다. 대형 자연어 요구 한 번으로 모든 플랫폼 기능을 동시에 만들지 않는다.

### 15.4 첫 번째 Codex 작업 지시문

```text
AGENTS.md와 docs/windows_rpa_implementation_spec.md를 읽고
Windows PC 한 대에서 실행할 LocalRPA의 1단계를 구현해라.

이번 범위는 계약, 실행 상태, RunStore, 제어 플래그, FakeAdapter,
완료 검증 계약, CLI, unit test다. 실제 GUI 입력과 브라우저 실행은 하지 마라.

요구사항:
- Runner 한 개만 활성화할 수 있는 실행 소유권 인터페이스.
- Workflow/Step/ActionResult/CheckResult/RunContext 타입.
- 실행 상태와 업무 결과 분리.
- 단계/동작 경계의 pause, resume, cancel.
- 변경 동작 전 실행 의도의 영구 저장.
- 변경 결과 미확정 시 자동 재실행 금지.
- PASS/FAIL/UNKNOWN과 RULE/HUMAN 구분.
- mock action으로 정상/실패/취소/재개/저장 직후 장애 시험.
- GUI 모듈을 import하지 않고 unit test 실행 가능.
- 미래 API를 만들어 놓기만 하지 말고 CLI demo가 끝까지 실행되게 구현.

먼저 간단한 파일 계획을 제시한 다음 구현하고 테스트해라.
마지막에 변경 파일, 실행한 테스트, 실패/미실행 테스트,
실제 Windows에서 나중에 검증해야 하는 항목을 보고해라.
비밀값과 실사용 업무 데이터는 사용하지 마라.
```

### 15.5 두 번째 Codex 작업 지시문

```text
1단계 엔진의 테스트를 유지하면서 DesktopDriver를 구현해라.

- pywinauto UIA와 PyAutoGUI의 책임을 분리한다.
- 대상 창/process 식별, foreground 확인, 값 입력, 캡처를 지원한다.
- PyAutoGUI FAILSAFE를 유지하고 재시도 예외에서 제외한다.
- GUI 호출은 Runner의 실행 스레드에서만 한다.
- 단일 모니터/배율 지원 범위를 명시한다.
- 한글·특수문자·선행 0 입력 후 readback을 확인한다.
- 실제 앱 selector를 추측하지 않는다.
- 라이브 테스트는 opt-in이며 사용자 허용 테스트 앱에서만 실행한다.
- 허용된 사용자 세션에 접근하지 못하면 테스트를 건너뛰고 사유를 보고한다.

UI나 Agent 모델 호출은 아직 추가하지 마라.
```

## 16. 테스트 전략

### 자동 테스트의 기본 범위

GUI가 없는 환경에서도 계약·상태 전이·검증·재시도·재개 정책은 FakeAdapter로 시험한다. GUI 관련 라이브러리는 지연 import하여 이 테스트를 방해하지 않는다.

### 실제 Windows 테스트

웹 HTML 폼과 접근성 정보를 조사할 수 있는 테스트 Windows 폼을 준비한다. 실제 앱 버전별 selector를 fixture로 저장하고 적합성을 확인한다. Playwright codegen은 locator/동작 초안을 얻는 용도로 사용할 수 있지만 업무 검증·예외·복구 코드는 별도 작성한다.[R18]

| 사례 | 기대 결과 |
|---|---|
| 정상 웹 → Windows 처리 | 동일 business_key와 주요 값 readback 일치 |
| 한글·특수문자·선행 0 | 입력 원본 보존 |
| 중복된 버튼/필드 | 대상 업무 영역 안의 한 요소만 선택 |
| 대상 창 사라짐/포커스 변경 | 후속 입력하지 않음 |
| 로그인 만료 | WAITING_USER, 쓰기 동작 미수행 |
| pause 요청 | PAUSE_REQUESTED 뒤 안전 경계에서 PAUSED |
| 수동 조작 후 resume | 화면·업무 건 재검증 |
| cancel/FAILSAFE | 이후 입력 미수행, 외부 결과 불명확 시 UNKNOWN |
| 변경 호출 직후 예외 | blind retry 없음 |
| 이미 저장됐지만 응답 누락 | 재조회로 결과 복원, 중복 없음 |
| verifier FAIL | 외부 미반영으로 단정하지 않음 |
| Controller/Runner 중단 | 새 실행 전 이전 결과 대조 |
| 실행 중 코드 변경 | 기존 실행 버전 고정, 재개 불일치 감지 |
| 파일명만 맞고 내용 틀림 | 검증 FAIL |
| 사용자 확인 완료 | HUMAN_CONFIRMED, 자동 성공과 구분 |
| RDP/화면 잠금 | 지원 여부 실측, 부적합 시 입력 차단 |

테스트 결과는 “실행한 자동 테스트”, “실행한 Windows 라이브 테스트”, “아직 실행하지 못한 테스트”를 구분해서 보고한다. 표본 몇 건의 성공을 모든 앱 호환성이나 무인 실행 보장으로 확대하지 않는다.

## 17. MVP 인수 기준

- 등록한 하나의 업무를 사용자가 시작해 웹과 Windows 앱을 순서대로 처리한다.
- 입력부터 결과까지 같은 업무 식별자를 추적한다.
- 주요 입력의 readback/결과 대조가 가능하다.
- 일시정지·사용자 처리·재개가 가능하다.
- 중단하면 후속 입력이 나가지 않는다. 이미 진행 중인 변경 결과는 정확히 표시한다.
- 실패한 단계·원인·필요 조치·관련 증거가 남는다.
- 변경 결과 미확정 상태가 단순 실패나 성공으로 숨겨지지 않는다.
- 수동 확인과 자동 검증이 구분된다.
- 활성 실행은 한 개이고, Codex가 없어도 프로그램을 실행할 수 있다.
- 아직 모델을 연결하지 않았다면 Browser Agent/시각적 Computer Use가 완성됐다고 주장하지 않는다.

## 18. 확인한 공식 문서

아래 URL은 개발자가 공식 원문을 확인하기 위한 참조이다. 기능 지원과 제약은 개발 시 설치 버전으로 재확인하고 의존성을 고정한다. 이 문서의 아키텍처·API·상태 모델은 위 자료에 대한 설계 제안이며 해당 라이브러리의 표준 API가 아니다.

- [R01] Playwright locators: `https://playwright.dev/python/docs/locators`
- [R02] Playwright library, threading/Windows/cancellation: `https://playwright.dev/python/docs/library`
- [R03] Playwright actionability: `https://playwright.dev/python/docs/actionability`
- [R04] Playwright BrowserType, persistent profile: `https://playwright.dev/python/docs/api/class-browsertype`
- [R05] PyAutoGUI overview/FAQ/fail-safe: `https://pyautogui.readthedocs.io/en/latest/`
- [R06] pywinauto getting started: `https://pywinauto.readthedocs.io/en/latest/getting_started.html`
- [R07] pywinauto COM threading: `https://pywinauto.readthedocs.io/en/latest/HowTo.html`
- [R08] Microsoft Interactive Services: `https://learn.microsoft.com/en-us/windows/win32/services/interactive-services`
- [R09] Microsoft SendInput/UIPI: `https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput`
- [R10] Microsoft Inspect: `https://learn.microsoft.com/en-us/windows/win32/winauto/inspect-objects`
- [R11] Microsoft Task Scheduler security context: `https://learn.microsoft.com/en-us/windows/win32/taskschd/security-contexts-for-running-tasks`
- [R12] Playwright downloads: `https://playwright.dev/python/docs/downloads`
- [R13] PyAutoGUI screenshot/image search: `https://pyautogui.readthedocs.io/en/latest/screenshot.html`
- [R14] pywinauto remote execution: `https://pywinauto.readthedocs.io/en/latest/remote_execution.html`
- [R15] pywinauto keyboard: `https://pywinauto.readthedocs.io/en/latest/code/pywinauto.keyboard.html`
- [R16] OpenAI Codex Windows sandbox: `https://developers.openai.com/codex/windows` (확인한 리다이렉트: `https://learn.chatgpt.com/docs/windows/windows-sandbox`)
- [R17] OpenAI Codex AGENTS.md: `https://developers.openai.com/codex/guides/agents-md` (리다이렉트: `https://learn.chatgpt.com/docs/agent-configuration/agents-md`)
- [R18] Playwright codegen: `https://playwright.dev/python/docs/codegen`
