$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$binary = Join-Path $repoRoot 'tools\cloudflared.exe'
$tokenFile = Join-Path $repoRoot 'cloudflare-tunnel.token'

if (-not (Test-Path -LiteralPath $binary)) {
    throw 'cloudflared is missing. Run .\scripts\install-cloudflared.ps1 first.'
}
if (-not (Test-Path -LiteralPath $tokenFile) -or (Get-Item -LiteralPath $tokenFile).Length -eq 0) {
    throw 'Save your Cloudflare Tunnel token as cloudflare-tunnel.token in the project root.'
}

& $binary tunnel run --token-file $tokenFile
exit $LASTEXITCODE
