# Sends CDP commands to an iPhone Safari tab through the tunnel (127.0.0.1:9223), for when the `iphone` MCP isn't loaded or fails.
# Picks the first tab whose URL matches -Match. -Steps is a JSON array of {"method", "params", "save", "sleep"}:
#   "save": file name (no extension) for a Page.captureScreenshot result, written to -OutDir
#   "sleep": milliseconds to wait before the step (e.g. after Page.reload)
# Examples:
#   .\cdp.ps1 -Status                      # tabs, which local processes hold a connection to the phone, the bridge's recent refusals
#   .\cdp.ps1 -Match '*ca-tims*' -OutDir $env:TEMP -Steps '[{"method":"Runtime.evaluate","params":{"expression":"(() => innerWidth)()","returnByValue":true}},{"method":"Page.captureScreenshot","params":{"format":"png"},"save":"shot"}]'
# Wrap evaluated scripts in (() => { ... })(): top-level const survives between calls.
# Stops at the first step that fails and prints why (exit 2): who holds the tab, and the bridge's own reason from its log on the Mac.
param([string]$Port = '9223', [string]$Match = '*ca-tims*', [string]$Steps, [string]$OutDir, [switch]$Status,
      [string]$Mac = 'seres@192.168.2.119')

function Show-Clients {
  # Every local process with a connection open to the tunnel. WebKit serves one inspector per tab, and the bridge
  # lets one client session hold a tab at a time: an `iphone` MCP that was used once holds every tab it attached
  # to for as long as its Claude Code session lives, idle or not.
  $conns = Get-NetTCPConnection -RemotePort $Port -State Established -ErrorAction SilentlyContinue |
    Where-Object RemoteAddress -in '127.0.0.1', '::1'
  if (-not $conns) { "clients on ${Port}: none"; return }
  foreach ($pid_ in ($conns.OwningProcess | Sort-Object -Unique)) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$pid_" -ErrorAction SilentlyContinue
    if (-not $p) { "client on ${Port}: pid $pid_ (gone)"; continue }
    $what = if ($p.CommandLine -match 'chrome-devtools-mcp') { 'iphone MCP' } elseif ($p.CommandLine -match 'cdp\.ps1') { 'cdp.ps1' } else { $p.Name }
    # Walk up to the Claude Code process that owns the MCP, so the session can be told apart from others.
    $owner = $null; $q = $p
    for ($i = 0; $i -lt 6 -and $q; $i++) {
      $q = Get-CimInstance Win32_Process -Filter "ProcessId=$($q.ParentProcessId)" -ErrorAction SilentlyContinue
      if ($q -and $q.Name -like 'claude*') { $owner = "claude pid $($q.ProcessId), started $($q.CreationDate.ToString('dd.MM HH:mm'))"; break }
    }
    "client on ${Port}: $what, pid $pid_, started $($p.CreationDate.ToString('dd.MM HH:mm'))$(if ($owner) { " ($owner)" })"
  }
}

function Show-Bridge([string]$PageId) {
  # The bridge's own reasons, from its log on the Mac. The interesting lines:
  #   "already being debugged over Web Inspector connection X"  the phone still lists X (often a dead connection) as the tab's inspector
  #   "the session holding it did not release it in time"       another client of the bridge holds the tab (usually an iphone MCP)
  #   "busy with another debugger session, not attaching"       same, seen from a browser-level client such as the MCP
  #   "stopped responding (Page.getResourceTree)"                the tab doesn't answer (a background tab iOS has suspended)
  #   "taking the page over from the session holding it"         a newer page-level client (cdp.ps1, DevTools) took the tab from an older one
  $pat = 'already being debugged|did not release|busy with another|stopped responding|not found|no longer inspectable|taking the page over|Started server|disconnected|connection to the device lost'
  $lines = & ssh.exe -o BatchMode=yes -o ConnectTimeout=5 $Mac "grep -E '$pat' ~/Library/Logs/iphone-cdp.log | tail -n 200" 2>$null
  if (-not $lines) { 'bridge log: unreadable (ssh failed?)'; return }
  $start = ($lines | Select-String 'Started server' | Select-Object -Last 1)
  $since = if ($start) { $lines[($start.LineNumber - 1)..($lines.Count - 1)] } else { $lines }
  $rel = if ($PageId) { $since | Where-Object { $_ -match [regex]::Escape($PageId) -or $_ -match 'Started server|disconnected|lost' } } else { $since }
  # Collapse the bridge's retry spam (one line every 5-30 s) to the last line per distinct message.
  $rel | Group-Object { $_ -replace '^\S+ \S+ ', '' } | ForEach-Object { "bridge: $($_.Group[-1])" + $(if ($_.Count -gt 1) { "  (x$($_.Count))" }) } | Select-Object -Last 10
}

function Fail([string]$Why, [string]$PageId) {
  "FAILED: $Why"
  Show-Clients
  Show-Bridge $PageId
  exit 2
}

$tabs = Invoke-RestMethod "http://127.0.0.1:$Port/json/list" -TimeoutSec 10 -ErrorAction SilentlyContinue
if ($Status) {
  try { $v = Invoke-RestMethod "http://127.0.0.1:$Port/json/version" -TimeoutSec 5; "bridge: $($v.Browser) / $($v.'User-Agent')" } catch { "tunnel: no answer on $Port" }
  if ($tabs) { $tabs | ForEach-Object { "tab $($_.id)  $($_.url)" } } else { 'tabs: none (phone locked, unplugged, or no Safari tab open)' }
  Show-Clients
  Show-Bridge
  exit 0
}

$t = $tabs | Where-Object url -like $Match | Select-Object -First 1
if (-not $t) { 'no matching tab'; exit 1 }
"tab: $($t.url)"
$ws = [System.Net.WebSockets.ClientWebSocket]::new(); $ct = [Threading.CancellationToken]::None
if (-not $ws.ConnectAsync([Uri]$t.webSocketDebuggerUrl, $ct).Wait(15000)) { Fail 'connect timeout' $t.id }
$buf = [byte[]]::new(4MB); $id = 0
function Invoke-Cdp($method, $params) {
  $script:id++
  if ($ws.State -ne 'Open') { return @{ error = 'closed by bridge' } }
  $msg = @{ id = $script:id; method = $method; params = $(if ($params) { $params } else { @{} }) } | ConvertTo-Json -Compress -Depth 8
  $ws.SendAsync([ArraySegment[byte]][Text.Encoding]::UTF8.GetBytes($msg), 'Text', $true, $ct).Wait() | Out-Null
  $sb = [Text.StringBuilder]::new(); $deadline = (Get-Date).AddSeconds(45)
  while ((Get-Date) -lt $deadline -and $ws.State -eq 'Open') {
    $r = $ws.ReceiveAsync([ArraySegment[byte]]$buf, $ct)
    if (-not $r.Wait(45000)) { return @{ error = 'receive timeout' } }
    # The bridge closes the socket when it can't get the tab within 30 s (someone else holds it).
    if ($r.Result.MessageType -eq 'Close') { return @{ error = 'closed by bridge' } }
    [void]$sb.Append([Text.Encoding]::UTF8.GetString($buf, 0, $r.Result.Count))
    if ($r.Result.EndOfMessage) {
      $s = $sb.ToString(); [void]$sb.Clear()
      $o = $s | ConvertFrom-Json
      if ($o.id -eq $script:id) { return $o }
    }
  }
  if ($ws.State -ne 'Open') { return @{ error = 'closed by bridge' } }
  @{ error = 'timeout' }
}
foreach ($st in ($Steps | ConvertFrom-Json)) {
  if ($st.sleep) { Start-Sleep -Milliseconds $st.sleep }
  $p = @{}; if ($st.params) { $st.params.PSObject.Properties | ForEach-Object { $p[$_.Name] = $_.Value } }
  $sw = [Diagnostics.Stopwatch]::StartNew(); $o = Invoke-Cdp $st.method $p; $ms = $sw.ElapsedMilliseconds
  if ($o.error -is [string]) { Fail "$($st.method) ($ms ms): $($o.error)" $t.id }
  if ($st.save -and $o.result.data) {
    $f = Join-Path $OutDir "$($st.save).png"; [IO.File]::WriteAllBytes($f, [Convert]::FromBase64String($o.result.data))
    "== $($st.method) ($ms ms) -> $f"
  } else {
    $txt = $o | ConvertTo-Json -Compress -Depth 8
    "== $($st.method) ($ms ms) " + $txt.Substring(0, [Math]::Min(600, $txt.Length))
  }
}
$ws.Dispose()
