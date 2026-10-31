<#
.SYNOPSIS
    在 Windows 工作排程器註冊盤中掃描與盤後更新（PROJECT_PLAN.md 第 14 節 M5）。

.DESCRIPTION
    建立兩個每週一至週五執行的工作：
      stock-screener-scan    盤中掃描並推播 Discord（預設 13:10）
      stock-screener-update  盤後更新日 K（預設 21:00；FinMind 約 17:30 更新），失敗時推送錯誤通知

    - 以目前使用者身分執行，電腦須開機且已登入；執行時會出現命令列視窗。
    - 國定假日照常觸發：scan 會判定為非交易日後結束、不推播；update 查不到新資料。
    - 錯過的 update（例：當時關機）會在下次開機後補跑；錯過的 scan 不補跑，收盤後才推播盤中結果沒有意義。
    - 使用筆電電池時也會執行。
    - 重複執行本腳本會以新的設定覆蓋。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -ScanTime 13:05 -UpdateTime 19:00

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -Unregister
#>
param(
    [string]$ScanTime = "13:10",
    [string]$UpdateTime = "21:00",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$TaskNames = @("stock-screener-scan", "stock-screener-update")

if ($Unregister) {
    foreach ($name in $TaskNames) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "已移除 $name"
        }
    }
    return
}

if (-not (Test-Path $Python)) {
    throw "找不到 $Python。請先建立虛擬環境並安裝（見 README 的「安裝」）。"
}

function Register-ScreenerTask {
    param(
        [string]$Name,
        [string]$Time,
        [string]$Command,
        [string]$Description,
        [bool]$CatchUp,
        [timespan]$TimeLimit
    )
    $action = New-ScheduledTaskAction -Execute $Python -Argument "-m screener $Command" -WorkingDirectory $ProjectDir
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $Time
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable:$CatchUp -ExecutionTimeLimit $TimeLimit `
        -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings `
        -Description $Description -Force | Out-Null
    Write-Host "已註冊 $Name：週一至週五 $Time 執行 python -m screener $Command"
}

Register-ScreenerTask -Name $TaskNames[0] -Time $ScanTime -Command "scan" `
    -Description "台股盤中選股：掃描觀察清單並推播 Discord" -CatchUp $false -TimeLimit (New-TimeSpan -Minutes 10)
Register-ScreenerTask -Name $TaskNames[1] -Time $UpdateTime -Command "update" `
    -Description "台股盤中選股：盤後更新日 K，失敗時推送錯誤通知" -CatchUp $true -TimeLimit (New-TimeSpan -Hours 1)

Write-Host "工作目錄：$ProjectDir。可在「工作排程器」程式中查看或手動執行；log 寫入 logs\。"
