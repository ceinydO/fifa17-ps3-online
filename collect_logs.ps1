# Pakuje logi z ostatnich 3 godzin do jednego zip-a do wyslania. Usage: .\collect_logs.ps1 [-Rpcs3Dir "C:\sciezka\do\rpcs3"]
# W zip-ie: logi sesji serwera (logs\session_*.log), dziennik GameManager (logs\gm_attempts.log), przechwyty polaczen
# (logs\captures), stan drabinki (state\gm_variant.json), config.json oraz RPCS3.log i TTY.log tego komputera
# (folder RPCS3: parametr -Rpcs3Dir, zmienna RPCS3_DIR, uruchomiony rpcs3.exe albo wyszukiwanie na dyskach/pulpicie).
# Zamknij gre w RPCS3 przed uruchomieniem, zeby log byl kompletny. RPCS3.log kolegi dolacz osobno.
param([string]$Rpcs3Dir = $env:RPCS3_DIR)
Set-Location $PSScriptRoot

function Find-Rpcs3Dir {
    $p = Get-Process rpcs3 -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($p -and $p.Path) { return (Split-Path $p.Path) }
    $roots = @()
    foreach ($d in (Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue)) { $roots += $d.Root }
    $roots += [Environment]::GetFolderPath("Desktop")
    foreach ($r in $roots) {
        if (Test-Path (Join-Path $r "rpcs3.exe")) { return $r }
        foreach ($sub in (Get-ChildItem $r -Directory -ErrorAction SilentlyContinue)) {
            if (Test-Path (Join-Path $sub.FullName "rpcs3.exe")) { return $sub.FullName }
        }
    }
    return $null
}
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
if (-not $Rpcs3Dir) { $Rpcs3Dir = Find-Rpcs3Dir }
if ($Rpcs3Dir -and (Test-Path (Join-Path $Rpcs3Dir "RPCS3.log"))) {
    New-Item -ItemType Directory -Path (Join-Path $stage "rpcs3") -Force | Out-Null
    foreach ($f in @("RPCS3.log", "TTY.log")) {
        $src = Join-Path $Rpcs3Dir $f
        if (Test-Path $src) { Copy-Item $src (Join-Path $stage "rpcs3") }
    }
    Write-Host "Dolaczono RPCS3.log z: $Rpcs3Dir"
} else {
    Write-Host "UWAGA: nie znaleziono RPCS3.log -- podaj folder: .\collect_logs.ps1 -Rpcs3Dir ""C:\sciezka\do\rpcs3"""
}
$zip = Join-Path $PSScriptRoot ("fifa17_logs_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".zip")
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip -Force
Remove-Item $stage -Recurse -Force
Write-Host "Gotowe: $zip"
Write-Host "Wyslij ten plik oraz RPCS3.log kolegi."
