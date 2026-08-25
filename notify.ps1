<#
Play-RagSound -Success | -Failure

Fired from ingest.ps1 only, once, on the single $ec decision. Never mid-run:
error scanning during a run trips on the benign "3/4 fields on ENTITY" warnings
(~0.12% of records) and on transient retries.

A sound failure is swallowed. A missing sound card, an RDP session or a locked
desktop must never turn a good ingest into a reported failure.
#>
function Play-RagSound {
  param([switch]$Success, [switch]$Failure)
  $wav = if ($Success) { "$env:WINDIR\Media\Ring10.wav" } else { "$env:WINDIR\Media\Windows Critical Stop.wav" }
  try {
    if (Test-Path -LiteralPath $wav) {
      (New-Object Media.SoundPlayer $wav).PlaySync()
    } else {
      if ($Success) { [console]::beep(1200, 200) }
      else { 1..3 | ForEach-Object { [console]::beep(880, 300); Start-Sleep -Milliseconds 120 } }
    }
  } catch {
    Write-Host "notify: sound suppressed ($($_.Exception.Message))"
  }
}
