$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $repoRoot '.env'
if (-not (Test-Path -LiteralPath $envFile)) {
    throw '.env is missing.'
}

$settings = @{}
foreach ($line in Get-Content -LiteralPath $envFile) {
    if ($line -match '^\s*(TIKTOK_REDIRECT_URI|HTTP_HOST|HTTP_PORT)\s*=\s*(.*?)\s*$') {
        $settings[$Matches[1]] = $Matches[2].Trim('"', "'")
    }
}
$redirect = $settings['TIKTOK_REDIRECT_URI']
if (-not $redirect -or $redirect.Contains('your-domain.example')) {
    throw 'Set TIKTOK_REDIRECT_URI in .env to your real Cloudflare hostname first.'
}
$publicUri = [Uri]$redirect
if ($publicUri.Scheme -ne 'https' -or $publicUri.AbsolutePath -ne '/oauth/callback') {
    throw 'TIKTOK_REDIRECT_URI must be https://YOUR-HOSTNAME/oauth/callback.'
}
$localHost = if ($settings['HTTP_HOST']) { $settings['HTTP_HOST'] } else { '127.0.0.1' }
$localPort = if ($settings['HTTP_PORT']) { $settings['HTTP_PORT'] } else { '8080' }
$localHealth = "http://${localHost}:${localPort}/healthz"
$publicHealth = "https://$($publicUri.Host)/healthz"

foreach ($url in @($localHealth, $publicHealth)) {
    try {
        $response = Invoke-WebRequest -Uri $url -TimeoutSec 15 -UseBasicParsing
        if ($response.StatusCode -ne 200 -or $response.Content.Trim() -ne 'ok') {
            throw "Unexpected response: HTTP $($response.StatusCode)"
        }
        Write-Host "OK $url"
    }
    catch {
        throw "Health check failed for $url. Start the bot and tunnel, then verify the Cloudflare route. $($_.Exception.Message)"
    }
}
Write-Host "TikTok callback URI: $redirect"
