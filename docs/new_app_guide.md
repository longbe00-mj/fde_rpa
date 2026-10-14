# 새 앱 붙이기 가이드 (앱 커넥터 + 업무)

LocalRPA는 **앱 커넥터**(앱당 1번)와 **업무**(업무당 1번, 얇게)를 나눠 만든다.
앱 N개 × 업무 M개를 N×M이 아니라 N + M으로 만들기 위해서다.

```
src/rpa/apps/<app_id>/          ← 앱 커넥터: 그 앱을 다루는 방법 전부
  connector.py                    AppInfo 등록 + 동작(열기·입력·저장·조회…) + 상태 판정
  selectors.json                  창 조건 / 선택자 / 좌표 / 단축키 / 수집 환경·검증 버전
  assets/*.png                    기준 이미지 (화면 기반 앱만)
  vision_profile.py               (선택) 화면 해석 상태 정의
src/rpa/workflows/<workflow_id>/workflow.py   ← 업무: 단계 순서 + 완료 검증 + 대조만
```

규칙
- 앱 고유의 표기(라벨·표 머리글·경로·좌표·기준 이미지)는 커넥터 안에만 둔다. 업무에는 정규화한 값(`customer_id`, `name`…)을 넘긴다.
- 커넥터는 **사실을 돌려주고**, 성공/실패 **판단은 업무 verifier**가 한다. 예: `new_chat()`은 기준 이미지 개수를 돌려주고 PASS 여부는 업무가 정한다.
- 커넥터 파일(코드·선택자·기준 이미지)은 그 앱을 쓰는 업무의 버전 해시에 포함된다 (`source_files=(__file__, *APP.source_files())`). 앱 지식이 바뀌면 진행 중 실행의 재개 시 감지된다.
- 존재하지 않는 selector·AutomationId·좌표를 추측해서 만들지 않는다. 모든 값은 아래 조사로 확인한다.

---

## 1단계 — 조작 방식 정하기 (5분)

```powershell
& $py -m rpa inspect-window --process app.exe          # 또는 --title "정규식" / --class 클래스명
```
읽기 전용이다. 창 기술·DPI·관리자 권한·UIA 노출 정도를 보고 추천한다.

| 추천 | 의미 | 사용할 어댑터 |
|---|---|---|
| `web` (웹 사이트) | 브라우저로 쓰는 업무 | `adapters/browser.py` (Playwright) |
| `desktop_uia` | UIA로 이름 있는 컨트롤이 보임 (Win32·WinForms·WPF·메모장 등) | pywinauto UIA (3단계에서 추가 예정) |
| `desktop_visual` | UIA에 내용이 없음 (Electron·Chromium·Java·원격 화면) | `adapters/desktop_visual.py` (기준 이미지) |
| `blocked` | 관리자 권한 앱 — 일반 Runner 입력이 UIPI로 막힘 | 앱을 일반 권한으로 실행해야 함 |

출력의 `selectors.json window 초안`을 커넥터 선택자의 `window`로 시작점 삼는다.

## 2단계 — 선택자 수집

**웹**: Playwright codegen으로 초안을 얻고 role/label/test id 기준으로 정리한다. 좌표·nth 금지.
```powershell
& $py -m playwright codegen --channel msedge <URL>
```
**UIA 앱**: Inspect.exe(Windows SDK) 또는 `inspect-window` 출력 표본에서 `control_type + name/automation_id`.

**화면 기반 앱** (Claude 앱 사례):
1. 업무의 각 상태 화면을 캡처한다 (예: 새 대화 / 입력 후 / 생성 중 / 완료 / 다른 대화). 대상 창이 맨 앞일 때만.
2. 상태마다 그 상태에만 있는 UI 조각을 원본 해상도에서 잘라 기준 이미지로 만든다. 깜빡이는 커서·시간 표시는 빼고, 아이콘 하나보다 아이콘 줄처럼 고유한 범위를 쓴다.
3. 교차 검증한다:
   ```powershell
   & $py -m rpa template-check -t assets\a.png -t assets\b.png 캡처1.png 캡처2.png 캡처3.png
   ```
   **좋은 기준 이미지 = 해당 상태 캡처에서만 1, 나머지 0.** 여러 곳(⚠)이면 더 넓게 잘라 다시 한다.
4. 클릭 지점은 기준 이미지 위치 + 오프셋(`copy_offset` 등)으로 정한다. 화면 절대 좌표는 쓰지 않는다.
5. 수집 환경을 `selectors.json`의 `environment`(DPI·배율·테마·언어·창 크기)와 `app_version`에 기록한다. 커넥터는 환경이 다르면 시작 전에 거부해야 한다.

## 3단계 — 커넥터 작성

`apps/<app_id>/connector.py` 골격:
```python
from rpa.apps.base import AppInfo, register_app

APP = register_app(AppInfo(app_id="my_app", name="내 앱", kind="desktop_visual",  # web | desktop_uia
                           description="…", package_dir=Path(__file__).resolve().parent))

class MyApp:
    app = APP
    def __init__(self, dv): self.dv, self.sel = dv, APP.selectors()
    def ensure_ready(self, env): ...   # 창 (재)연결 → 한 번 활성화 → 환경(DPI·크기·버전) 확인
    def <동작>(self, env, ...): ...     # 입력 후 readback 값을 돌려준다
    def lookup(self, key): ...          # 결과를 앱/시스템에서 다시 조회 (검증·대조용)

def make_connector(...):               # (원시 어댑터, 커넥터) — 어댑터는 엔진 훅용으로 업무 adapters에도 넣는다
```
필수 체크리스트
- [ ] `ensure_ready`: 재시작 후 재연결, 활성화는 한 번만, 환경이 수집 기준과 다르면 `PreconditionFailed`
- [ ] 변경 동작마다 **재조회 수단**(`lookup`)이 있다 — 없으면 그 업무는 무인화 대상이 아니다
- [ ] 입력은 readback 값을 돌려준다 (한글·특수문자·선행 0 확인)
- [ ] 화면 기반이면 상태 판정이 기준 이미지 개수로 되어 있고 "정확히 1개"일 때만 클릭한다
- [ ] 웹이면 로그인은 사용자가 직접 (`wait_for_login`), 허용 origin 설정
- [ ] `src/rpa/apps/base.py`의 `_load_builtin()`에 import 추가

## 4단계 — 업무 작성

`workflows/<workflow_id>/workflow.py`는 커넥터 동작을 조합한다.
- `make_adapters(data_dir, control, faults)`: `{"<원시 어댑터>": dv, "<앱>": connector, ...}`
- 각 단계의 `effect_kind`를 정확히: 읽기만 `READ_ONLY`, 로컬 파일·화면 상태 `LOCAL_CHANGE`, 외부 반영 `EXTERNAL_CHANGE`
- 변경 단계: `operation_key`(같은 업무 건이면 같은 키), `reconcile`(커넥터 `lookup`으로 APPLIED/NOT_APPLIED/UNKNOWN)
- verifier는 커넥터 반환값과 재조회 결과로 PASS/FAIL/UNKNOWN을 낸다. 토스트·클릭 반환만으로 PASS 금지
- `settings={"apps": [APP.app_id], ...}`, `source_files=(__file__, *APP.source_files())`
- `core/registry.py`의 `_load_builtin()`에 import 추가

여러 앱을 쓰는 업무는 커넥터를 여러 개 넣으면 된다 (예: 웹 포털 조회 → 테스트 폼 입력).

## 5단계 — 시험과 등록

| 시험 | 방법 |
|---|---|
| 커넥터 로직 | 가짜 어댑터로 단위 테스트 (`tests/unit/test_apps.py`의 `FakeDV` 참고) |
| 웹 | 로컬 테스트 사이트 + headless Edge 통합 테스트 (`tests/integration`) |
| 데스크톱 | opt-in 라이브 테스트 (`tests/live_windows`, `LOCALRPA_LIVE=1`) — 사용자가 허용한 시간·앱에서만 |
| 실제 앱 | 실제 계정·데이터로 1회 실행 후 `selectors.json`의 `verified_versions`에 버전과 결과를 기록 |

`rpa apps`로 등록·검증 버전·사용 업무를 확인한다.

## 유지보수

- 앱 업데이트 후: 상태 화면을 다시 캡처해 `template-check`로 기존 기준 이미지가 여전히 "해당 상태에서만 1"인지 확인 → 통과하면 `verified_versions`에 추가, 아니면 해당 이미지만 다시 수집.
- 배율·테마·언어가 다른 PC: 기준 이미지 세트를 그 환경용으로 따로 수집한다 (커넥터가 환경 불일치 시 거부).
- 선택자가 깨지면 실행은 "UI 변경 가능성"으로 멈춘다 (좌표로 대체하지 않음). 실패 증거(화면·trace)를 보고 2단계를 다시 한다.
