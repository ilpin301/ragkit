# Blocks system sleep while THIS process lives. Kill it to release.
# Display is deliberately allowed to sleep (ES_DISPLAY_REQUIRED omitted).
Add-Type -Name P -Namespace W -MemberDefinition '[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint f);'
# ES_CONTINUOUS(0x80000000) | ES_SYSTEM_REQUIRED(0x1) | ES_AWAYMODE_REQUIRED(0x40).
# Must be built as [uint32]: PowerShell parses 0x80000000 as a signed Int32 and the P/Invoke throws.
$flags = [uint32](2147483648 + 1 + 64)
$r = [W.P]::SetThreadExecutionState($flags)
if ($r -eq 0) { Write-Error "SetThreadExecutionState failed"; exit 1 }
Write-Output "KEEPAWAKE_ON pid=$PID"
while ($true) { Start-Sleep -Seconds 60 }
