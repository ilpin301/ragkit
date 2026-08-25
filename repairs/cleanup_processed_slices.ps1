# Repair: delete slice PDFs in IN\ whose ENTIRE slice family is marked processed in doc_status.
# Idempotent: already-deleted files are skipped. Never touches un-sliced source PDFs.
# Default is a dry run. Pass -Apply to actually delete.
param([Parameter(Mandatory=$true)][string]$Root, [switch]$Apply)

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path -LiteralPath $Root).Path
$IN = Join-Path $Root 'IN'
$status = Get-Content (Join-Path $Root 'lightrag\data\rag_storage\kv_store_doc_status.json') -Raw | ConvertFrom-Json
$processed = @{}
foreach ($p in $status.PSObject.Properties) {
  if ($p.Value.status -eq 'processed' -and $p.Value.file_path) { $processed[$p.Value.file_path] = $true }
}

$slices = Get-ChildItem "$IN\*.pdf" | Where-Object { $_.Name -match '-\d{2,3}-\d{2,3}\.pdf$' }
$families = $slices | Group-Object { $_.Name -replace '-\d{2,3}-\d{2,3}\.pdf$','' }

$total = 0
foreach ($f in $families) {
  $unprocessed = @($f.Group | Where-Object { -not $processed.ContainsKey($_.Name) })
  if ($unprocessed.Count -gt 0) {
    "SKIP family $($f.Name): $($unprocessed.Count)/$($f.Count) slice(s) NOT processed -> $($unprocessed.Name -join ', ')"
    continue
  }
  foreach ($s in $f.Group) {
    $total++
    if ($Apply) { Remove-Item $s.FullName -Force; "DELETED $($s.Name)" } else { "DRY RUN would DELETE $($s.Name)" }
  }
}
"`n$total slice file(s) in $($families.Count) family(ies) considered."
if (-not $Apply) { "(dry run; re-run with -Apply to delete)" }
