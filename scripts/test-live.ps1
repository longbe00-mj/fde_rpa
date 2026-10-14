# 라이브 Windows 테스트 — 실제 마우스·키보드를 사용한다 (테스트 폼 앱만 조작).
# 실행 중에는 마우스·키보드를 만지지 마세요. 비상 중단: 마우스를 화면 모서리로.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$env:LOCALRPA_LIVE = "1"
try {
    & "$root\.venv\Scripts\python.exe" -m pytest "$root\tests\live_windows" -v @args
    exit $LASTEXITCODE
} finally {
    Remove-Item Env:\LOCALRPA_LIVE -ErrorAction SilentlyContinue
}
