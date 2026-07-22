# Build the Windows release: a windowed .exe plus a bundled copy of wit,
# zipped into one archive for the GitHub Releases tab.
#
# Run from the repository root:
#   pwsh packaging/build_release.ps1
#
# Requires: py -3.13, PyInstaller (pip install pyinstaller), and wit already
# unpacked under tools/ (the same copy the app auto-detects during development).

$ErrorActionPreference = "Stop"
$root = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location $root

$version = (Select-String -Path "riivultimatum\__init__.py" -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
$name = "RiivolutionUltimatum"
$stage = Join-Path $root "release\$name"
Write-Host "Building $name v$version"

# 1. Locate the wit copy to bundle.
$witSrc = Get-ChildItem "tools" -Directory -Filter "wit-*" -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending | Select-Object -First 1
if (-not $witSrc) {
    throw "No wit found under tools/. Download it from https://wit.wiimm.de/ and unpack it there."
}

# 2. Compile the exe.
py -3.13 -m PyInstaller "packaging\RiivolutionUltimatum.spec" --noconfirm --distpath "build\dist" --workpath "build\work" | Out-Host
$exe = "build\dist\$name.exe"
if (-not (Test-Path $exe)) { throw "PyInstaller did not produce $exe" }

# 3. Assemble the release folder.
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force $stage | Out-Null
Copy-Item $exe $stage
Copy-Item "LICENSE" $stage
Copy-Item "NOTICE" $stage
Copy-Item "packaging\RELEASE-README.txt" (Join-Path $stage "README.txt")
# wit, unmodified, license and source-pointer files included.
Copy-Item $witSrc.FullName (Join-Path $stage "wit") -Recurse

# 4. Zip it.
$zip = Join-Path $root "release\$name-v$version-win64.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path "$stage\*" -DestinationPath $zip
Write-Host ""
Write-Host "Done:"
Write-Host "  folder: $stage"
Write-Host "  zip:    $zip  ($([math]::Round((Get-Item $zip).Length/1MB,1)) MB)"
