param([switch]$Browser, [switch]$Stop)
$ErrorActionPreference = 'Stop'
$archiveRoot = Join-Path $PSScriptRoot 'archive'
$archivePython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
$archiveInstancePath = Join-Path $archiveRoot '.cache/app-instance.json'
if (-not (Test-Path -LiteralPath $archivePython)) { throw 'Missing .venv. Follow README.md installation instructions.' }
$archiveInstance = $null
if (Test-Path -LiteralPath $archiveInstancePath) {
    $archiveInstance = Get-Content -LiteralPath $archiveInstancePath -Raw -Encoding UTF8 | ConvertFrom-Json
}
if ($Stop) {
    if ($archiveInstance) {
        $archiveParts = $archiveInstance.url -split '#', 2
        Invoke-RestMethod -Method Post -Uri ($archiveParts[0] + 'api/quit') -Headers @{ Authorization = ('Bearer ' + $archiveParts[1]) } -ContentType 'application/json' -Body '{}' -TimeoutSec 5 | Out-Null
        $archiveStopDeadline = [DateTime]::UtcNow.AddSeconds(10)
        while ((Test-Path -LiteralPath $archiveInstancePath) -and [DateTime]::UtcNow -lt $archiveStopDeadline) {
            Start-Sleep -Milliseconds 200
        }
        if (Test-Path -LiteralPath $archiveInstancePath) { throw 'Service is still finishing its tasks; wait before reopening. No process was killed.' }
    }
    exit 0
}
if ($archiveInstance) {
    try {
        $archiveParts = $archiveInstance.url -split '#', 2
        $archiveHealth = Invoke-RestMethod -Uri ($archiveParts[0] + 'api/status') -Headers @{ Authorization = ('Bearer ' + $archiveParts[1]) } -TimeoutSec 3
        if ($archiveHealth.archive_path) {
            $archiveOpenArguments = @('-m', 'experiment_archive.app', '--open-url', $archiveInstance.url)
            if (-not $Browser) { $archiveOpenArguments += '--desktop' }
            & $archivePython @archiveOpenArguments
            exit $LASTEXITCODE
        }
    } catch { }
}
$archiveArguments = @('-m', 'experiment_archive.app', '--archive', ('"' + $archiveRoot + '"'))
$archiveConfig = Join-Path $PSScriptRoot 'pipeline.json'
if (Test-Path -LiteralPath $archiveConfig) { $archiveArguments += @('--config', ('"' + $archiveConfig + '"')) }
if (-not $Browser) { $archiveArguments += '--desktop' }
$archiveLogDirectory = Join-Path $archiveRoot '.cache'
New-Item -ItemType Directory -Path $archiveLogDirectory -Force | Out-Null
Start-Process -FilePath $archivePython -ArgumentList $archiveArguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $archiveLogDirectory 'app.stdout.log') -RedirectStandardError (Join-Path $archiveLogDirectory 'app.stderr.log') | Out-Null
