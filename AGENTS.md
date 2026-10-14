# AGENTS.md — LocalRPA

명세: `docs/windows_rpa_implementation_spec.md` (작업 전 반드시 읽을 것)

## 확정 사항
- 대상: 웹 사이트 + Windows 앱 혼합 Workflow, Windows PC 한 대, 활성 Runner 최대 1개
- LLM: Anthropic API 직접 호출. Computer Use는 `computer_toolset_20260801`
- 업무 화면 스크린샷의 API 전송 허용
- 해상도/배율은 런타임 화면 측정(`rpa preflight`)으로 점검한다

## 규칙
- 앱 고유 지식(창 조건·선택자·라벨·표 머리글·경로·좌표·기준 이미지)은 `src/rpa/apps/<app_id>/` 커넥터에만 둔다. 업무(`workflows/`)는 커넥터 동작을 조합하고 완료 검증·대조만 정의한다. 새 앱 절차: `docs/new_app_guide.md`
- 커넥터는 사실(readback·재조회 값·기준 이미지 개수)을 돌려주고, PASS/FAIL 판단은 업무 verifier가 한다.
- `src/rpa/core`는 GUI 라이브러리(pyautogui, pywinauto, playwright)를 import하지 않는다. GUI 의존은 adapters에서 지연 import.
- 실행 상태(execution_status)와 업무 결과(business_outcome)를 섞지 않는다.
- 변경(EXTERNAL_CHANGE/LOCAL_CHANGE) 단계는 실행 의도를 먼저 영구 저장하고, timeout·예외만으로 재실행하지 않는다. reconcile로 미반영을 확인한 경우에만 재실행.
- 존재하지 않는 selector, AutomationId, 좌표를 추측해서 만들지 않는다.
- stdout은 IPC(JSON 이벤트) 전용, 일반 로그는 stderr.
- 비밀값·실사용 업무 데이터를 저장소에 넣지 않는다.

## 테스트
- `scripts/test-unit.ps1` 또는 `.venv\Scripts\python -m pytest`
- 보고 시 "실행한 자동 테스트 / 실행한 라이브 테스트 / 미실행 테스트와 사유"를 구분한다.
