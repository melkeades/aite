# Safari and iPhone Safari from Windows

Lets Claude Code on the Windows PC drive Safari on the Mac and Safari on the iPhone: screenshots, reloads, DOM and CSS edits, console, network. Everything goes over one SSH connection; nothing on the Mac listens on the LAN except SSH.

```
Windows (Claude Code)
 ├─ MCP "safari": ssh seres@192.168.2.119 ./safari-mcp.sh ──> Safari Technology Preview: safaridriver --mcp
 └─ MCP "iphone": chrome-devtools-mcp → 127.0.0.1:9223
        └─ task "iPhone CDP tunnel": ssh -L 9223 ──> Mac 127.0.0.1:9222
                                                   └─ LaunchAgent: pymobiledevice3 webinspector cdp ──USB──> iPhone Safari
```

| Piece | Where | Starts | Log |
|---|---|---|---|
| SSH key `id_mac` (no passphrase) | PC `~/.ssh/id_mac`, Mac `~/.ssh/authorized_keys` | — | `ssh -v` |
| `safari` MCP | PC, user scope | per Claude Code session | — |
| `iphone` MCP | PC, user scope | per Claude Code session | — |
| Tunnel 9223 → 9222 | PC task "iPhone CDP tunnel" → `~/.local/bin/iphone-tunnel.ps1` | at Windows logon, reconnects every 10 s | `%LOCALAPPDATA%\iphone-tunnel.log` |
| iPhone CDP bridge | Mac LaunchAgent `com.seres.iphone-cdp` | at Mac login, restarts on exit | `~/Library/Logs/iphone-cdp.log` |
| `tunneld` (root, iOS developer tunnel) | Mac LaunchDaemon `com.seres.pmd3-tunneld` | at Mac boot, before login; restarts on exit | `/var/log/pmd3-tunneld.log` |

Machines: Mac `seres@192.168.2.119` (macOS 27.2, Safari 27.2 and Safari Technology Preview), iPhone on USB to the Mac (iOS 26.5.2), pymobiledevice3 11.43.1, PC at `192.168.2.152` (Vite on `:5173`).

**Port 9222 on Windows is taken** by the Playwright Chrome (`aite/playwright`, `--remote-debugging-port=9222`). The iPhone tunnel uses **9223** locally. If `curl.exe http://127.0.0.1:9223/json/version` ever answers `Chrome/…` instead of `Safari` / `pymobiledevice3`, something points at the wrong port.

## Setup

### Mac

1. Install [Safari Technology Preview](https://developer.apple.com/safari/resources/). Safari 27 has the same MCP at `/usr/bin/safaridriver --mcp`; `safari-mcp.sh` uses the Preview.
2. In whichever Safari the MCP uses: Settings > Advanced > **Show features for web developers**, then Settings > Developer > **Allow remote automation and external agents**.
3. System Settings > General > Sharing > **Remote Login** on.
4. Keep it awake: `sudo pmset -a sleep 0` (display sleep is fine).
5. pymobiledevice3: `python3 -m pip install --user -U pymobiledevice3` (lands in `~/Library/Python/3.9/bin`).
6. `~/safari-mcp.sh`, then `chmod +x ~/safari-mcp.sh`:

   ```zsh
   #!/bin/zsh
   exec "/Applications/Safari Technology Preview.app/Contents/MacOS/safaridriver" --mcp
   ```

7. iPhone: plug into the Mac by USB, tap **Trust**. Settings > Apps > Safari > Advanced: **Web Inspector** and **Remote Automation** on. Turn on the Tampermonkey `devMode` script on the phone if you want the Vite build there.
8. `tunneld` as a LaunchDaemon: see [tunneld](#tunneld).
9. The bridge as a LaunchAgent. `~/Library/LaunchAgents/com.seres.iphone-cdp.plist`:

   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
   <plist version="1.0">
   <dict>
     <key>Label</key><string>com.seres.iphone-cdp</string>
     <key>ProgramArguments</key>
     <array>
       <string>/Users/seres/Library/Python/3.9/bin/pymobiledevice3</string>
       <string>webinspector</string><string>cdp</string>
       <string>--host</string><string>127.0.0.1</string>
       <string>--port</string><string>9222</string>
     </array>
     <key>RunAtLoad</key><true/>
     <key>KeepAlive</key><true/>
     <key>ThrottleInterval</key><integer>15</integer>
     <key>StandardOutPath</key><string>/Users/seres/Library/Logs/iphone-cdp.log</string>
     <key>StandardErrorPath</key><string>/Users/seres/Library/Logs/iphone-cdp.log</string>
   </dict>
   </plist>
   ```

   ```zsh
   plutil -lint ~/Library/LaunchAgents/com.seres.iphone-cdp.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.seres.iphone-cdp.plist
   curl -s http://127.0.0.1:9222/json/version     # "Browser":"Safari","User-Agent":"pymobiledevice3"
   ```

   Keep `--host 127.0.0.1`: the bridge has no authentication, so it must not listen on the LAN.

### Windows

Run these one at a time in PowerShell.

1. **A separate key with no passphrase.** Don't reuse or overwrite `id_ed25519`: it has a passphrase and `ssh-agent` is disabled, so nothing non-interactive can use it. Press **Enter** at both passphrase prompts:

   ```powershell
   ssh-keygen -t ed25519 -f $env:USERPROFILE\.ssh\id_mac -C mac-debug
   ```

   Don't pass `-N '""'` from PowerShell: it sets the passphrase to the two characters `""` (see [Troubleshooting](#troubleshooting)).

2. Copy it to the Mac (asks for the Mac password once):

   ```powershell
   type $env:USERPROFILE\.ssh\id_mac.pub | ssh seres@192.168.2.119 "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
   ```

3. `~/.ssh/config`, so every `ssh` to the Mac uses that key:

   ```
   Host 192.168.2.119
     User seres
     IdentityFile ~/.ssh/id_mac
     IdentitiesOnly yes
   ```

4. Test. It must print `ok` with no prompt; `BatchMode` makes it fail instead of asking, which is how the MCP and the task run:

   ```powershell
   ssh -o BatchMode=yes seres@192.168.2.119 echo ok
   ```

5. The MCPs, at **user** scope (without `-s user` they only load in the folder you ran the command from). On native Windows `npx` needs `cmd /c`:

   ```powershell
   claude mcp add -s user safari -- ssh seres@192.168.2.119 ./safari-mcp.sh
   claude mcp add -s user iphone -- cmd /c npx -y chrome-devtools-mcp@latest --browserUrl http://127.0.0.1:9223
   ```

6. The tunnel script, `~/.local/bin/iphone-tunnel.ps1`:

   ```powershell
   $log = "$env:LOCALAPPDATA\iphone-tunnel.log"
   while ($true) {
     Add-Content $log "$(Get-Date -Format s) connecting"
     & ssh.exe -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 `
       -N -L 9223:127.0.0.1:9222 seres@192.168.2.119 2>&1 | ForEach-Object { Add-Content $log "$(Get-Date -Format s) $_" }
     Add-Content $log "$(Get-Date -Format s) ssh exited ($LASTEXITCODE), retrying in 10s"
     Start-Sleep 10
   }
   ```

7. A logon task that runs it with no window (`conhost --headless`); no admin needed:

   ```powershell
   $pwsh = (Get-Command pwsh).Source
   $action = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\conhost.exe" -Argument "--headless `"$pwsh`" -NoProfile -ExecutionPolicy Bypass -File `"$env:USERPROFILE\.local\bin\iphone-tunnel.ps1`""
   $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
   $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
   $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
   Register-ScheduledTask -TaskName 'iPhone CDP tunnel' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force
   Start-ScheduledTask -TaskName 'iPhone CDP tunnel'
   ```

8. Restart Claude Code: MCP tools load only in new sessions.

### Changes from the first version of these steps

| First version | Now | Why |
|---|---|---|
| `ssh-keygen -t ed25519` (default `id_ed25519`) | separate `id_mac`, no passphrase, set in `~/.ssh/config` | `id_ed25519` already existed with a passphrase; the MCP and tunnel can't type it, and enabling `ssh-agent` needs an admin shell |
| `ssh-keygen … -N '""'` | press Enter at the prompts | PowerShell passes `""` literally; the key got a 2-character passphrase and login failed after "Server accepts key" |
| `claude mcp add safari …` | `claude mcp add -s user …` | default scope is local to the current folder; added from `C:\Users\Serj`, they didn't load in project folders |
| `iphone` on `127.0.0.1:9222` | `127.0.0.1:9223` | 9222 is the Playwright Chrome; the MCP would have driven that Chrome, and the tunnel failed with `bind … Permission denied` |
| `--browserUrl <http://127.0.0.1:9222>` | no angle brackets | Notion wraps links in `<>`; pasted as-is the URL is invalid |
| `npx …` | `cmd /c npx …` | stdio servers on native Windows can't start `npx` directly |
| Terminal tabs for `tunneld` and the bridge, PowerShell window for the tunnel | LaunchDaemon, LaunchAgent and logon task | they died with their windows and didn't come back after a reboot |

## Checks

```powershell
ssh -o BatchMode=yes seres@192.168.2.119 echo ok                 # key login
curl.exe -s http://127.0.0.1:9223/json/version                     # "Browser":"Safari","User-Agent":"pymobiledevice3"
curl.exe -s http://127.0.0.1:9223/json/list                        # the iPhone's open Safari tabs
Get-ScheduledTask -TaskName 'iPhone CDP tunnel' | Select-Object State   # Running
Get-Content $env:LOCALAPPDATA\iphone-tunnel.log -Tail 5
claude mcp list                                                     # safari and iphone: √ Connected
```

```zsh
launchctl print gui/$(id -u)/com.seres.iphone-cdp | grep -E "state =|pid =|last exit"
tail -20 ~/Library/Logs/iphone-cdp.log
sudo launchctl print system/com.seres.pmd3-tunneld | grep -E "state =|pid ="
curl -s http://127.0.0.1:49151/                                     # tunneld: the iPhone's tunnel, keyed by its UDID
/Users/seres/Library/Python/3.9/bin/pymobiledevice3 usbmux list    # the iPhone, ConnectionType USB
```

`claude mcp list` only shows that the `iphone` process starts. It says `√ Connected` even with the tunnel down; check `/json/version` on 9223 for that.

## Maintenance

- **Restart the bridge** (after a pymobiledevice3 upgrade, or if `/json/list` is stale): `launchctl kickstart -k gui/$(id -u)/com.seres.iphone-cdp`
- **Restart the tunnel:** `Stop-ScheduledTask 'iPhone CDP tunnel'; Start-ScheduledTask 'iPhone CDP tunnel'`
- **Restart `tunneld`:** `sudo launchctl kickstart -k system/com.seres.pmd3-tunneld`
- **Upgrade pymobiledevice3:** `python3 -m pip install --user -U pymobiledevice3`, then restart `tunneld` and the bridge.
- **Mac IP changes:** update `~/.ssh/config` (`Host`), `iphone-tunnel.ps1`, and re-add the `safari` MCP. Better: a DHCP reservation for `.119` (and `.152` for Vite).
- **After a Mac reboot:** the LaunchAgent starts only once `seres` is logged in to the desktop, not at the login screen. `tunneld` starts at boot on its own.
- **After a Windows reboot:** the task starts at logon and retries every 10 s until the Mac answers.
- **Remove:**

  ```zsh
  launchctl bootout gui/$(id -u)/com.seres.iphone-cdp && rm ~/Library/LaunchAgents/com.seres.iphone-cdp.plist
  sudo launchctl bootout system/com.seres.pmd3-tunneld && sudo rm /Library/LaunchDaemons/com.seres.pmd3-tunneld.plist
  ```

  ```powershell
  Unregister-ScheduledTask -TaskName 'iPhone CDP tunnel' -Confirm:$false
  claude mcp remove -s user safari; claude mcp remove -s user iphone
  ```

### tunneld

`tunneld` (root) provides the iOS 17+ developer tunnel. It runs as a LaunchDaemon: it starts at boot, before anyone logs in, restarts if it exits, and opens a tunnel to the iPhone as soon as it's plugged in (the log shows `Created tunnel --rsd …`). Whether `webinspector cdp` works without it hasn't been tested; screenshots and JS worked with it running. It needs root, so installing it needs the Mac password.

`/Library/LaunchDaemons/com.seres.pmd3-tunneld.plist` (`/usr/bin/python3` with the user's `PYTHONPATH` is the same Xcode Python 3.9 that runs the bridge):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.seres.pmd3-tunneld</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string><string>-m</string><string>pymobiledevice3</string>
    <string>remote</string><string>tunneld</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>PYTHONPATH</key><string>/Users/seres/Library/Python/3.9/lib/python/site-packages</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>/var/log/pmd3-tunneld.log</string>
  <key>StandardErrorPath</key><string>/var/log/pmd3-tunneld.log</string>
</dict>
</plist>
```

Install it (copy the plist to `~` first, e.g. with `scp` from the PC; stop any `tunneld` running in a Terminal with Ctrl+C, or two copies fight over port 49151):

```zsh
sudo mv ~/com.seres.pmd3-tunneld.plist /Library/LaunchDaemons/
sudo chown root:wheel /Library/LaunchDaemons/com.seres.pmd3-tunneld.plist
sudo chmod 644 /Library/LaunchDaemons/com.seres.pmd3-tunneld.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/com.seres.pmd3-tunneld.plist
sudo launchctl print system/com.seres.pmd3-tunneld | grep -E "state =|pid ="    # state = running
```

The log starts with a urllib3 `NotOpenSSLWarning` (LibreSSL); it's harmless. To run it by hand instead, for debugging:

```zsh
sudo env PYTHONPATH=/Users/seres/Library/Python/3.9/lib/python/site-packages python3 -m pymobiledevice3 remote tunneld
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `safari` MCP: "connection timed out after 30000ms" | SSH is waiting for input (passphrase, host key, password) | `ssh -o BatchMode=yes seres@192.168.2.119 echo ok` shows the real error |
| `Permission denied (publickey,…)` right after `Server accepts key` (in `ssh -v`) | the key has a passphrase | In Git Bash: `ssh-keygen -y -P "" -f ~/.ssh/id_mac >/dev/null && echo none \|\| echo has-passphrase`. Clear it with `ssh-keygen -p -f $env:USERPROFILE\.ssh\id_mac` (old: the passphrase, e.g. `""`; new: Enter twice) |
| `Permission denied` and no `Server accepts key` | the public key isn't in the Mac's `authorized_keys`, or the config points at another key | redo step 2; check `IdentityFile` |
| `Set-Service ssh-agent … Access is denied` | needs an admin shell | not needed with `id_mac`; otherwise Terminal (Admin) |
| tunnel log: `bind [127.0.0.1]:9223: Permission denied` | another process holds 9223 (or a second tunnel) | `netstat -ano \| findstr :9223`; the task's `IgnoreNew` prevents duplicates of itself |
| `/json/version` on 9223 says `Chrome/…` | wrong port somewhere | the tunnel and the `iphone` MCP must both use 9223 |
| `/json/list` is `[]` | iPhone locked, unplugged, Web Inspector off, or no Safari tab open | unlock, open a tab, check `pymobiledevice3 usbmux list` |
| a tab is listed but commands get no answer | another inspector holds that page (Safari's Web Inspector on the Mac, or a second client) | WebKit allows one inspector session per page; close the other one |
| Mac `curl :9222` refused | bridge not running | `launchctl print …`, read `iphone-cdp.log`, `kickstart -k` |
| `devMode` is `false` | Tampermonkey script off in that browser | enable it in Safari Technology Preview and on the iPhone, reload |

## Using it

Learned from the first test run (desktop through the `safari` MCP, iPhone through raw CDP; `chrome-devtools-mcp` itself was only health-checked).

- **Mac screenshots:** pass `savePath`. Without it the PNGs came back cropped at odd sizes (474×463, 925×413); with it, the full viewport at 2× (2048×1328). The file is written on the Mac: `scp seres@192.168.2.119:/tmp/shot.png .`
- **Mac reload:** the MCP has no reload tool, and `location.reload()` through `evaluate_javascript` didn't reload. Use `navigate_to_url` with the same URL (a navigation, not a true Cmd+R).
- **Mac window:** each MCP connection opens its own Safari Technology Preview window, 1024×610 by default. Use `set_viewport_size` for exact widths. `evaluate_javascript` takes a function body: `return` the value.
- **Without the `iphone` MCP** (tools load only in new sessions): `cdp.ps1` in this folder sends CDP steps to the first matching tab on 9223, e.g. `.\cdp.ps1 -Match '*ca-tims*' -OutDir $env:TEMP -Steps '[{"method":"Page.captureScreenshot","params":{"format":"png"},"save":"shot"}]'`. See its header.
- **iPhone screenshots:** `Page.captureScreenshot` returns the viewport at device pixels (1170×2097 for 390×699 at 3×), about 0.5 s each. `Page.reload` is a native reload.
- **iPhone JS:** wrap each `Runtime.evaluate` in `(() => { … })()`. Top-level `const` survives between calls, so a second script declaring the same name fails with `Can't create duplicate variable`.
- **devMode:** comes from Tampermonkey in each browser, not from injection. Check with `document.devMode === true` and a script `src` containing `5173`.
