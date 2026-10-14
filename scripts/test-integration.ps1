# 웹 어댑터 통합 테스트 — 로컬 테스트 포털 + headless Edge (데스크톱 입력 없음, 1분 내외)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
& "$root\.venv\Scripts\python.exe" -m pytest "$root\tests\integration" @args
exit $LASTEXITCODE
