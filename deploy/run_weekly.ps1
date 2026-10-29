# Windows 定时任务调用的脚本：运行一次，把输出追加到 data\agent.log。
# .env 由程序自己读取。
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
New-Item -ItemType Directory -Force -Path data | Out-Null
$env:PYTHONIOENCODING = "utf-8"
"===== $(Get-Date -Format o) =====" | Out-File -Append -Encoding utf8 data\agent.log
& .venv\Scripts\ibkr-agent.exe run *>&1 | Out-File -Append -Encoding utf8 data\agent.log
& .venv\Scripts\ibkr-agent.exe report *>&1 | Out-File -Append -Encoding utf8 data\agent.log
