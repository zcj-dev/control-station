# control-station · 中控站架构

> 美西 VPS 到本地中控站的动脉贯通。
> 把 VPS / PC / DSH / MCP 四层资产模块化入库，可复现部署，Git 版本封印。

## 架构

```plain
                         公网 HTTPS (LE 证书, 8444)
   移动端浏览器 ──────────────────────────────┐
                                              ▼
┌──────────────────────────────────────────────────────────────┐
│  VPS 100.109.206.100 (DediRock, Debian 12)                   │
│                                                              │
│  nginx :80/:8444  ──► 公网入口 /h/ + TLS + 口令                │
│  :443                ──► sing-box Reality (不动)              │
│  :9000  control-station 编排器  ◄── 指令入口 + 队列            │
│                                                              │
│  Docker × 11: sing-box / prometheus / cadvisor /              │
│              traffmonetizer / urmine / repocket / ...        │
└──────────────────────────┬───────────────────────────────────┘
                           │ Tailscale (100.117.110.112)
                           ▼
┌──────────────────────────────────────────────────────────────┐
│  PC longz (Windows, 神舟 RTX 3060)                          │
│                                                              │
│  DSH :3080 Web UI ──► :8326 OpenAI 兼容网关 (41 模型)         │
│  Caddy :3081 / :8327 ──► 短链 + 带 key 的转发壳               │
│  u2mcp :8765 ──► Android 自动化 MCP (77 工具)                 │
│                                                              │
│  worker.py ──► 轮询 VPS 编排器领任务 → 调 DSH 网关 → 回报     │
│  dsh-supervisor.ps1 / u2mcp-supervisor.ps1 ──► 崩溃自愈      │
└──────────────────────────────────────────────────────────────┘
```

## 目录结构

```plain
control-station/
├── .env.example              密钥模板 (真值只进 .env, 已 gitignore)
├── .gitignore
├── README.md
├── pc/                       PC 层
│   ├── dsh/                  dsh-supervisor.ps1 + 计划任务 XML
│   ├── mcp/                  u2mcp-supervisor.ps1
│   ├── gateway/              Caddyfile (:3081 / :8327)
│   └── scripts/
├── dsh/                      DSH 配置层 (已脱敏)
│   ├── config/               settings / catalog / registry / cordis.patch
│   └── plugin-patch/         插件源码补丁锚点
├── vps/                      VPS 层
│   ├── nginx/                dsh-harness.conf + nginx.conf
│   ├── compose/              sing-box config + 11 容器 inspect + 清单
│   ├── host-info.json
│   └── scripts/
├── orchestrator/             ★ 闭环核心
│   ├── server.py             VPS 侧编排器 (零依赖, 标准库)
│   ├── worker.py             PC 侧执行器
│   ├── net.py                共享网络层 (清系统代理)
│   └── queue/                运行时队列 (gitignore)
├── scripts/
│   ├── bootstrap.py          一键部署 (pc / vps / all / check)
│   └── scan_secrets.py       ★ 入库前密钥扫描 (强制门禁)
└── docs/
    ├── ARCHITECTURE.md       架构详解 + 踩坑
    └── OPERATIONS.md         运维手册
```

## 快速开始

```bash
# 1. 配置密钥
copy .env.example .env       # Windows
# 编辑 .env 填入真值

# 2. 部署
python scripts/bootstrap.py all

# 3. 健康检查
python scripts/bootstrap.py check
```

## 闭环用法

```bash
# 提交指令 (任何能访问 VPS:9000 的地方)
curl -X POST http://100.109.206.100:9000/submit \
  -H "Content-Type: application/json" \
  -d '{"prompt":"总结今天的部署","model":"nvidia_nim/stepfun-ai/step-5-preview"}'
# => {"ok": true, "job_id": "job-xxxx", "status": "queued"}

# 查询结果
curl http://100.109.206.100:9000/status/job-xxxx
```

**实测**：提交 → VPS 队列 → PC worker 领 → DSH 调度模型 → 执行 → 回传，**26.74 秒完成**。

## 安全设计

| 层 | 机制 |
|---|---|
| 密钥 | 全部在 `.env`（已 gitignore）+ `.env.example` 占位；`scan_secrets.py` 入库前强制扫描 |
| 网关 | Caddy 层校验 `Bearer LOBE-2026-SHELL`，无 key 直接 401 |
| 公网 | nginx TLS + Let's Encrypt 真证书 + 访问口令 + DSH 签名 cookie |
| Origin | DSH `trustedHosts` 白名单 |
| 编排器 | 仅监听 Tailscale 内网可达地址（建议加访问控制） |

## 关键约束

- **VPS 公网 22 端口不通** → SSH 必须走 Tailscale IP `100.109.206.100`
- **本机有系统代理** `HTTP_PROXY=127.0.0.1:10808` → 内网请求必须清代理（见 `orchestrator/net.py`）
- **不碰 443** → sing-box Reality 占用
- **不碰 Docker 11 容器** → 只抽配置进库，不重建
- **Windows PowerShell 5.1** → 文件写入用 Python 脚本，避开引号地狱

## 验证命令

```bash
python scripts/bootstrap.py check     # 全链路健康检查
python scripts/scan_secrets.py        # 密钥泄漏扫描 (0 命中才算过)
```
