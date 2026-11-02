# Run from any working directory; all archive paths stay under this project.
$archivePython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $archivePython)) {
    Write-Error 'Missing virtual environment. Follow README.md setup instructions.'
    exit 2
}
$archiveArguments = @($args)
if ($archiveArguments.Count -gt 0 -and $archiveArguments[0] -eq 'sync' -and
    -not ($archiveArguments | Where-Object { $_ -eq '--config' -or $_ -like '--config=*' })) {
    $archiveArguments += @('--config', (Join-Path $PSScriptRoot 'pipeline.json'))
}
& $archivePython -m experiment_archive --archive (Join-Path $PSScriptRoot 'archive') @archiveArguments
exit $LASTEXITCODE
