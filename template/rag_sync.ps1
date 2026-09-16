# rag_sync.ps1 - move the LightRAG DB snapshot between machines via a Google Drive folder.
# Lives in the repo root, next to the lightrag\ folder.
#   .\rag_sync.ps1 push     # this machine's DB -> Drive
#   .\rag_sync.ps1 pull     # Drive -> this machine's DB (full overwrite, not a merge)
# Drive root: -DriveDir, else $env:__NAME___DRIVE, else "J:\My Drive\RAG" (the ilpin301 account).
# Snapshot lives in <root>\<project>\rag_storage.tgz - e.g. "...\RAG\__NAME__\rag_storage.tgz".
# The project subfolder is created by the first push.
# The LightRAG container is stopped for the duration and restarted only if it was running.
param(
    [Parameter(Mandatory, Position = 0)][ValidateSet('push', 'pull')][string]$Action,
    [string]$DriveDir = $(if ($env:__NAME___DRIVE) { $env:__NAME___DRIVE } else { 'J:\My Drive\RAG' }),
    [switch]$KeepRunning,
    [switch]$Force
)
$ErrorActionPreference = 'Stop'

$storage = Join-Path $PSScriptRoot 'lightrag\data\rag_storage'
$dataDir = Split-Path $storage
$project = Split-Path $PSScriptRoot -Leaf
$destDir = [System.IO.Path]::Combine($DriveDir, $project)
$archive = [System.IO.Path]::Combine($destDir, 'rag_storage.tgz')

# --- vector backend ----------------------------------------------------------
# A nano base is entirely inside rag_storage. A Qdrant base is not: its vectors
# live in a docker volume, so the snapshot has to be pulled out over the API and
# carried in the same tarball, or a restore silently comes back with no vectors.
$snapDir   = Join-Path $dataDir 'qdrant_snapshots'
$qdrantUrl = 'http://127.0.0.1:6333'
$useQdrant = $false
$syncEnv   = Join-Path $PSScriptRoot 'lightrag\.env'
if (Test-Path $syncEnv) {
    $m = Select-String -Path $syncEnv -Pattern '^\s*LIGHTRAG_VECTOR_STORAGE\s*=\s*(\S+)' | Select-Object -First 1
    if ($m -and $m.Matches[0].Groups[1].Value -eq 'QdrantVectorDBStorage') { $useQdrant = $true }
    $u = Select-String -Path $syncEnv -Pattern '^\s*QDRANT_URL\s*=\s*(\S+)' | Select-Object -First 1
    if ($u) { $qdrantUrl = $u.Matches[0].Groups[1].Value }
}
$cpnMatch = Select-String -Path $syncEnv -Pattern '^\s*COMPOSE_PROJECT_NAME\s*=\s*(\S+)' -ErrorAction SilentlyContinue | Select-Object -First 1
$qdrantContainer = if ($cpnMatch) { "$($cpnMatch.Matches[0].Groups[1].Value.ToLower())-qdrant-1" } else { "$($project.ToLower())-qdrant-1" }

function Get-QdrantCollections {
    $r = curl.exe -s -m 60 --noproxy '*' "$qdrantUrl/collections" | ConvertFrom-Json
    if (-not $r.result) { throw "qdrant at $qdrantUrl did not list its collections" }
    $names = @($r.result.collections | ForEach-Object { $_.name })
    if (-not $names) { throw "qdrant at $qdrantUrl holds no collections - nothing to back up" }
    return $names
}

if (-not [System.IO.Directory]::Exists($DriveDir)) {
    throw "Drive folder not found: $DriveDir`nStart Google Drive for Desktop, or pass -DriveDir '<path>'."
}

# A running server writes vdb_*.json continuously; a snapshot taken mid-write is a corrupt graph.
$container = $null
$wasRunning = $false
if (-not $KeepRunning) {
    $port = 9621
    $envFile = Join-Path $PSScriptRoot 'lightrag\.env'
    if (Test-Path $envFile) {
        $m = Select-String -Path $envFile -Pattern '^\s*PORT\s*=\s*(\d+)' | Select-Object -First 1
        if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
    }

    # Stopping the container mid-ingest kills that ingest and leaves the graph half-written.
    # An unreachable server just means nothing is running - that is fine, carry on.
    $irm = @{ Uri = "http://localhost:$port/health"; TimeoutSec = 10 }
    if ($PSVersionTable.PSVersion.Major -ge 6) { $irm.NoProxy = $true }
    $health = $null
    try { $health = Invoke-RestMethod @irm } catch { }
    if ($health -and ($health.pipeline_busy -or $health.pipeline_active -or $health.pipeline_scanning)) {
        if (-not $Force) {
            throw "LightRAG is mid-ingest (busy=$($health.pipeline_busy) active=$($health.pipeline_active) scanning=$($health.pipeline_scanning)).`nWait for it to finish, or pass -Force to stop it anyway."
        }
        "WARNING: pipeline busy, -Force given - the running ingest will be killed."
    }

    # The /health flags only cover server-side ingests. ingest_detached.ps1 runs python
    # outside the container and writes rag_storage directly, so a snapshot taken during
    # one is truncated. Detect it by command line - a PID file would go stale on a crash.
    $ingest = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*rag_ingest.py*' })
    if ($ingest) {
        if (-not $Force) {
            throw "A detached ingest is running (PID $($ingest.ProcessId -join ', ')).`nWait for it to finish, or pass -Force to snapshot anyway."
        }
        "WARNING: detached ingest running, -Force given - the snapshot may be inconsistent."
    }

    if (Get-Command docker -ErrorAction SilentlyContinue) {
        # A bare name=lightrag filter matches every base's container and would stop an
        # unrelated project's server. Pin to this base's own COMPOSE_PROJECT_NAME.
        $cpn = Select-String -Path (Join-Path $PSScriptRoot 'lightrag\.env') -Pattern '^\s*COMPOSE_PROJECT_NAME\s*=\s*(\S+)' -ErrorAction SilentlyContinue | Select-Object -First 1
        $container = if ($cpn) { "$($cpn.Matches[0].Groups[1].Value.ToLower())-lightrag-1" } else { "$($project.ToLower())-lightrag-1" }
        $wasRunning = [bool](@(docker ps --filter "name=^$container$" --format '{{.Names}}')[0])
        if ($wasRunning) {
            "stopping $container ..."
            docker stop $container | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "docker stop $container failed (exit $LASTEXITCODE)" }
        }
    }
    elseif (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        # No docker CLI - refuse to touch the DB while anything still holds the server port.
        throw "docker CLI not found and port $port is still in use. Stop LightRAG manually, or pass -KeepRunning."
    }
}

try {
    if ($Action -eq 'push') {
        if (-not (Test-Path $storage)) { throw "No local DB at $storage" }
        [void][System.IO.Directory]::CreateDirectory($destDir)

        # A Qdrant base keeps its vectors in a docker volume, NOT under
        # rag_storage, so tarring rag_storage alone would back up a store with
        # no vectors in it - and you would only find out during a restore.
        # Snapshots are taken through Qdrant's own API: tarring live segment
        # files is exactly the inconsistency the snapshot endpoint exists for.
        if ($useQdrant) {
            if (-not (docker ps --filter "name=^$qdrantContainer$" --format '{{.Names}}')) {
                "starting $qdrantContainer for the snapshot ..."
                docker start $qdrantContainer | Out-Null
                if ($LASTEXITCODE -ne 0) { throw "docker start $qdrantContainer failed" }
            }
            foreach ($i in 1..60) {
                $h = curl.exe -s -m 5 --noproxy '*' "$qdrantUrl/healthz" 2>$null
                if ($h -match 'passed') { break }
                if ($i -eq 60) { throw "qdrant at $qdrantUrl never became healthy" }
                Start-Sleep -Seconds 2
            }
            if (Test-Path $snapDir) { Remove-Item $snapDir -Recurse -Force }
            [void][System.IO.Directory]::CreateDirectory($snapDir)
            foreach ($coll in (Get-QdrantCollections)) {
                $snap = (curl.exe -s -m 600 --noproxy '*' -X POST "$qdrantUrl/collections/$coll/snapshots" | ConvertFrom-Json).result.name
                if (-not $snap) { throw "qdrant refused to snapshot $coll" }
                $out = Join-Path $snapDir "$coll.snapshot"
                curl.exe -s -m 3600 --noproxy '*' -o $out "$qdrantUrl/collections/$coll/snapshots/$snap"
                if ($LASTEXITCODE -ne 0 -or -not (Test-Path $out)) { throw "downloading snapshot of $coll failed" }
                # drop the server-side copy or the volume grows by a full store every push
                curl.exe -s -m 60 --noproxy '*' -X DELETE "$qdrantUrl/collections/$coll/snapshots/$snap" | Out-Null
                "  snapshot $coll -> {0:N0} MB" -f ((Get-Item $out).Length / 1MB)
            }
        }
        # Build outside the Drive folder. Renaming a temp file over the archive inside Drive
        # is a "swap", and on a streaming mount Drive refuses to commit one until it has
        # pulled the whole old version back down first (SWAPPED_ITEM_NOT_FULLY_DOWNLOADED in
        # drive_fs.txt) - a pointless download of the copy we are about to discard.
        $tmp = Join-Path ([System.IO.Path]::GetTempPath()) "rag_storage.$PID.tgz"
        try {
            $tarArgs = @('-czf', $tmp, '-C', $dataDir, 'rag_storage')
            if ($useQdrant) { $tarArgs += 'qdrant_snapshots' }
            tar @tarArgs
            if ($LASTEXITCODE -ne 0) { throw "tar failed (exit $LASTEXITCODE)" }
            # Overwrite in place. Copy-Item opens the destination with CREATE_ALWAYS, which
            # Drive records as a new revision. Deleting first does not work on a streaming
            # mount: the delete is only queued for the cloud and Drive immediately restores
            # the placeholder, so the follow-up move fails with "The file exists".
            Copy-Item $tmp $archive -Force
        }
        finally {
            if (Test-Path $tmp) { Remove-Item $tmp -Force }
        }
        "pushed {0:N0} MB -> {1}" -f ((Get-Item $archive).Length / 1MB), $archive
        'Wait for the Drive tray icon to finish uploading before pulling on the other machine.'
    }
    else {
        if (-not [System.IO.File]::Exists($archive)) {
            throw "No snapshot at $archive`nPush from the other machine first."
        }
        $bak = "$storage.bak"
        if (Test-Path $bak) { Remove-Item $bak -Recurse -Force }
        if (Test-Path $storage) { Move-Item $storage $bak }
        try {
            tar -xzf $archive -C $dataDir
            if ($LASTEXITCODE -ne 0) { throw "tar failed (exit $LASTEXITCODE)" }
        }
        catch {
            if (Test-Path $storage) { Remove-Item $storage -Recurse -Force }
            if (Test-Path $bak) { Move-Item $bak $storage }
            throw
        }
        if ($useQdrant) {
            if (-not (Test-Path $snapDir)) { throw "archive has no qdrant_snapshots/ but this base uses Qdrant - refusing to start with an empty vector index" }
            if (-not (docker ps --filter "name=^$qdrantContainer$" --format '{{.Names}}')) {
                docker start $qdrantContainer | Out-Null
            }
            foreach ($i in 1..60) {
                $h = curl.exe -s -m 5 --noproxy '*' "$qdrantUrl/healthz" 2>$null
                if ($h -match 'passed') { break }
                if ($i -eq 60) { throw "qdrant at $qdrantUrl never became healthy" }
                Start-Sleep -Seconds 2
            }
            foreach ($f in (Get-ChildItem $snapDir -Filter '*.snapshot')) {
                $coll = $f.BaseName
                # priority=snapshot: the snapshot's data wins over whatever is
                # currently in the collection, which is the point of a restore.
                $r = curl.exe -s -m 3600 --noproxy '*' -X POST -H 'Content-Type:multipart/form-data' `
                     -F "snapshot=@$($f.FullName)" `
                     "$qdrantUrl/collections/$coll/snapshots/upload?priority=snapshot"
                if ($LASTEXITCODE -ne 0 -or $r -notmatch '"status"\s*:\s*"ok"') { throw "restoring snapshot for $coll failed: $r" }
                "  restored $coll"
            }
        }
        "pulled {0} -> {1}" -f $archive, $storage
        "previous DB kept at $bak - delete it once the RAG answers correctly"
    }
}
finally {
    if ($wasRunning) {
        "starting $container ..."
        docker start $container | Out-Null
    }
}
