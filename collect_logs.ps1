# Pakuje logi z ostatnich 3 godzin do jednego zip-a do wyslania. Usage: .\collect_logs.ps1
# W zip-ie: logi sesji serwera (logs\session_*.log), dziennik GameManager (logs\gm_attempts.log), przechwyty polaczen
# (logs\captures), stan drabinki (state\gm_variant.json) i config.json. RPCS3.log (host i kolega) dolacz osobno.
Set-Location $PSScriptRoot
$since = (Get-Date).AddHours(-3)
$stage = Join-Path $env:TEMP ("fifa17_logs_" + (Get-Date -Format "yyyyMMdd_HHmmss"))
New-Item -ItemType Directory -Path $stage | Out-Null
if (Test-Path logs) {
    Get-ChildItem logs -Recurse -File | Where-Object { $_.LastWriteTime -gt $since } | ForEach-Object {
        $rel = $_.FullName.Substring((Resolve-Path logs).Path.Length).TrimStart('\')
        $dest = Join-Path $stage (Join-Path "logs" $rel)
        New-Item -ItemType Directory -Path (Split-Path $dest) -Force | Out-Null
        Copy-Item $_.FullName $dest
    }
}
foreach ($f in @("state\gm_variant.json", "config.json")) {
    if (Test-Path $f) {
        $dest = Join-Path $stage $f
        New-Item -ItemType Directory -Path (Split-Path $dest) -Force | Out-Null
        Copy-Item $f $dest
    }
}
$zip = Join-Path $PSScriptRoot ("fifa17_logs_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".zip")
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip -Force
Remove-Item $stage -Recurse -Force
Write-Host "Gotowe: $zip"
Write-Host "Wyslij ten plik oraz RPCS3.log hosta i kolegi."
