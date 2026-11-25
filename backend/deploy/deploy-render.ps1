<#
Deploys the DocScan backend to Render's free plan WITHOUT a Git repository:
  1. builds the Docker image and pushes it to your PRIVATE Docker Hub repository
  2. creates (or reuses) on Render: free Postgres, free Key Value (Redis), and a free web service
     that runs the image (gunicorn + Celery worker in one container)
  3. on later runs: pushes the new image and triggers a redeploy

Before the first run:
  - Docker Hub: create a PRIVATE repository named docscan-backend (hub.docker.com -> Create repository),
    and an access token (Account settings -> Personal access tokens, Read & Write).
  - Render: create an API key (dashboard -> Account settings -> API keys).

Run from the backend folder:
  powershell -ExecutionPolicy Bypass -File deploy\deploy-render.ps1 -DockerUser <your-dockerhub-username>
Optional: -Region singapore|oregon|ohio|virginia|frankfurt   -ServiceName docscan-api
Secrets are asked for interactively and are never written to disk.
#>
param(
    [Parameter(Mandatory = $true)][string]$DockerUser,
    [string]$Region = "singapore",
    [string]$ServiceName = "docscan-api",
    [string]$DbName = "docscan-db",
    [string]$RedisName = "docscan-redis",
    [string]$ImageRepo = "docscan-backend"
)

$ErrorActionPreference = "Stop"
$Api = "https://api.render.com/v1"
$BackendDir = Split-Path -Parent $PSScriptRoot
$Image = "docker.io/$DockerUser/${ImageRepo}:latest"

function Read-Secret([string]$Prompt) {
    $secure = Read-Host -Prompt $Prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function Get-EnvFileValue([string]$Path, [string]$Key) {
    if (-not (Test-Path $Path)) { return $null }
    foreach ($line in Get-Content $Path) {
        if ($line -match "^\s*$Key\s*=\s*(.*)\s*$") { return $Matches[1].Trim('"', "'") }
    }
    return $null
}

function Invoke-Render([string]$Method, [string]$Path, $Body = $null) {
    $params = @{
        Method  = $Method
        Uri     = "$Api$Path"
        Headers = @{ Authorization = "Bearer $script:RenderKey"; Accept = "application/json" }
    }
    if ($null -ne $Body) {
        $params.ContentType = "application/json"
        $params.Body = ($Body | ConvertTo-Json -Depth 10)
    }
    try { return Invoke-RestMethod @params }
    catch {
        $detail = $_.ErrorDetails.Message
        throw "Render API $Method $Path failed: $($_.Exception.Message) $detail"
    }
}

function Find-ByName($Items, [string]$Property, [string]$Name) {
    foreach ($item in @($Items)) {
        $obj = $item.$Property
        if ($obj -and $obj.name -eq $Name) { return $obj }
    }
    return $null
}

function Wait-Available([string]$Path, [string]$Label) {
    Write-Host "    waiting for $Label to become available..." -NoNewline
    for ($i = 0; $i -lt 60; $i++) {
        $status = (Invoke-Render GET $Path).status
        if ($status -eq "available") { Write-Host " ok"; return }
        Write-Host "." -NoNewline
        Start-Sleep -Seconds 10
    }
    throw "$Label did not become available in 10 minutes (last status: $status)"
}

# ------------------------------------------------------------------ secrets
$script:RenderKey = Read-Secret "Render API key"
$DockerToken = Read-Secret "Docker Hub access token (for $DockerUser)"

# ------------------------------------------------------------------ 1. image
Write-Host "==> Logging in to Docker Hub"
$DockerToken | docker login --username $DockerUser --password-stdin
if ($LASTEXITCODE -ne 0) { throw "docker login failed" }

Write-Host "==> Building $Image (linux/amd64)"
docker build --platform linux/amd64 -t $Image $BackendDir
if ($LASTEXITCODE -ne 0) { throw "docker build failed" }

Write-Host "==> Pushing $Image"
docker push $Image
if ($LASTEXITCODE -ne 0) { throw "docker push failed (does the PRIVATE repo $DockerUser/$ImageRepo exist?)" }

# ------------------------------------------------------------------ 2. Render
Write-Host "==> Connecting to Render"
$owner = @(Invoke-Render GET "/owners?limit=20")[0].owner
if (-not $owner) { throw "No Render workspace found for this API key" }
$OwnerId = $owner.id
Write-Host "    workspace: $($owner.name) ($OwnerId)"

# Existing web service? Then just redeploy the new image.
$existing = Find-ByName (Invoke-Render GET "/services?name=$ServiceName&limit=20") "service" $ServiceName
if ($existing) {
    Write-Host "==> $ServiceName exists; deploying the new image"
    Invoke-Render POST "/services/$($existing.id)/deploys" @{ clearCache = "do_not_clear" } | Out-Null
    $url = $existing.serviceDetails.url
    Write-Host "`nDeploy started. Watch it at https://dashboard.render.com/web/$($existing.id)"
    Write-Host "API: $url/api   Health: $url/healthz/"
    exit 0
}

# Azure Document Intelligence settings: read from the local .env files, else ask.
$envFiles = @((Join-Path $BackendDir ".env"), (Join-Path (Split-Path -Parent $BackendDir) ".env"))
$DocEndpoint = $null; $DocKey = $null
foreach ($f in $envFiles) {
    if (-not $DocEndpoint) { $DocEndpoint = Get-EnvFileValue $f "DOCINTEL_ENDPOINT" }
    if (-not $DocKey) { $DocKey = Get-EnvFileValue $f "DOCINTEL_KEY" }
}
if (-not $DocEndpoint) { $DocEndpoint = Read-Host "DOCINTEL_ENDPOINT" }
if (-not $DocKey) { $DocKey = Read-Secret "DOCINTEL_KEY" }

Write-Host "==> Postgres ($DbName, free)"
$db = Find-ByName (Invoke-Render GET "/postgres?name=$DbName&limit=20") "postgres" $DbName
if (-not $db) {
    $db = Invoke-Render POST "/postgres" @{
        name = $DbName; ownerId = $OwnerId; plan = "free"; region = $Region
        version = "16"; databaseName = "docscan"; databaseUser = "docscan"
    }
}
Wait-Available "/postgres/$($db.id)" "Postgres"
$DatabaseUrl = (Invoke-Render GET "/postgres/$($db.id)/connection-info").internalConnectionString

Write-Host "==> Key Value / Redis ($RedisName, free)"
$kv = Find-ByName (Invoke-Render GET "/key-value?name=$RedisName&limit=20") "keyValue" $RedisName
if (-not $kv) {
    $kv = Invoke-Render POST "/key-value" @{
        name = $RedisName; ownerId = $OwnerId; plan = "free"; region = $Region
        maxmemoryPolicy = "noeviction"; ipAllowList = @()
    }
}
Wait-Available "/key-value/$($kv.id)" "Key Value"
$RedisUrl = (Invoke-Render GET "/key-value/$($kv.id)/connection-info").internalConnectionString

Write-Host "==> Registry credential for Docker Hub"
$cred = Invoke-Render POST "/registrycredentials" @{
    ownerId = $OwnerId; name = "dockerhub-$DockerUser"; registry = "DOCKER"
    username = $DockerUser; authToken = $DockerToken
}

$bytes = New-Object byte[] 48
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$secretKey = [Convert]::ToBase64String($bytes)
$envVars = [ordered]@{
    SECRET_KEY           = $secretKey
    DATABASE_URL         = $DatabaseUrl
    REDIS_URL            = $RedisUrl
    ALLOWED_HOSTS        = ".onrender.com,localhost"
    CSRF_TRUSTED_ORIGINS = "https://*.onrender.com"
    SECURE_SSL_REDIRECT  = "False"   # Render terminates HTTPS; keeps the health check from getting a 301
    SERVE_MEDIA          = "True"
    RUN_WORKER_IN_WEB    = "True"    # free plan: Celery worker runs inside the web container
    GUNICORN_WORKERS     = "2"       # sized for the 512 MB free instance
    CELERY_CONCURRENCY   = "1"
    OCR_ENGINE           = "azure"
    DOCINTEL_TIER        = "F0"
    DOCINTEL_ENDPOINT    = $DocEndpoint
    DOCINTEL_KEY         = $DocKey
}

Write-Host "==> Web service ($ServiceName, free)"
$created = Invoke-Render POST "/services" @{
    type    = "web_service"
    name    = $ServiceName
    ownerId = $OwnerId
    image   = @{ ownerId = $OwnerId; imagePath = $Image; registryCredentialId = $cred.id }
    envVars = @($envVars.GetEnumerator() | ForEach-Object { @{ key = $_.Key; value = $_.Value } })
    serviceDetails = @{
        runtime         = "image"
        plan            = "free"
        region          = $Region
        healthCheckPath = "/healthz/"
    }
}
$svc = if ($created.service) { $created.service } else { $created }
$url = $svc.serviceDetails.url

Write-Host ""
Write-Host "Done. First deploy takes a few minutes: https://dashboard.render.com/web/$($svc.id)"
Write-Host "API:     $url/api"
Write-Host "Swagger: $url/api/docs/"
Write-Host "Health:  $url/healthz/"
Write-Host "Mobile:  set EXPO_PUBLIC_API_URL=$url/api"
