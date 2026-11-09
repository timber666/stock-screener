<#
.SYNOPSIS
    在 Windows 工作排程器註冊盤中掃描、盤後更新、訊號成績單、全市場資料更新與模擬組合（PROJECT_PLAN.md 第 14 節 M5、
    RESEARCH_PLAN.md 4.1、4.3、4.6）。

.DESCRIPTION
    建立兩個每週一至週五執行的工作，與三個每週六執行的研究工作：
      stock-screener-scan    盤中掃描並推播 Discord（預設 13:10）
      stock-screener-update  盤後更新日 K（預設 21:00；FinMind 約 17:30 更新），失敗時推送錯誤通知
      stock-screener-weekly  訊號成績單：上一週推播的訊號，推播到研究頻道（預設週六 10:00，R3）
      stock-screener-market  全市場資料：補抓上一週的證交所、櫃買中心行情與事件到 market.db（預設週六 09:00，R9），
                             失敗時錯誤通知推到研究頻道
      stock-screener-portfolio  模擬組合：換股週結算上一次的名單、產生新名單並推播到研究頻道，其他週直接結束
                             （預設週六 09:30，R36）；失敗時錯誤通知推到研究頻道

    - 以目前使用者身分執行，電腦須開機且已登入；執行時會出現命令列視窗。
    - 國定假日照常觸發：scan 會判定為非交易日後結束、不推播；update 查不到新資料。
    - 錯過的 update、weekly、market、portfolio（例：當時關機）會在下次開機後補跑；錯過的 scan 不補跑，收盤後才推播盤中結果沒有意義。
    - weekly、market、portfolio 是研究功能：失敗時錯誤通知推到研究頻道，不影響每日的 scan、update（PROJECT_PLAN.md 10.4）。
    - 使用筆電電池時也會執行。
    - 重複執行本腳本會以新的設定覆蓋。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -ScanTime 13:05 -UpdateTime 19:00 -WeeklyTime 09:00

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -Unregister
#>
param(
    [string]$ScanTime = "13:10",
    [string]$UpdateTime = "21:00",
    [string]$WeeklyTime = "10:00",
    [string]$MarketTime = "09:00",
    [string]$PortfolioTime = "09:30",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$TaskNames = @("stock-screener-scan", "stock-screener-update", "stock-screener-weekly", "stock-screener-market",
    "stock-screener-portfolio")
$Weekdays = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")

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
        [timespan]$TimeLimit,
        [string[]]$Days,
        [string]$DaysLabel
    )
    $action = New-ScheduledTaskAction -Execute $Python -Argument "-m screener $Command" -WorkingDirectory $ProjectDir
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Days -At $Time
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable:$CatchUp -ExecutionTimeLimit $TimeLimit `
        -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings `
        -Description $Description -Force | Out-Null
    Write-Host "已註冊 $Name：$DaysLabel $Time 執行 python -m screener $Command"
}

Register-ScreenerTask -Name $TaskNames[0] -Time $ScanTime -Command "scan" -Days $Weekdays -DaysLabel "週一至週五" `
    -Description "台股盤中選股：掃描觀察清單並推播 Discord" -CatchUp $false -TimeLimit (New-TimeSpan -Minutes 10)
Register-ScreenerTask -Name $TaskNames[1] -Time $UpdateTime -Command "update" -Days $Weekdays -DaysLabel "週一至週五" `
    -Description "台股盤中選股：盤後更新日 K，失敗時推送錯誤通知" -CatchUp $true -TimeLimit (New-TimeSpan -Hours 1)
Register-ScreenerTask -Name $TaskNames[2] -Time $WeeklyTime -Command "signal-report" -Days @("Saturday") `
    -DaysLabel "每週六" -Description "台股盤中選股（研究）：上一週的訊號成績單，推播到研究頻道" -CatchUp $true `
    -TimeLimit (New-TimeSpan -Minutes 30)
Register-ScreenerTask -Name $TaskNames[3] -Time $MarketTime -Command "market-update" -Days @("Saturday") `
    -DaysLabel "每週六" -Description "台股盤中選股（研究）：補抓上一週的全市場行情與事件到 market.db" -CatchUp $true `
    -TimeLimit (New-TimeSpan -Hours 2)
Register-ScreenerTask -Name $TaskNames[4] -Time $PortfolioTime -Command "portfolio-track" -Days @("Saturday") `
    -DaysLabel "每週六" -Description "台股盤中選股（研究）：模擬組合的換股名單與績效，換股週推播到研究頻道" -CatchUp $true `
    -TimeLimit (New-TimeSpan -Minutes 30)

Write-Host "工作目錄：$ProjectDir。可在「工作排程器」程式中查看或手動執行；log 寫入 logs\。"
