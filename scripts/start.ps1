# 개발 환경 준비: 가상환경 생성 + 패키지 설치 (1단계는 표준 라이브러리 + pytest만 필요)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path "$root\.venv")) { python -m venv "$root\.venv" }
& "$root\.venv\Scripts\python.exe" -m pip install -q -e "$root[dev]"
& "$root\.venv\Scripts\python.exe" -m rpa preflight
