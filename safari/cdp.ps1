# Sends CDP commands to an iPhone Safari tab through the tunnel (127.0.0.1:9223), for when the `iphone` MCP isn't loaded.
# Picks the first tab whose URL matches -Match. -Steps is a JSON array of {"method", "params", "save", "sleep"}:
#   "save": file name (no extension) for a Page.captureScreenshot result, written to -OutDir
#   "sleep": milliseconds to wait before the step (e.g. after Page.reload)
# Example:
#   .\cdp.ps1 -Match '*ca-tims*' -OutDir $env:TEMP -Steps '[{"method":"Runtime.evaluate","params":{"expression":"(() => innerWidth)()","returnByValue":true}},{"method":"Page.captureScreenshot","params":{"format":"png"},"save":"shot"}]'
# Wrap evaluated scripts in (() => { ... })(): top-level const survives between calls.
param([string]$Port = '9223', [string]$Match = '*ca-tims*', [string]$Steps, [string]$OutDir)
$t = (Invoke-RestMethod "http://127.0.0.1:$Port/json/list" -TimeoutSec 10) | Where-Object url -like $Match | Select-Object -First 1
if (-not $t) { 'no matching tab'; exit 1 }
"tab: $($t.url)"
$ws = [System.Net.WebSockets.ClientWebSocket]::new(); $ct = [Threading.CancellationToken]::None
if (-not $ws.ConnectAsync([Uri]$t.webSocketDebuggerUrl, $ct).Wait(15000)) { 'connect timeout'; exit 1 }
$buf = [byte[]]::new(4MB); $id = 0
function Invoke-Cdp($method, $params) {
  $script:id++
  $msg = @{ id = $script:id; method = $method; params = $(if ($params) { $params } else { @{} }) } | ConvertTo-Json -Compress -Depth 8
  $ws.SendAsync([ArraySegment[byte]][Text.Encoding]::UTF8.GetBytes($msg), 'Text', $true, $ct).Wait() | Out-Null
  $sb = [Text.StringBuilder]::new(); $deadline = (Get-Date).AddSeconds(45)
  while ((Get-Date) -lt $deadline -and $ws.State -eq 'Open') {
    $r = $ws.ReceiveAsync([ArraySegment[byte]]$buf, $ct)
    if (-not $r.Wait(45000)) { return @{ error = 'receive timeout' } }
    [void]$sb.Append([Text.Encoding]::UTF8.GetString($buf, 0, $r.Result.Count))
    if ($r.Result.EndOfMessage) {
      $s = $sb.ToString(); [void]$sb.Clear()
      $o = $s | ConvertFrom-Json
      if ($o.id -eq $script:id) { return $o }
    }
  }
  @{ error = 'timeout' }
}
foreach ($st in ($Steps | ConvertFrom-Json)) {
  if ($st.sleep) { Start-Sleep -Milliseconds $st.sleep }
  $p = @{}; if ($st.params) { $st.params.PSObject.Properties | ForEach-Object { $p[$_.Name] = $_.Value } }
  $sw = [Diagnostics.Stopwatch]::StartNew(); $o = Invoke-Cdp $st.method $p; $ms = $sw.ElapsedMilliseconds
  if ($st.save -and $o.result.data) {
    $f = Join-Path $OutDir "$($st.save).png"; [IO.File]::WriteAllBytes($f, [Convert]::FromBase64String($o.result.data))
    "== $($st.method) ($ms ms) -> $f"
  } else {
    $txt = $o | ConvertTo-Json -Compress -Depth 8
    "== $($st.method) ($ms ms) " + $txt.Substring(0, [Math]::Min(600, $txt.Length))
  }
}
$ws.Dispose()
