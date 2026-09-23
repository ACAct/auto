@echo off
REM ============================================================
REM  智汇福大晚点名自动签到 · 本地批处理入口
REM  由 Windows 任务计划程序每日定时调用
REM ============================================================

REM 切 UTF-8 代码页（main.py 也强制了 UTF-8，双保险）
chcp 65001 >nul

REM 切到项目根目录——main.py 用相对路径找 config.yaml / src/
cd /d "%~dp0"

REM 确保日志目录存在
if not exist "logs" mkdir logs

REM 通过 PowerShell 管道执行 Python 并追加 UTF-8 BOM 日志
REM 为什么：cmd 的 >> 重定向写不出 BOM，PowerShell 的 Out-File -Encoding utf8 会写带 BOM 的 UTF-8，
REM 这样以后 PowerShell / VSCode / Notepad 全都能正确识别，不会再乱码。
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "& { & 'D:\anaconda\envs\real\python.exe' main.py 2>&1 | Out-File -FilePath 'logs\checkin.log' -Encoding utf8 -Append }"

REM 空一行分隔每次运行，用 PowerShell 追加保持同样编码
powershell -NoProfile -Command ^
  "Add-Content -Path 'logs\checkin.log' -Value '' -Encoding utf8"
