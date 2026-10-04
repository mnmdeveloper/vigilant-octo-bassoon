param([switch]$NonInteractive)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot

function Find-Python {
    $candidates = @(
        (Join-Path $ProjectRoot '.venv\Scripts\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe')
    )
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command) { $candidates += $command.Source }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            $version = & $candidate -c "import sys,struct; print('OK' if sys.version_info[:2]==(3,11) and struct.calcsize('P')==8 else 'NO')"
            if ($LASTEXITCODE -eq 0 -and $version -eq 'OK') { return $candidate }
        }
    }
    return $null
}

try {
    Write-Host 'Telegram Promo Assistant setup' -ForegroundColor Cyan
    $pythonExe = Find-Python
    if (-not $pythonExe) {
        $winget = Get-Command winget -ErrorAction SilentlyContinue
        if (-not $winget) { throw 'Install Python 3.11 x64 from python.org, then run install.cmd again.' }
        & $winget.Source install --source winget --id Python.Python.3.11 --exact --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) { throw 'Python installation failed.' }
        $pythonExe = Find-Python
        if (-not $pythonExe) { throw 'Python was not found. Restart install.cmd.' }
    }
    $venvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        & $pythonExe -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
    }
    & $venvPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
    $env:Path += ';' + [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
    if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue) -or -not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
        $winget = Get-Command winget -ErrorAction SilentlyContinue
        if (-not $winget) { throw 'FFmpeg is required. Install FFmpeg and add its bin directory to PATH.' }
        & $winget.Source install --source winget --id Gyan.FFmpeg --exact --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) { throw 'FFmpeg installation failed.' }
        $env:Path += ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
    }
    if (-not (Test-Path -LiteralPath '.env')) {
        Copy-Item -LiteralPath '.env.example' -Destination '.env'
        if (-not $NonInteractive) {
            Start-Process notepad.exe -ArgumentList ('"' + (Join-Path $ProjectRoot '.env') + '"')
        }
    }
    & $venvPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Dependency verification failed.' }
    & $venvPython -m app.doctor
    if ($LASTEXITCODE -ne 0) { throw 'Application dependency verification failed.' }
    Write-Host 'Setup complete. Fill in .env, then run start.cmd.' -ForegroundColor Green
    if (-not $NonInteractive) { Read-Host 'Press Enter to close' }
    exit 0
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
