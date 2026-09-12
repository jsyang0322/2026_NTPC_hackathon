# Bootstrap the repository environment and run Henry's pipeline.
# Run this from the workspace folder in a normal PowerShell window.
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $RepoRoot

function Get-RealPython {
    # The Store stub lives under WindowsApps and does not actually run code.
    $cand = Get-Command py -ErrorAction SilentlyContinue
    if ($cand) { return "py" }
    $cand = Get-Command python -ErrorAction SilentlyContinue
    if ($cand -and $cand.Source -notlike "*WindowsApps*") { return $cand.Source }
    return $null
}

$py = Get-RealPython
if (-not $py) {
    Write-Host "No real Python found. Installing Python 3.12 via winget..."
    winget install --id Python.Python.3.12 -e --source winget `
        --accept-package-agreements --accept-source-agreements
    Write-Host ""
    Write-Host "Python installed. CLOSE this window, open a NEW PowerShell, and run this script again."
    Write-Host "(PATH updates only apply to new shells.)"
    exit 0
}

Write-Host "Using Python launcher: $py"

# 1. Create the virtual environment named .venv
if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    & $py -m venv .venv
    Write-Host "Created .venv"
} else {
    Write-Host ".venv already exists"
}

$venvPy = ".\.venv\Scripts\python.exe"

# 2. Activate (Windows PowerShell). On macOS/Linux this would be: source .venv/bin/activate
& ".\.venv\Scripts\Activate.ps1"

# 3. Install packages into the venv
& $venvPy -m pip install --upgrade pip
& $venvPy -m pip install -r requirements.txt

# 4. Run the pipeline with the venv's Python
& $venvPy ".\henry\workflow_appeal.py"
