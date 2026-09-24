# ============================================================
#  常驻后台监听器：一登录就启动，监听 System Event Log 的关机事件
#  Event ID 6006 = "The Event log service was stopped"
#     → Windows 关机/重启前几秒一定会写这条
#
#  策略：无限 while + try/catch 全包裹，任何异常都 Sleep 2 秒重来
#        死循环永远不退出，这样关机前的最后一次轮询总能命中
# ============================================================

$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$stateFile = Join-Path $env:TEMP "fzu_shutdown_listener_last.json"

if (-not (Test-Path $stateFile)) { '{"last": null}' | Out-File $stateFile }

while ($true) {
    try {
        Start-Sleep -Seconds 2

        $state = Get-Content $stateFile -Raw | ConvertFrom-Json
        $cutoff = if ($state.last) { [DateTime]$state.last } else { (Get-Date).AddMinutes(-2) }

        $events = Get-WinEvent -FilterHashtable @{
            LogName   = 'System'
            Id        = 6006
            StartTime = $cutoff
        } -MaxEvents 3 -ErrorAction SilentlyContinue

        foreach ($evt in $events) {
            $evtTime = $evt.TimeCreated
            @{ last = $evtTime.ToString("o") } | ConvertTo-Json | Out-File $stateFile

            # 异步触发签到
            Start-Process -FilePath "powershell.exe" `
                -ArgumentList "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\shutdown_checkin.ps1`"" `
                -WindowStyle Hidden
        }
    } catch {
        # 任何异常（包括进程被强杀前的中断）都静默处理，继续循环
        try { Start-Sleep -Seconds 1 } catch {}
    }
}
