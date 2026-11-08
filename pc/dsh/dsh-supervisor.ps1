# DSH 一体化启动器 (Windows PowerShell 5.1)
# 1. 拉起 dsh web (绑 Tailscale IP)  2. 抓 launch token  3. 同步 Caddy 短链  4. 守护重启

# ── 加载本地密钥 (不入库) ──────────────────────────────────
$ENV_FILE = "D:\000\dsh\.env"
if (Test-Path $ENV_FILE) {
  Get-Content $ENV_FILE -Encoding UTF8 | ForEach-Object {
    if ($_ -match "^\s*([^#=]+?)\s*=\s*(.*)$") {
      [Environment]::SetEnvironmentVariable($Matches[1].Trim(), $Matches[2].Trim().Trim('"'), "Process")
    }
  }
}

$ErrorActionPreference = "Continue"
$DSH_DIR    = "D:\000\dsh"
$DSH_CLI    = "C:\Users\chan\AppData\Roaming\npm\dsh.cmd"
$CADDY      = "C:\Users\chan\.dsh\caddy.exe"
$CADDY_CF   = "C:\Users\chan\.dsh\openai-gateway\Caddyfile"
$DSH_LOG    = "D:\000\dsh\dsh.log"
$TOKEN_FILE = "D:\000\dsh\dsh-token.txt"
$WEB_LOG    = "D:\000\dsh\dsh-web.log"
$WEB_ERR    = "D:\000\dsh\dsh-web.err"
$HOST_IP    = "100.117.110.112"
$PORT       = 3080
$CDY_PORT   = 3081
$GW_KEY     = $env:DSH_GATEWAY_KEY
$SHELL_KEY  = $env:DSH_SHELL_KEY
# 公网入口: VPS (DediRock) 上 nginx 的 /h 短链需要同步 DSH launch token
$VPS_HOST   = "root@23.95.35.82"
$VPS_SCRIPT = "/root/dsh-set-token.sh"
$SSHPASS    = $env:VPS_SSH_PASSWORD
$SSH_BIN    = "C:\Windows\System32\OpenSSH\ssh.exe"
$SSHPASS_BIN = "C:\Users\chan\scoop\shims\sshpass.exe"

New-Item -ItemType Directory -Path $DSH_DIR -Force | Out-Null

function Log($m) {
  Add-Content -Path $DSH_LOG -Value ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m)
}

# 用单引号数组拼 Caddyfile, 避免 here-string 引号转义问题
function Write-Caddyfile($token) {
  $L = New-Object System.Collections.Generic.List[string]
  $L.Add(':8327 {')
  $L.Add('    @authorized {')
  $L.Add('        header Authorization "Bearer ' + $SHELL_KEY + '"')
  $L.Add('    }')
  $L.Add('    handle @authorized {')
  $L.Add('        reverse_proxy 127.0.0.1:8326 {')
  $L.Add('            header_up Authorization "Bearer ' + $GW_KEY + '"')
  $L.Add('        }')
  $L.Add('    }')
  $L.Add('    handle {')
  $L.Add('        respond 401')
  $L.Add('    }')
  $L.Add('}')
  $L.Add('')
  $L.Add(':' + $CDY_PORT + ' {')
  $L.Add('    @d path /d /d/*')
  $L.Add('    handle @d {')
  $L.Add('        rewrite * /?token=' + $token)
  $L.Add('        reverse_proxy ' + $HOST_IP + ':' + $PORT + ' {')
  $L.Add('            header_down Location "http://' + $HOST_IP + ':' + $CDY_PORT + '/"')
  $L.Add('        }')
  $L.Add('    }')
  $L.Add('    handle {')
  $L.Add('        reverse_proxy ' + $HOST_IP + ':' + $PORT + ' {')
  $L.Add('            header_down Location "http://' + $HOST_IP + ':' + $CDY_PORT + '/"')
  $L.Add('        }')
  $L.Add('    }')
  $L.Add('}')
  $text = ($L -join "`r`n") + "`r`n"
  [System.IO.File]::WriteAllText($CADDY_CF, $text, (New-Object System.Text.ASCIIEncoding))
}

function Sync-Caddy($token) {
  if ([string]::IsNullOrWhiteSpace($token)) { return }
  Write-Caddyfile $token
  & $CADDY reload --config $CADDY_CF --adapter caddyfile 2>&1 | Out-Null
  Set-Content -Path $TOKEN_FILE -Value $token -NoNewline
  Log ("Caddy synced, token=" + $token.Substring(0, [Math]::Min(10, $token.Length)))
}

# 同步 token 到 VPS 的 nginx 公网短链 (户外访问用)
function Sync-VPS($token) {
  if ([string]::IsNullOrWhiteSpace($token)) { return }
  if (-not (Test-Path $SSHPASS_BIN)) { Log "sshpass not found, skip VPS sync"; return }
  try {
    $env:SSHPASS = $SSHPASS
    $out = & $SSHPASS_BIN -e $SSH_BIN -o StrictHostKeyChecking=no -o ConnectTimeout=12 -o BatchMode=no `
            "$VPS_HOST" "bash $VPS_SCRIPT $token" 2>&1
    $txt = ($out | Out-String).Trim()
    if ($txt -match "nginx reloaded") {
      Log "VPS public link synced"
    } else {
      Log ("VPS sync: " + ($txt -replace "`r?`n", " | "))
    }
  } catch {
    Log ("VPS sync ERR: " + $_.Exception.Message)
  }
}

function Find-Token {
  foreach ($f in @($WEB_LOG, $WEB_ERR)) {
    if (Test-Path $f) {
      $m = Select-String -Path $f -Pattern "token=([A-Za-z0-9_-]+)" -ErrorAction SilentlyContinue | Select-Object -Last 1
      if ($m -and $m.Matches.Count -gt 0) { return $m.Matches[0].Groups[1].Value }
    }
  }
  return ""
}

Log "supervisor starting"
$lastToken = ""
$proc = $null

while ($true) {
  try {
    $conn = Get-NetTCPConnection -LocalPort $PORT -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $conn) {
      # a process we launched earlier may still be binding the port; never start a second one
      if ($proc -and -not $proc.HasExited) {
        Start-Sleep -Seconds 5
        continue
      }
      Log "DSH down on $PORT - launching"
      Remove-Item $WEB_LOG, $WEB_ERR -ErrorAction SilentlyContinue
      $proc = Start-Process -FilePath $DSH_CLI `
            -ArgumentList @("web","--no-open","--host",$HOST_IP,"--port",$PORT,"--trusted-host","23.95.35.82") `
            -WorkingDirectory "C:\Users\chan\.dsh" `
            -RedirectStandardOutput $WEB_LOG -RedirectStandardError $WEB_ERR `
            -PassThru -WindowStyle Hidden
      Log "launched pid " + $proc.Id
      # wait for DSH to bind the port (Bun model-list fetch + NIM probe can take minutes)
      $ready = $false
      for ($w = 0; $w -lt 60; $w++) {
        Start-Sleep -Seconds 5
        $c2 = Get-NetTCPConnection -LocalPort $PORT -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($c2) { $ready = $true; Log ("DSH ready on " + $PORT + " after " + (($w+1)*5) + "s"); break }
        if ($proc.HasExited) { Log "DSH exited early"; break }
        # publish the launch token as soon as DSH writes it, not after the full wait
        $tk = Find-Token
        if ($tk -and $tk -ne $lastToken) { Sync-Caddy $tk; Sync-VPS $tk; $lastToken = $tk }
      }
      if (-not $ready) { Log "DSH not ready yet; keeping the process and rechecking" }
    }

    $tok = Find-Token
    if ($tok -and $tok -ne $lastToken) {
      Sync-Caddy $tok
      Sync-VPS $tok
      $lastToken = $tok
    }
  } catch {
    Log ("ERR: " + $_.Exception.Message)
  }
  Start-Sleep -Seconds 8
}
