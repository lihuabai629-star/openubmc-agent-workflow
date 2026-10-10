[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$OutputDirectory,
  [string]$Archive = "",
  [switch]$ExportToGitHubActions
)

$ErrorActionPreference = "Stop"
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
  throw "This toolchain is for native Windows x64 only"
}
$pin = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot "windows-python.lock.json") | ConvertFrom-Json
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $OutputDirectory) {
  throw "Output directory already exists; refusing to replace a toolchain"
}
if ($ExportToGitHubActions -and (-not $env:GITHUB_PATH -or -not $env:GITHUB_ENV)) {
  throw "GitHub Actions environment files are required"
}
New-Item -ItemType Directory -Path $OutputDirectory | Out-Null
if ($Archive) {
  $Archive = (Resolve-Path -LiteralPath $Archive).Path
} else {
  $Archive = Join-Path $OutputDirectory "python.tar.gz"
  Invoke-WebRequest -Uri $pin.url -OutFile $Archive
}
if ((Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $pin.sha256) {
  throw "Windows Python archive SHA-256 mismatch; nothing extracted or executed"
}
$runtime = Join-Path $OutputDirectory "runtime"
New-Item -ItemType Directory -Path $runtime | Out-Null
& tar -xf $Archive -C $runtime
if ($LASTEXITCODE -ne 0) { throw "Windows Python archive extraction failed" }
$python = Join-Path $runtime "python/python.exe"
$probe = 'import json,struct,sys; assert sys.implementation.name == "cpython" and sys.platform == "win32" and struct.calcsize("P") == 8 and sys.version_info[:3] == tuple(map(int,sys.argv[1].split("."))) and not hasattr(sys,"gettotalrefcount"); print(json.dumps({"version":sys.version.split()[0],"platform":sys.platform,"executable":sys.executable}))'
$identity = & $python -I -B -c $probe $pin.version
if ($LASTEXITCODE -ne 0) { throw "Windows Python interpreter identity mismatch" }
$identity = $identity | ConvertFrom-Json
if ($ExportToGitHubActions) {
  $utf8 = New-Object System.Text.UTF8Encoding($false)
  [IO.File]::AppendAllText($env:GITHUB_PATH, (Split-Path $python) + "`n", $utf8)
  [IO.File]::AppendAllText($env:GITHUB_ENV, "OPENUBMC_PLUGIN_WINDOWS_PYTHON=$python`n", $utf8)
}
@{
  version = $identity.version
  platform = $identity.platform
  python = $identity.executable
  archive_sha256 = $pin.sha256
  supplier_release = $pin.release
} | ConvertTo-Json -Compress
