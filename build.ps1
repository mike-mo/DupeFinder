$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    python -m venv (Join-Path $ProjectRoot ".venv")
}

& $Python -m pip install --quiet --disable-pip-version-check -r (Join-Path $ProjectRoot "requirements.txt")
& $Python -m PyInstaller --noconfirm --clean (Join-Path $ProjectRoot "DupeFinder.spec")

Write-Host "Built $ProjectRoot\dist\DupeFinder.exe"
