## Keep the machine awake (mandatory)

Any long-running RAG shell process — ingest, parse, insert, delete, enrich, audit — must run with sleep blocked, or the box suspends mid-run and the job dies.

Start this BEFORE launching the process (`keepawake.ps1` is a per-base script, in the base's `lightrag\`):

```powershell
$ka = Start-Process pwsh -ArgumentList '-NoProfile','-File',(Join-Path $LrDir 'keepawake.ps1') -PassThru -WindowStyle Hidden
```

Verify it registered (needs an elevated shell to read):

```powershell
powercfg /requests | Select-String -Pattern 'SYSTEM:' -Context 0,2
```

Expect `SYSTEM: [PROCESS] ...pwsh.exe`. If it says `None.`, the block is NOT active — do not start the run.

`keepawake.ps1` holds `SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED|ES_AWAYMODE_REQUIRED)` for as long as it lives, so killing the process releases the block automatically — there is no persistent power setting to restore. Display sleep is deliberately still allowed; only system sleep is blocked.

Stop it as part of the cleanup below, once no RAG process is still running:

```powershell
Stop-Process -Id $ka.Id -Force
```

Gotcha: PowerShell parses `0x80000000` as a signed Int32, so building the flags inline throws on the P/Invoke and the keepawake silently does nothing while still looking alive. Use the script, which casts to `[uint32]`.

## Cleanup after success (mandatory)

When the run finishes SUCCESSFULLY — `EXITCODE=0` and the verification steps passed — do both of these before reporting done:

1. **Kill every shell process started for this run.** Background waiters, `tail -f` tails, monitors, poll loops, and the `keepawake.ps1` process — the user's and yours. Use `TaskStop` on each background task id. Leave nothing running.
2. **Delete the logs the run produced.** `lightrag\LOG\ingest_run.log`, `LOG\*.log` for this run, and any scratchpad task-output files.

Order matters: kill the tails BEFORE deleting the logs, or a live `tail -f` holds the handle.

NEVER do either of these before success. While a run is in flight the log is the only evidence of progress, and on a FAILED or killed run both the logs and the shells must be KEPT for diagnosis.
