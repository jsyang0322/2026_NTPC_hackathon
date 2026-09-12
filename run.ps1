# Demo 一鍵啟動（Windows PowerShell 版，對應 run.sh）：載入設定 → 前置檢查 → 起 Streamlit。
#
# 程式本身已會自動載入 .env（見 core/env.py），這支腳本再多做兩件事：
#   1. 把 .env 也讀進當前行程的環境變數，讓還沒 import core 的子步驟（如 preflight
#      直接讀 os.environ）也拿得到值。
#   2. 跑 preflight_check 擋掉三個假資料陷阱，通過才啟動 Streamlit。
#
# 用法（在專案根目錄的 PowerShell 視窗）：
#   .\run.ps1                 # 檢查 → 啟動 Streamlit
#   .\run.ps1 -SkipCheck      # 跳過 preflight（不建議）
#   .\run.ps1 -PurgeCache     # 啟動前順手清掉 dry-run 假快取
#
# 若遇到「因為這個系統上已停用指令碼執行」錯誤，先在此視窗執行：
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

param(
    [switch]$SkipCheck,
    [switch]$PurgeCache
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

# --- 選 venv 的 python，沒有就退回系統 python ---
$Py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Write-Host "[run] 未找到 .venv，改用系統 python（建議先建立 .venv）。"
    $Py = "python"
}

# --- 1) 載入 .env（不存在則從範本複製）---
if ((-not (Test-Path ".env")) -and (Test-Path ".env.example")) {
    Write-Host "[run] 未找到 .env，從 .env.example 複製一份。"
    Copy-Item ".env.example" ".env"
}
if (Test-Path ".env") {
    Get-Content ".env" | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $name, $value = $line -split "=", 2
            [Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), "Process")
        }
    }
}

# --- 2) 前置檢查 ---
if (-not $SkipCheck) {
    Write-Host "[run] 執行前置檢查…"
    $preArgs = @("-m", "scripts.preflight_check")
    if ($PurgeCache) { $preArgs += "--purge-cache" }
    & $Py @preArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "[run] 前置檢查未通過。修正後再跑，或用 .\run.ps1 -SkipCheck 略過（不建議）。"
        exit 1
    }
}

# --- 3) 啟動 Streamlit ---
Write-Host "[run] 啟動 Streamlit…（提醒：側邊欄 Dry-run 開關預設為開，Demo 時請手動關閉）"
& $Py -m streamlit run app\streamlit_app.py
