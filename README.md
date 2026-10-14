# LocalRPA

Windows PC 한 대에서 웹 + Windows 앱 업무를 실행하는 로컬 RPA. 명세: `docs/windows_rpa_implementation_spec.md`

## 현재 단계

- 1단계: 엔진·계약·상태·FakeAdapter·CLI
- 2단계: PyAutoGUI 기반 DesktopVisual 어댑터 (`adapters/desktop_visual.py`) + 테스트 폼 라이브 업무 `demo_test_form`
- 화면 기반 업무 `claude_prompt` (Claude 데스크톱 앱에 프롬프트 전송 → 응답 저장. 배율 125%·라이트 테마·한국어 기준 이미지)
- 실행 결과 리포트 `rpa report` / `--report` (`core/report.py`): 요청자에게 결과를 전달하는 report.md·report.json

- 3단계(실제 앱): Claude 데스크톱 앱 업무 `claude_prompt` — 새 대화 → 프롬프트 입력·readback → 전송 → 응답 완료 대기 → 응답 파일 저장
  - Claude 앱은 UIA로 내부 컨트롤을 노출하지 않아(renderer 접근성 비활성) 화면 기반으로 조작한다
  - 상태 판정은 실제 화면에서 잘라 교차 검증한 기준 이미지(`apps/claude_desktop/assets`)로 한다 — 125% 배율·라이트 테마 기준
  - 대상 창을 덮은 다른 앱 창은 최소화했다가 실행 종료 시 복원한다 (`minimize_obstructions`, 이 업무만 사용)

- 4a(화면 해석): `vision/` — 기준 이미지 판정이 실패한 지점에서만 Claude Opus 5.5가 스크린샷의 상태를 판정·설명
  - Computer Use 도구 미사용. 이미지 + structured output(json_schema)으로 허용된 상태 중 하나만 받음. 모델 판단으로 입력이 생기지 않음
  - 판정은 사용자에게 보여 줄 진단(WAITING_USER 사유에 첨부)·'더 기다릴지' 결정에만 쓰고 RULE 검증을 대신하지 않음
  - 대상 창 영역만 전송(Claude 앱은 사이드바 제외), 대상 창이 가려졌으면 전송하지 않음. 화면 속 지시문은 따르지 않고 표시만
  - 거절은 `fallbacks: "default"`, 실행당 호출 6회 상한, 보낸 이미지·판정·토큰·추정 비용을 증거로 저장

- 5단계(웹): `adapters/browser.py` (Playwright 동기 API) + 업무 `demo_web_portal` (테스트 포털 `fixtures/web/portal_site.py`)
  - RPA 전용 프로필(`<데이터>\browser_profiles\portal`), PC에 설치된 Edge 사용(브라우저 다운로드 불필요), 실사용은 headful
  - 로그인은 사용자가 RPA 브라우저에서 직접 (자격 증명 입력·저장 안 함). 로그인 화면이면 안내 후 대기, 시간 초과 시 WAITING_USER
  - 허용 origin 밖 최상위 이동(스크립트·새 창 포함) 차단. 요소는 role/label/test id로 정확히 1개일 때만 조작
  - 요소를 못 찾으면 원인 분류: 로그인 필요 / 권한 없음(401·403) / 서버 오류(5xx, 재시도) / 로딩 중 / UI 변경 — 좌표 클릭으로 대체하지 않음
  - 입력 후 readback, 다운로드 이벤트로 실행 폴더에 저장 후 내용 검증, 등록은 서버 재조회로 대조(응답 유실 시 재전송 없음)
  - 실패 시 브라우저 화면 + Playwright trace(zip) 증거

- 웹 실업무: 업무 `naver_news` — www.naver.com 뉴스스탠드 '언론사편집' 탭에서 기사 N개(기본 10)를 페이지를 넘기며 수집 → JSON·Markdown 저장
  - 사이트에 쓰는 동작 없음 (탭 전환·페이지 넘김만, 기사 링크는 열지 않음). 허용 origin은 www.naver.com 하나
  - 선택자 `workflows/naver_news/selectors.json`: 2026-10-14 실제 페이지 조사. 영역·탭·버튼은 role/이름, 기사 항목은 CSS 모듈 클래스 접두어
  - 검증: 요청 수만큼, URL 중복 없음, 제목·언론사 있음, `n.news.naver.com/article/..` 형식. 저장 파일은 다시 읽어 대조
  - 결과: `<데이터>\outputs\naver_news\<request_id>.md|json`. 통합 테스트는 가짜 네이버(`fixtures/web/naver_like_site.py`)로만 함
  - 브라우저 어댑터에 읽기 전용 `extract(spec, js)`·`attr(spec, name)` 추가 (정확히 하나인 요소에만)

Computer Use 조작(4b/4c)은 모델 API가 필요해 보류 중이다.

### 화면 해석 키·평가
```powershell
& $py -m rpa vision-key            # Anthropic API 키를 Windows 자격 증명 관리자에 저장 (입력 숨김, 출력 안 함)
& $py -m rpa vision-key --check    # 저장 여부만 확인
& $py -m rpa vision-eval .localrpa\eval\claude_screens --max-usd 1   # 라벨 세트로 정확도·비용 측정 (실제 API 호출)
```
**업무 중 화면 해석은 기본 꺼짐**이다. 켜려면 `$env:LOCALRPA_VISION = "on"` (API 키·API 과금 필요).
꺼져 있으면 캡처·API 호출 없이, 판정 실패 시 진단 없이 사용자 확인으로 넘긴다.
키는 환경 변수 `ANTHROPIC_API_KEY`가 있으면 그것을 먼저 쓰고, 없으면 자격 증명 관리자의 키를 쓴다.
평가 세트(`labels.json` + 실제 화면 캡처)는 개인 화면이 포함되므로 저장소 밖 데이터 폴더에 둔다.

### 클립보드 정책
입력값 readback·응답 복사에 클립보드를 쓴다. 원래 내용이 일반 텍스트면 끝난 뒤 되돌리고, 서식·이미지나
**회사 보호 데이터(`EnterpriseDataProtectionId`)** 가 있으면 덮어쓰지 않고 멈춘다 — 텍스트만 복원하면 보호 꼬리표가 빠지기 때문.
이때는 사용자가 클립보드를 비운 뒤 `resolve done --resume` 한다.

### DesktopVisual 어댑터 지원 범위
- 주 모니터 안의 창만. 프로세스는 Per-Monitor DPI v2(물리 픽셀), 좌표는 대상 창 클라이언트 기준 (`selectors.json`)
- 입력 직전 검사: 취소 플래그, FAILSAFE, 대상 창 존재·포그라운드, 좌표가 클라이언트·모니터 안, 다른 창에 가려지지 않음
- 포커스는 단계 시작 시 한 번만 활성화 시도. 입력 중 포커스가 바뀌면 멈추고 사용자 확인(WAITING_USER)
- 한글·특수문자: SendInput 유니코드(IME 비의존). 입력 후 클립보드 복사로 값 readback (원래 클립보드 텍스트 복원,
  서식·이미지가 있으면 덮어쓰지 않음)
- 이미지 기준점은 대상 창 안에서 정확히 한 곳일 때만 사용. 창이 나타난 직후엔 화면이 안정될 때까지 대기
- 관리자 권한 앱(UIPI) 거부. 이 실행이 띄운 앱만 닫음
- 전역 단축키: Ctrl+Alt+Q 취소, Ctrl+Alt+P 일시정지/재개 (단계 경계에서 적용). 비상 중단: 마우스를 화면 모서리로

## 앱 커넥터 구조 (앱 N개 × 업무 M개 → N + M)

앱 하나를 다루는 방법(창 조건·선택자·기준 이미지·동작·상태 판정)은 `src/rpa/apps/<app_id>/` **커넥터**에 한 번 만들고,
업무(`src/rpa/workflows/`)는 커넥터 동작을 조합해 단계 순서·완료 검증·대조만 정의한다. 새 앱 절차: `docs/new_app_guide.md`

| 커넥터 | 종류 | 사용 업무 |
|---|---|---|
| `claude_desktop` | 화면 기반 | `claude_prompt` |
| `test_form` | 화면 기반 | `demo_test_form` |
| `web_portal` | 웹 | `demo_web_portal` |

```powershell
& $py -m rpa apps                                          # 커넥터·검증 버전·사용 업무
& $py -m rpa inspect-window --process app.exe              # 새 앱 조사 → 조작 방식(UIA/화면/웹) 추천 (읽기 전용)
& $py -m rpa template-check -t a.png -t b.png s1.png s2.png  # 기준 이미지 교차 검증
```

## 준비

```powershell
.\scripts\start.ps1          # .venv 생성, 설치, preflight 실행
pip install -e ".[desktop]"  # 2단계 데스크톱 어댑터 의존성 (pyautogui, opencv-python, pillow)
.\scripts\test-unit.ps1      # 단위 테스트 (실제 입력 없음)
.\scripts\test-live.ps1      # 라이브 테스트: 실제 마우스·키보드로 테스트 폼만 조작 (실행 중 PC 조작 금지)
.\scripts\test-integration.ps1  # 웹 통합 테스트: 테스트 포털 + headless Edge (데스크톱 입력 없음)
```

웹 데모 실행 (브라우저 창이 열리고, 처음엔 그 창에서 rpa / test로 로그인):
```powershell
& $py fixtures\web\portal_site.py --store "$env:LOCALAPPDATA\LocalRPA\portal_store.json"   # 별도 창에서 포털 실행 (127.0.0.1:8765)
& $py -m rpa run demo_web_portal -i customer_id=DEMO-002 -i "memo=방문 요청" --report
```
`LOCALRPA_PORTAL_URL`로 포털 주소, `LOCALRPA_BROWSER_HEADLESS=1`로 창 없이 실행.

## 사용

```powershell
$py = ".\.venv\Scripts\python.exe"
& $py -m rpa preflight                                     # 해상도·배율·모니터·잠금·RDP·실제 캡처 점검
& $py -m rpa workflows
& $py -m rpa run demo_web_to_desktop -i customer_id=DEMO-001
& $py -m rpa run demo_test_form -i customer_id=CLI-001 -i "name=홍길동" -i phone=0010-0200   # 실제 입력
& $py -m rpa run claude_prompt -i request_id=REQ-001 -i "prompt=요약해 주세요: ..."          # Claude 앱 (실제 전송)
& $py -m rpa run naver_news -i request_id=NEWS-001 [-i count=10] --report                 # 네이버 메인 뉴스 수집 (웹, 읽기만)
& $py -m rpa status                                        # 실행 목록
& $py -m rpa status <run_id>                               # 상세 (operations/attempts/interventions/evidence)

# 장애 주입 (FakeAdapter)
& $py -m rpa run demo_web_to_desktop -i customer_id=DEMO-001 --fault desktop.save=crash_after_apply
& $py -m rpa resume <run_id>                               # 재조회로 대조 후 재개 (중복 저장 없음)
& $py -m rpa run demo_web_to_desktop -i customer_id=DEMO-001 --fault web.get_customer=login_expired
& $py -m rpa resolve <run_id> --resolution done --note "다시 로그인함" --resume
& $py -m rpa cancel <run_id>

# 실행 결과 리포트 (전달용) — 실행 폴더 runs\<run_id>\report.md, report.json
& $py -m rpa run demo_web_to_desktop -i customer_id=DEMO-001 --report    # 끝나면 자동 생성 (resume/resolve에도 --report)
& $py -m rpa report [<run_id>]                             # 생략하면 최근 실행. --stdout: 파일 대신 출력, --json: 전체 JSON
& $py -m rpa --json workflows                              # 업무 목록 JSON (입력·단계 effect_kind·changes_data)
```

리포트 구성: 결론(SUCCESS / COMPLETED_UNVERIFIED / NEEDS_USER / FAILED / CANCELLED / INCOMPLETE) → 다음 할 일 →
산출물(경로·sha256 재확인 무결성·텍스트 미리보기) → 입력 → 단계별 결과 → 단계 출력값 → 사용자 조치 이력 →
실패 화면 → 기타 증거 → 변경 작업 기록. Hermes 스킬 `local-rpa`가 이 리포트로 결과를 전달한다.

장애 종류: `web.get_customer=transient|login_expired`,
`desktop.save=error_before_apply|error_after_apply|crash_before_apply|crash_after_apply|corrupt|slow`

데이터 위치: `%LOCALAPPDATA%\LocalRPA` (또는 `LOCALRPA_HOME`, `--data-dir`). `--json`이면 stdout에 JSON 이벤트만 출력한다.

종료 코드: 0 완료, 3 사용자 조치 대기, 4 취소, 1 실패, 2 입력/상태 오류, 70 시뮬레이션된 강제 종료.

## 구조

```
src/rpa/
  cli.py                       CLI
  core/contracts.py            Workflow/Step/ActionResult/CheckResult/상태 enum
  core/engine.py               단계 실행 알고리즘, 대조(reconcile), 사용자 조치, 재개
  core/run_store.py            SQLite (runs, step_attempts, operations, evidence, user_interventions, events)
  core/control.py              pause/resume/cancel 플래그, 취소 가능한 대기
  core/ownership.py            단일 Runner 잠금 인터페이스
  core/report.py               실행 결과 리포트 (report.md/report.json)
  core/screen_scale.py         Computer Use 스크린샷 축소·좌표 역변환
  platform/windows/display.py  모니터·DPI·잠금·RDP 측정, GDI 캡처 점검
  platform/windows/preflight.py 측정값 기반 실행 가능 판정
  platform/windows/instance_lock.py  named mutex
  adapters/fake.py             가짜 웹·앱 + 장애 주입
  adapters/desktop_visual.py   PyAutoGUI 화면 입력 어댑터
  platform/windows/win32.py    창·포커스·SendInput·클립보드·권한(UIPI)
  platform/windows/hotkeys.py  전역 취소/일시정지 단축키
  workflows/demo_fake/         데모 업무: 웹 조회 → 앱 저장 → 결과 파일
  workflows/demo_test_form/    라이브 업무: 테스트 폼 입력 → 저장 → 재조회 (selectors.json)
  apps/base.py                 AppInfo(커넥터 메타데이터)·목록, 커넥터 파일을 업무 버전 해시에 포함
  apps/claude_desktop/         커넥터: Claude 앱 (selectors.json + assets/ + vision_profile.py)
  apps/test_form/              커넥터: 테스트 폼
  apps/web_portal/             커넥터: 테스트 포털 (표 머리글 → customer_id 등 정규화)
  tools/                       새 앱 조사 도구 (inspect_window, template_check)
  workflows/common.py          업무 공통 도우미 (rule)
  workflows/claude_prompt/     업무: Claude 앱 프롬프트 전송 → 응답 저장
  adapters/browser.py          Playwright 웹 어댑터 (전용 프로필·origin 차단·정확히 1개 요소·원인 분류·다운로드·trace)
  workflows/demo_web_portal/   웹 업무: 로그인 확인 → 검색 → 상세(iframe) → CSV 다운로드 검증 → 접수 등록(재조회)
  workflows/naver_news/        웹 업무: 네이버 메인 → 언론사편집 기사 N개 수집(페이지 넘김) → JSON·MD 저장
  vision/contracts.py          ScreenObservation / AppProfile / ScreenAssessment
  vision/interpreter.py        Opus 5.5 화면 상태 판정 (structured output, 거절 fallback, 호출 상한, 비용 기록)
fixtures/windows/              테스트 폼 앱, 방해 창 앱
fixtures/web/portal_site.py    테스트 업무 포털 (로그인·조회·iframe·다운로드·업로드·장애 모드)
fixtures/web/naver_like_site.py  naver_news 테스트용 가짜 네이버 메인 (합성 기사, 모드: normal/overlap/ui_v2/short)
tests/integration/             웹 통합 테스트 (headless Edge)
tests/live_windows/            opt-in 라이브 테스트 (LOCALRPA_LIVE=1)
```
