# u2mcp-supervisor.ps1 —— 手机/平板 MCP 服务守护
# 职责:
#   1. 保证 u2mcp HTTP 服务活着 (8765)
#   2. 定期清理并重连离线设备 (无线 adb 锁屏即断, 必须自愈)
#   3. 设备恢复后自动连上, MCP 侧无需重启
$ErrorActionPreference = "Continue"

$PY       = "C:\adb\Farm\.venv\Scripts\python.exe"
$ADB      = "C:\adb\platform-tools\adb.exe"
$PORT     = 8765
$TOKEN    = "u2mcp-mobile-2026"
$LOG      = "D:\000\dsh\logs\u2mcp-supervisor.log"
$ERRLOG   = "C:\adb\Farm\logs\u2mcp.err"
$OUTLOG   = "C:\adb\Farm\logs\u2mcp.log"

# 已知设备端点候选 (IP 会变, 所以每次都靠 mDNS + 全网扫描找回)
$CANDIDATES = @(
  "192.168.0.110:5555",
  "192.168.0.104:5555",
  "192.168.0.106:5555",
  "192.168.0.103:37231"
)

New-Item -ItemType Directory -Path (Split-Path $LOG) -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path $ERRLOG) -Force | Out-Null

function Log($m) {
  $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
  Add-Content -Path $LOG -Value "[$ts] $m"
}

function ServerAlive {
  try {
    $c = Get-NetTCPConnection -LocalPort $PORT -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    return $null -ne $c
  } catch { return $false }
}

function OnlineDevices {
  $out = @()
  try {
    $raw = & $ADB devices 2>&1
    foreach ($line in $raw) {
      if ("$line" -match "^(\S+)\s+device$") { $out += $Matches[1] }
    }
  } catch {
    $out = @()
  }
  return $out
}

function ReconnectDevices {
  # 1) drop stale offline connections
  try { $r = & $ADB disconnect 2>&1 } catch { Write-Host "disconnect failed" }
  Start-Sleep -Seconds 2

  # 2) mDNS 找回当前端点 (最稳, 不依赖写死 IP)
  try {
    $mdns = & $ADB mdns services 2>&1
    foreach ($line in $mdns) {
      $parts = "$line" -split '\s+'
      if ($parts.Count -ge 3 -and $parts[1] -like "_adb*") {
        $ep = $parts[2]
        if ($ep -match "\d+\.\d+\.\d+\.\d+:\d+$") {
          $r = & $ADB connect $ep 2>&1
        }
      }
    }
  } catch {
    Write-Host "mdns scan failed"
  }

  # 3) fallback: try known candidate endpoints
  foreach ($ep in $CANDIDATES) {
    $r = & $ADB connect $ep 2>&1
    Write-Host "connect $ep -> $r"
  }
}

Log "supervisor starting"
$lastReconnect = ""
$reconnectCooldown = 30

while ($true) {
  try {
    # --- 服务存活 ---
    if (-not (ServerAlive)) {
      Log "u2mcp not listening on $PORT - launching"
      $env:PYTHONIOENCODING = "utf-8"
      $env:PATH = $env:PATH + ";C:\Users\chan\AppData\Local\Microsoft\WinGet\Packages\Genymobile.scrcpy_Microsoft.Winget.Source_8wekyb3d8bbwe\scrcpy-win64-v5.0.1"
      Start-Process -FilePath $PY `
        -ArgumentList @("-m","u2mcp","http","--host","0.0.0.0","--port","$PORT","--token",$TOKEN,"--log-level","info") `
        -WorkingDirectory "C:\adb\Farm" `
        -RedirectStandardOutput $OUTLOG -RedirectStandardError $ERRLOG `
        -PassThru -WindowStyle Hidden | Out-Null
      Start-Sleep -Seconds 20
      if (ServerAlive) { Log "u2mcp up on $PORT" } else { Log "u2mcp still not up; will retry next loop" }
    }

    # --- 设备自愈 ---
    $devs = OnlineDevices
    if ($devs.Count -eq 0) {
      $now = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
      if ($now -ne $lastReconnect) {
        $lastReconnect = $now
        Log "no device online - reconnecting"
        ReconnectDevices
        Start-Sleep -Seconds 5
        $devs = OnlineDevices
        if ($devs.Count -gt 0) { Log ("device(s) back: " + ($devs -join ", ")) }
      }
    } else {
      if ($lastReconnect -ne "") { Log ("device(s) online: " + ($devs -join ", ")); $lastReconnect = "" }
    }
  } catch {
    Log ("ERR: " + $_.Exception.Message)
  }
  Start-Sleep -Seconds 12
}
