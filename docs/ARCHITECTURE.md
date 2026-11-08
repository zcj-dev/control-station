# 架构详解 · control-station

## 四层资产

| 层 | 主机 | 端口 | 组件 |
|---|---|---|---|
| 公网入口 | VPS 100.109.206.100 | 80 / 8444 | nginx + Let's Encrypt |
| 代理 | VPS | 443 / 8443 / 9443 | sing-box Reality / Hysteria2 / TUIC |
| 编排 | VPS | 9000 | control-station orchestrator (systemd) |
| 观测 | VPS | 9090 / 8080 / 9100 | prometheus / cadvisor / node-exporter |
| 运行时 | PC 100.117.110.112 | 3080 / 8326 | DSH Web + OpenAI 网关 |
| 接入 | PC | 3081 / 8327 | Caddy 短链 + 带 key 转发壳 |
| 移动自动化 | PC | 8765 | u2mcp (77 工具, MCP streamable-http) |
| 执行器 | PC | — | worker.py (轮询编排器) |

## 闭环数据流

```plain
  调用方                     VPS                          PC
    │                         │                            │
    │ POST /submit            │                            │
    ├────────────────────────►│                            │
    │                         │ 落盘 pending.jsonl         │
    │                         │ 状态 = queued              │
    │ ◄─── 202 {job_id} ──────┤                            │
    │                         │                            │
    │ GET /status/<id>        │                            │
    ├────────────────────────►│                            │
    │                         │◄──── POST /claim ─────────┤ worker.py 轮询(5s)
    │                         │     (领到任务)             │
    │                         │                            │
    │                         │            POST /v1/chat/completions
    │                         │            ────────────────►│ DSH 网关 :8326
    │                         │                            │ 调 NIM 模型执行
    │                         │◄──── POST /result ────────┤
    │                         │  状态 = done + result      │
    │ ◄─── {status, result} ──┤                            │
```

**设计要点**：

- 编排器**零外部依赖**（纯标准库），VPS 上 `python3 server.py` 直接跑，不引入依赖地狱
- 任务**先落盘再处理**（`pending.jsonl` + `state.json`），VPS 重启不丢任务
- worker **主动轮询**而非服务端 push —— 穿透 NAT/防火墙简单，PC 离线时任务留在队列
- worker **禁系统代理**（`net.py`）—— v2rayN 的 `HTTP_PROXY` 会把内网请求丢给代理 → 502

## 端口绑定（易踩）

```
DSH Web  :3080  绑 100.117.110.112  (Tailscale IP, 不绑 127.0.0.1)
DSH 网关 :8326  绑 127.0.0.1        (仅本机)
Caddy    :3081  绑 0.0.0.0
Caddy    :8327  绑 0.0.0.0
u2mcp    :8765  绑 0.0.0.0
```

**健康检查必须用对应地址**，用 127.0.0.1 探 3080 会误报 DOWN。

## 本轮踩坑

### 坑 1：系统代理污染内网请求

**症状**：worker 连 VPS 报 `http 502`，但 `Invoke-WebRequest` 直连同一个 URL 返回 200。

**根因**：本机装了 v2rayN，环境里有 `HTTP_PROXY=http://127.0.0.1:10808`。Python 的 `urllib` 默认读环境变量 → Tailscale 内网请求被丢给代理 → 代理不认识 `100.109.206.100` → 502。

**修法**：抽出 `orchestrator/net.py`，import 时清掉所有 proxy 变量 + 设 `NO_PROXY=*`。**所有访问内网的脚本都必须先 import 它**。

### 坑 2：argparse default 拿不到 .env

**症状**：worker 报 `DSH gateway key missing`，但 `load_env()` 明明读到了。

**根因**：`argparse` 的 `default=` 在 `main()` 里求值，我写的是 `os.environ.get('DSH_GATEWAY_KEY')` —— 但 `load_env()` 只返回 dict，**没写进 `os.environ`**。

**修法**：`for k, v in env.items(): os.environ.setdefault(k, v)` 之后再建 parser。

### 坑 3：URL 少 http:// 前缀

**症状**：worker 报 `unknown url type: 100.109.206.100`。

**根因**：`--server` 默认值拼成 `'%s:%s' % (ip, port)`，`urllib` 需要完整 scheme。

**修法**：`'http://%s:%s'`。

### 坑 4：Register-ScheduledTask Access denied

**症状**：`Register-ScheduledTask -TaskName U2MCP-Supervisor ...` → `Access is denied (0x80070005)`。

**根因**：CIM 路径本身要更高权限，**即使显式给了 `-Principal -LogonType Interactive -RunLevel Limited` 也不行**。而用 XML 导入同样的 principal 却能成功。

**修法**：`schtasks /Create /TN <name> /XML <file> /F`，XML 里 `LogonType=InteractiveToken` + `RunLevel=LeastPrivilege`。

### 坑 5：base64 传输远程文件（老坑 9 的延续）

多层 shell 传配置，`$host` / 引号在 PowerShell → ssh → bash 三层被各层提前展开。

**修法**（全程通用）：本地构造完整文件 → base64 → `echo '<b64>' | base64 -d > <path>`。**零转义问题**。

### 坑 6：健康检查用 HTTP 状态码判活会误报

**症状**：DSH Web 报 DOWN、网关模型报 DOWN。

**根因**：
- DSH 根路径返回 **303** 跳转，不是 200
- Caddy 网关无 key 返回 **401**（这是**安全机制正常工作**）

**修法**：端口用 **TCP connect 探测**；网关模型请求**带 shell key**。

### 坑 7：密钥门禁抓到 sing-box 真密码

`vps/compose/sing-box/config.json` 里有 Reality UUID / Hysteria2 密码 / TUIC 密码。

**修法**：入库前统一替换成 `${VAR}` 占位符；`scripts/scan_secrets.py` 作为**强制门禁**，命中即失败。

## 为什么不装 Caddy 到 VPS

VPS 上 nginx 已经跑通公网入口（8444 + LE 证书），sing-box 占着 443。装 Caddy 会：
- 多一层端口管理
- 需要重新签证书（LE 已有）
- 引入新的配置面

**保持单入口**（nginx 8444），Caddy 只在 PC 侧做本地转发壳。
