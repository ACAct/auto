# ============================================================
#  关机/注销前自动签到脚本
#  由 Windows 组策略（本地计算机策略 → 注销脚本）调用
#  设计要点：
#    1. 有时间窗口判定——非晚点名时段（21:30-23:59）直接跳过，
#       不然白天关机也会触发签到请求，浪费时间
#    2. 超时收紧到 10 秒——关机过程中系统给脚本的执行时间有限
#    3. 静默执行——关机屏幕上别蹦弹窗；日志写到 logs/ 方便事后查
#    4. 不重定向 main.py 输出（main.py 自己会打日志到 logs/checkin.log）
# ============================================================

param()

$ErrorActionPreference = "SilentlyContinue"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# ---- 1. 时间窗口判定（北京时间 21:30 - 次日 00:00）----
try {
    $bjNow = [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId(
        [DateTime]::Now, "China Standard Time")
    $hhmm = $bjNow.ToString("HHmm")
    $inWindow = $hhmm -ge "2130" -and $hhmm -lt "2359"
    # 23:59 之后算"当天已关窗"，也跳过（main.py 自己会识别但省一个网络请求）
} catch {
    $inWindow = $true   # 拿不到时间就放行，让 main.py 自己判断
}

if (-not $inWindow) {
    exit 0
}

# ---- 2. 确保日志目录存在 ----
if (-not (Test-Path "logs")) { New-Item -ItemType Directory -Path "logs" -Force | Out-Null }

# ---- 3. 执行签到（进程超时 15 秒，避免卡死关机）----
$python = "D:\anaconda\envs\real\python.exe"
if (-not (Test-Path $python)) {
    $python = "python.exe"   # 兜底：PATH 里找
}

# 用 Start-Process + Wait-Process -Timeout 控制最长等待时间
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $python
$psi.Arguments = "main.py"
$psi.WorkingDirectory = $root
$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $false   # main.py 自己写日志
$psi.RedirectStandardError = $false

$proc = [System.Diagnostics.Process]::Start($psi)
if ($proc) {
    $proc.WaitForExit(15000) | Out-Null   # 等 15 秒
    if (-not $proc.HasExited) {
        $proc.Kill()                      # 超时强杀，不耽误关机
    }
}

exit 0   # 无论成功失败都返回 0，关机流程不中断
