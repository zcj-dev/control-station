# 运维手册 · control-station

## 日常检查

```bash
python scripts/bootstrap.py check
```

输出示例：

```plain
  [OK ] DSH Web        100.117.110.112:3080
  [OK ] DSH 网关         127.0.0.1:8326
  [OK ] Caddy Web      127.0.0.1:3081
  [OK ] Caddy 网关       127.0.0.1:8327
  [OK ] u2mcp          127.0.0.1:8765
  [OK ] VPS 编排器   {"ok": true, ...}
  [OK ] 网关模型     41 个

整体: ALL GREEN
```

## 常用入口

| 用途 | 地址 |
|---|---|
| DSH Web (Tailscale) | `http://100.117.110.112:3080/?token=<当前token>` |
| DSH 短链 (Tailscale) | `http://100.117.110.112:3081/d/` |
| DSH 网关 (Tailscale) | `http://100.117.110.112:8327/v1/` + `Bearer LOBE-2026-SHELL` |
| 公网 (VPS) | `https://23.95.35.82.nip.io:8444/h/` |
| u2mcp MCP | `http://100.117.110.112:8765/mcp` + `Bearer u2mcp-mobile-2026` |
| 编排器 | `http://100.109.206.100:9000/health`（无鉴权）/ 其余接口需 `Bearer $CS_TOKEN` |

**token 每次 DSH 重启都会变** → 从 `D:\000\dsh\dsh-web.log` 最后一行 `dsh web: http://...token=` 取。
supervisor 会自动把新 token 同步到 Caddy 和 VPS 公网短链。

## 常用操作

```bash
# 重启 DSH (冷启动 40-120s)
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | Where-Object { $_.CommandLine -match 'dsh-supervisor' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; Start-Sleep 3; Start-ScheduledTask -TaskName DSH-Supervisor"

# 重启 worker
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*orchestrator\worker.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"

# 重启 VPS 编排器
ssh vps "systemctl restart control-station && systemctl is-active control-station"

# 看 VPS 编排器日志
ssh vps "tail -30 /opt/control-station/orchestrator.log"

# 看最近任务 (需要 token)
curl -H "Authorization: Bearer $CS_TOKEN" http://100.109.206.100:9000/jobs

# 取 VPS 上自动生成的 token (若 .env 里没配 CS_TOKEN)
ssh vps "cat /opt/control-station/queue/token.txt"
```

## SSH 到 VPS

```bash
ssh vps          # 用 ~/.ssh/config 的别名 (Tailscale IP)
```

**公网 22 端口不通**，必须走 Tailscale IP `100.109.206.100`。
密码在 `.env` 的 `VPS_SSH_PASSWORD`。

## 密钥管理

| 文件 | 是否入库 | 说明 |
|---|---|---|
| `.env` | ❌ gitignore | 真值 |
| `.env.example` | ✅ | 模板 + 占位符 |
| `D:\000\dsh\.env` | ❌ (仓库外) | 本机运行时真值 |

**每次改配置后必须跑门禁**：

```bash
python scripts/scan_secrets.py     # 0 命中才算过
```

## 重装 DSH 后的恢复

```bash
# 1. 停 DSH (插件退出时会用内存快照覆写 settings.json)
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*dsh*bin.js*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"

# 2. 从仓库还原配置 (按 ARCHITECTURE.md 的配置关系图)
#    dsh/config/settings.json      -> C:\Users\chan\.dsh\our-free-model\settings.json
#    dsh/config/catalog.json       -> C:\Users\chan\.dsh\our-free-model\catalog.json
#    dsh/config/cordis.patch.yml   -> C:\Users\chan\.dsh\profiles\web\cordis.patch.yml
#    dsh/config/registry.json      -> ...\dsh-our-free-model\src\providers\registry.json
#    dsh/plugin-patch/             -> ...\dsh-our-free-model\src\catalog.js (见 scripts/patch_plugin.py)

# 3. 起
python scripts/bootstrap.py pc
```

## 已知限制

- **手机/平板 Tailscale 离线** → 锁屏即断，无线 adb 关掉。本轮不依赖移动端实测。
- **OpenCode 免费池 429** → 上游免费额度用尽，与本仓库无关。
- **DSH 冷启动 40-220 秒** → Bun 拉模型列表 + NIM 探测；supervisor 等待上限 300 秒 + 进程存活守卫。
- **编排器无速率限制** → 有 token 鉴权，但没有请求频率限制。对公网暴露前建议加 nginx limit_req 或在编排器里加计数。

## ⚠️ 手工启动 DSH 一定会打断反代链路

**2026-11-09 实际事故**：宿主机重启后有人手工跑了
`node .../dsh/lib/bin.js web`，**漏掉 `--host` 参数** → DSH 绑到 `127.0.0.1:3080`
→ Caddy 反代目标 `100.117.110.112:3080` 不存在 → **Caddy :3081/:8327 连不上**
→ **VPS 公网入口 8444 返回 502**。

**正确做法**：**永远不要手工起 DSH**。要重启就跑：

```powershell
# 停掉手工起的实例, 让 supervisor 用正确参数接管
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
  Where-Object { $_.CommandLine -match "dsh-supervisor" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Seconds 3
schtasks /Run /TN "DSH-Supervisor"
```

supervisor 的启动参数（含 `--host` / `--port` / `--trusted-host`）在
`pc/dsh/dsh-supervisor.ps1` 的 `$HOST_IP` / `$PORT` 那几行，不要手抄。

**自查绑定是否正确**：

```powershell
Get-NetTCPConnection -LocalPort 3080 -State Listen |
  Select-Object LocalAddress, OwningProcess
# 必须是 100.117.110.112, 不是 127.0.0.1
```

## Caddy 已纳入 supervisor 守护

2026-11-09 之前 supervisor 只**写** Caddyfile 并 `caddy reload`，
**从不启动 Caddy 进程** —— Caddy 一挂就没人拉起，公网入口跟着 502。

现在 supervisor 每轮检查 `:3081` 是否监听，掉了就拉起。实测：

```plain
[07:12:14] Caddy down on 3081 - starting
[07:12:19] Caddy ready on 3081 pid 8096
```

**崩溃 → 6 秒自动恢复**，VPS 公网入口全程 200。
