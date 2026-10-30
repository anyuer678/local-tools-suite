param(
    [string]$ReleaseDir = "",
    [string[]]$Apps = @("EnvForge", "RunTrace", "SpacePilot", "QuickDeck", "RecallFS"),
    [switch]$KeepInstalled
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($ReleaseDir)) { $ReleaseDir = $PSScriptRoot }
$results = @()

$hashFile = Join-Path $ReleaseDir "SHA256SUMS.txt"
$expected = @{}
if (Test-Path $hashFile) {
    Get-Content $hashFile | ForEach-Object {
        if ($_ -match "^([0-9a-f]{64})  (.+)$") {
            $expected[$Matches[2]] = $Matches[1]
        }
    }
}

foreach ($app in $Apps) {
    $setupRel = "installers\$app-0.1.0-setup.exe"
    $setup = Join-Path $ReleaseDir $setupRel
    $installDir = Join-Path $env:LOCALAPPDATA "Programs\$app"
    $exe = Join-Path $installDir "$app.exe"
    $uninstaller = Join-Path $installDir "unins000.exe"
    $record = [ordered]@{ App = $app; Hash = "SKIP"; Install = "FAIL"; Launch = "FAIL"; Title = ""; Uninstall = "FAIL"; Milliseconds = 0 }

    if (-not (Test-Path $setup)) {
        $record.Install = "FAIL (installer missing)"
        $results += [pscustomobject]$record
        continue
    }

    if ($expected.ContainsKey($setupRel)) {
        $actual = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
        $record.Hash = if ($actual -eq $expected[$setupRel]) { "OK" } else { "MISMATCH" }
    }

    try {
        Start-Process $setup -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/NOICONS" -Wait
        $record.Install = if (Test-Path $exe) { "OK" } else { "FAIL (exe missing)" }
    } catch {
        $record.Install = "FAIL ($($_.Exception.Message))"
    }

    if (Test-Path $exe) {
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $proc = Start-Process $exe -PassThru
        while ($sw.Elapsed.TotalSeconds -lt 20) {
            $proc.Refresh()
            if ($proc.HasExited -or $proc.MainWindowTitle -ne "") { break }
            Start-Sleep -Milliseconds 50
        }
        $record.Milliseconds = [int]$sw.Elapsed.TotalMilliseconds
        $record.Title = $proc.MainWindowTitle
        $record.Launch = if ($proc.HasExited) { "FAIL (exited)" } elseif ($proc.MainWindowTitle -ne "") { "OK" } else { "FAIL (no window)" }
        if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
        Start-Sleep -Seconds 1
    }

    if ($KeepInstalled) {
        $record.Uninstall = "SKIPPED"
    } elseif (Test-Path $uninstaller) {
        Start-Process $uninstaller -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART" -Wait
        $deadline = (Get-Date).AddSeconds(30)
        while ((Get-Date) -lt $deadline -and (Test-Path $installDir)) {
            Start-Sleep -Milliseconds 500
        }
        $record.Uninstall = if (Test-Path $installDir) { "FAIL (dir remains)" } else { "OK" }
    } else {
        $record.Uninstall = "FAIL (uninstaller missing)"
    }

    $results += [pscustomobject]$record
}

$results | Format-Table -AutoSize
$failed = $results | Where-Object { $_.Hash -eq "MISMATCH" -or $_.Install -like "FAIL*" -or $_.Launch -like "FAIL*" -or $_.Uninstall -like "FAIL*" }
if ($failed) {
    Write-Host "RESULT: FAIL ($($failed.Count) issue(s))" -ForegroundColor Red
    exit 1
}
Write-Host "RESULT: PASS" -ForegroundColor Green
exit 0
