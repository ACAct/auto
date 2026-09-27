# ============================================================
#  智汇福大晚点名自动签到 · 静默入口
#  供 Windows 任务计划以隐藏窗口模式调用
#  不弹任何窗口、不抢焦点
#  所有路径硬编码（避免 $PSScriptRoot 在 -File 模式下为 null 的 bug）
# ============================================================

# 强制隐藏 PowerShell 用 UTF-8 输出（默认会用 GBK 导致 main.py 输出乱码）
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Continue"

# 硬编码绝对路径
$project = "D:\codes\GitHub\fzu-auto-checkin-main"
$logDir  = "$project\logs"
$logFile = "$logDir\checkin.log"
$python  = "D:\anaconda\envs\real\python.exe"

# 确保日志目录存在
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

# 切到项目目录——main.py 用相对路径找 config.yaml / src/
Set-Location -Path $project

# 执行签到脚本，输出追加到日志
& $python main.py 2>&1 | Out-File -FilePath $logFile -Encoding utf8 -Append

# 空一行分隔每次运行
Add-Content -Path $logFile -Value "" -Encoding utf8
