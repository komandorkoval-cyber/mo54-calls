[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^https://')]
    [string]$CrmUrl,
    [switch]$SkipScheduledTask,
    [switch]$SkipFfmpegInstall,
    [switch]$BrowserOnly
)

$ErrorActionPreference = 'Stop'
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$agentRoot = Join-Path $env:LOCALAPPDATA 'MO54CallsAgent'
$venv = Join-Path $agentRoot 'venv'
$pythonCandidates = @(
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
    (Join-Path $env:ProgramFiles 'Python312\python.exe')
)

$python = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $python) {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        $launcherProbe = & $launcher.Source -3.12 -c "import sys; print(sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $launcherProbe -match '^\(3, 12\)') {
            $python = $launcher.Source
        }
    }
}

if (-not $python) {
    throw 'Python 3.12 is required. Install it for the current user, then run this setup again.'
}

New-Item -ItemType Directory -Force -Path $agentRoot, (Join-Path $agentRoot 'audio'), (Join-Path $agentRoot 'models') | Out-Null
if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
    if ((Split-Path -Leaf $python) -ieq 'py.exe') {
        & $python -3.12 -m venv $venv
    } else {
        & $python -m venv $venv
    }
}

$venvPython = Join-Path $venv 'Scripts\python.exe'
& $venvPython -m pip install --upgrade pip
if ($BrowserOnly) {
    # First Novofon validation must not download ASR/PyTorch/model dependencies.
    & $venvPython -m pip install playwright httpx keyring tzdata
    & $venvPython -m pip install --no-deps $scriptRoot
} else {
    & $venvPython -m pip install $scriptRoot
}
& $venvPython -m playwright install msedge

if (-not $SkipFfmpegInstall -and -not (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue)) {
    if (Get-Command winget.exe -ErrorAction SilentlyContinue) {
        winget install --id Gyan.FFmpeg.Shared --exact --accept-source-agreements --accept-package-agreements
    } else {
        Write-Warning 'ffmpeg is not found. Install it manually and add it to PATH before preflight.'
    }
}

$configPath = Join-Path $agentRoot 'config.json'
if (-not (Test-Path $configPath)) {
    $config = Get-Content (Join-Path $scriptRoot 'agent_config.example.json') -Raw | ConvertFrom-Json
    $config.crm_url = $CrmUrl.TrimEnd('/')
    $config | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $configPath -Encoding utf8
    Write-Host "Created $configPath. Set Novofon URL/selectors after inspecting the dedicated Edge profile."
}

if (-not $SkipScheduledTask) {
    $agentExe = Join-Path $venv 'Scripts\mo54-agent.exe'
    $action = New-ScheduledTaskAction -Execute $agentExe -Argument 'run-once'
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(2)
    $trigger.Repetition.Interval = (New-TimeSpan -Minutes 30)
    $trigger.Repetition.Duration = (New-TimeSpan -Days 3650)
    $settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 3)
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName 'MO54CallsAgent' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    Write-Host 'Registered MO54CallsAgent: every 30 minutes, interactive user session, wake-to-run.'
}

if ($BrowserOnly) {
    Write-Host 'Browser-only installation complete. Configure Novofon selectors, then run mo54-agent provision-browser and mo54-agent inventory.'
} else {
    Write-Host 'Installation complete. Run: mo54-agent preflight, then mo54-agent provision-browser.'
}
