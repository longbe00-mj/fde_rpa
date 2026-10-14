# 단위 테스트 (GUI 입력 없음). Windows 읽기 전용 측정 테스트도 포함된다.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
& "$root\.venv\Scripts\python.exe" -m pytest @args
exit $LASTEXITCODE
