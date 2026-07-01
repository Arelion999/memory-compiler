[CmdletBinding()]
param(
    [string]$ArchiveDir = "C:\Backups\memory-compiler\archives",
    [int]$MinFiles = 50
)

$ErrorActionPreference = "Stop"

$latest = Get-ChildItem -Path $ArchiveDir -Filter "knowledge-*.tar.gz" -File |
    Sort-Object Name -Descending | Select-Object -First 1

if (-not $latest) {
    Write-Host "FAIL: no archives in $ArchiveDir"
    exit 1
}

$tmp = Join-Path $env:TEMP ("mc-verify-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

$fail = @()
$mdCount = 0
try {
    tar -xzf $latest.FullName -C $tmp 2>$null
    if ($LASTEXITCODE -ne 0) { $fail += "tar extract failed" }

    $kb = Join-Path $tmp "knowledge"
    if (-not (Test-Path $kb)) {
        $fail += "no knowledge/ dir"
    } else {
        if (-not (Test-Path (Join-Path $kb ".git"))) { $fail += "no knowledge/.git" }
        $mdCount = (Get-ChildItem -Path $kb -Filter "*.md" -Recurse -File).Count
        if ($mdCount -lt $MinFiles) { $fail += "md files $mdCount < $MinFiles" }
    }
}
finally {
    Remove-Item -Path $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

if ($fail.Count -gt 0) {
    Write-Host ("FAIL [{0}]: {1}" -f $latest.Name, ($fail -join "; "))
    exit 1
}
Write-Host ("PASS [{0}]: md={1}" -f $latest.Name, $mdCount)
exit 0
