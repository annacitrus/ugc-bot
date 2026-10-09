$ErrorActionPreference = 'Stop'

# Official Cloudflare release. Update both the version and published SHA-256 together.
$version = '2026.10.0'
$expectedSha256 = '86aee4017b26625cee8484c113558f48effa4cd47f7aa05fcf425604e5d2b23c'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$toolsDir = Join-Path $repoRoot 'tools'
$binary = Join-Path $toolsDir 'cloudflared.exe'
$download = Join-Path $toolsDir 'cloudflared.exe.download'
$url = "https://github.com/cloudflare/cloudflared/releases/download/$version/cloudflared-windows-amd64.exe"

New-Item -ItemType Directory -Path $toolsDir -Force | Out-Null
if (Test-Path -LiteralPath $binary) {
    $existingHash = (Get-FileHash -LiteralPath $binary -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($existingHash -eq $expectedSha256) {
        & $binary --version
        exit $LASTEXITCODE
    }
    throw "An existing cloudflared.exe has a different checksum. Remove or rename it before installing $version."
}

try {
    Invoke-WebRequest -Uri $url -OutFile $download
    $actualSha256 = (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualSha256 -ne $expectedSha256) {
        throw "Cloudflare download checksum mismatch. Expected $expectedSha256, received $actualSha256."
    }
    Move-Item -LiteralPath $download -Destination $binary
    & $binary --version
    exit $LASTEXITCODE
}
finally {
    if (Test-Path -LiteralPath $download) {
        Remove-Item -LiteralPath $download -Force
    }
}
