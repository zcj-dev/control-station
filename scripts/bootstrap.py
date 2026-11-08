#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/bootstrap.py —— 一键部署 (PC + VPS)

用法:
    python scripts/bootstrap.py pc      # 只部署 PC 侧 (supervisor + Caddy)
    python scripts/bootstrap.py vps     # 只部署 VPS 侧 (编排器 + systemd)
    python scripts/bootstrap.py all     # 两边都部
    python scripts/bootstrap.py check   # 只做健康检查

依赖: paramiko (VPS 侧)。PC 侧只需要 PowerShell。
"""
import base64
import io
import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, 'orchestrator'))
import net  # noqa: E402

REPO = BASE
LOCAL_ENV = r'D:\000\dsh\.env'
DSH_DIR = r'D:\000\dsh'
SSH_KEY = os.path.expanduser(r'~\.ssh\id_ed25519')


def load_env(path=LOCAL_ENV):
    env = {}
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip().strip('"')
    return env


def vps_connect(env):
    import paramiko
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(env.get('VPS_TAILSCALE_IP', '100.109.206.100'),
                username='root', password=env['VPS_SSH_PASSWORD'], timeout=20)
    return ssh


def vps_run(ssh, cmd, timeout=90):
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode('utf-8', 'replace')
    err = stderr.read().decode('utf-8', 'replace')
    code = stdout.channel.recv_exit_status()
    return code, out, err


def vps_put(ssh, local_path, remote_path, mode='644'):
    raw = io.open(local_path, 'rb').read()
    b64 = base64.b64encode(raw).decode('ascii')
    # base64 传输: 零引号转义问题
    cmd = "echo '%s' | base64 -d > %s && chmod %s %s" % (b64, remote_path, mode, remote_path)
    return vps_run(ssh, cmd)


def deploy_vps(env):
    print('=== 部署 VPS 侧 ===')
    ssh = vps_connect(env)
    vps_run(ssh, 'mkdir -p /opt/control-station/queue')

    for name in ['server.py', 'worker.py', 'net.py']:
        local = os.path.join(REPO, 'orchestrator', name)
        remote = '/opt/control-station/' + name
        code, _, err = vps_put(ssh, local, remote, '755')
        print('  %-12s -> %s  %s' % (name, remote, 'OK' if code == 0 else 'ERR ' + err[:80]))

    # CS_TOKEN: env 优先; 没有就让 VPS 自动生成 (落盘 token.txt, 重启不变)
    cs_token = (env.get('CS_TOKEN') or '').strip()
    token_line = 'Environment=CS_TOKEN=%s' % cs_token if cs_token else \
                 '# CS_TOKEN not set - server will generate one into queue/token.txt'

    unit = '''[Unit]
Description=control-station orchestrator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/control-station
Environment=CS_PORT=9000
Environment=CS_BIND=0.0.0.0
%s
ExecStart=/usr/bin/python3 /opt/control-station/server.py
Restart=always
RestartSec=5
StandardOutput=append:/opt/control-station/orchestrator.log
StandardError=append:/opt/control-station/orchestrator.err

[Install]
WantedBy=multi-user.target
''' % token_line
    b64 = base64.b64encode(unit.encode('utf-8')).decode('ascii')
    vps_run(ssh, "echo '%s' | base64 -d > /etc/systemd/system/control-station.service" % b64)
    vps_run(ssh, 'systemctl daemon-reload && systemctl enable control-station 2>&1 | tail -1')
    vps_run(ssh, 'systemctl restart control-station')
    import time
    time.sleep(5)
    code, out, _ = vps_run(ssh, 'systemctl is-active control-station')
    print('  systemd:', out.strip())
    if not cs_token:
        code, out, _ = vps_run(ssh, 'cat /opt/control-station/queue/token.txt 2>/dev/null')
        if out.strip():
            print('  auto-generated token (copy to PC .env as CS_TOKEN):')
            print('    ' + out.strip())
    ssh.close()
    print('  VPS 部署完成')


def deploy_pc(env):
    print('=== 部署 PC 侧 ===')
    os.makedirs(DSH_DIR, exist_ok=True)

    # supervisor 脚本
    for src, dst in [
        (os.path.join(REPO, 'pc/dsh/dsh-supervisor.ps1'), DSH_DIR + r'\dsh-supervisor.ps1'),
        (os.path.join(REPO, 'pc/mcp/u2mcp-supervisor.ps1'), DSH_DIR + r'\u2mcp-supervisor.ps1'),
    ]:
        if os.path.exists(src):
            io.open(dst, 'w', encoding='utf-8', newline='\n').write(
                io.open(src, encoding='utf-8').read())
            print('  + ' + dst)

    # 计划任务 (schtasks XML, 绕开 Register-ScheduledTask 的 Access denied)
    for task_name, xml_rel in [
            ('U2MCP-Supervisor', 'pc/dsh/u2mcp-task.xml'),
            ('CS-Worker', 'pc/scripts/worker-task.xml')]:
        xml_src = os.path.join(REPO, xml_rel.replace('/', os.sep))
        if not os.path.exists(xml_src):
            continue
        dst = DSH_DIR + '\\' + os.path.basename(xml_rel)
        io.open(dst, 'w', encoding='utf-8', newline='\n').write(
            io.open(xml_src, encoding='utf-8').read())
        r = subprocess.run(['schtasks', '/Create', '/TN', task_name,
                            '/XML', dst, '/F'], capture_output=True, text=True)
        ok = 'SUCCESS' in (r.stdout or '') or (r.returncode == 0)
        print('  [%s] %s task' % ('OK ' if ok else 'ERR', task_name))

    # 启动 worker (交给计划任务 CS-Worker 管, 不手动起)
    subprocess.run(['powershell', '-NoProfile', '-Command', r'''Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*orchestrator\worker.py*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Seconds 3
schtasks /Run /TN "CS-Worker"'''], capture_output=True)
    print('  + CS-Worker 任务已启动')

    # 重启 DSH supervisor (加载 env 版)
    subprocess.run(['powershell', '-NoProfile', '-Command', r'''Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object { $_.CommandLine -match "dsh-supervisor" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Seconds 3
Start-ScheduledTask -TaskName "DSH-Supervisor"'''], capture_output=True)
    print('  + DSH-Supervisor 已重启')
    print('  PC 部署完成 (DSH 冷启动需 40-120s)')


def check(env):
    print('=== 健康检查 ===')
    ok = True

    # 端口存活用 TCP 探测 (HTTP 状态码对根路径没意义: DSH 根返回 303,
    # Caddy 无 key 返回 401 —— 两者都是"活着"的正确表现)
    # 注意绑定地址不同: DSH Web 绑 Tailscale IP, 网关绑 127.0.0.1
    import socket
    host_ip = env.get('DSH_HOST_IP', '100.117.110.112')
    for name, port, host in [
            ('DSH Web', 3080, host_ip),
            ('DSH 网关', 8326, '127.0.0.1'),
            ('Caddy Web', 3081, '127.0.0.1'),
            ('Caddy 网关', 8327, '127.0.0.1'),
            ('u2mcp', 8765, '127.0.0.1')]:
        s = socket.socket()
        s.settimeout(3)
        try:
            s.connect((host, port))
            mark = 'OK '
        except Exception:
            mark = 'DOWN'
            ok = False
        finally:
            s.close()
        print('  [%s] %-14s %s:%d' % (mark, name, host, port))

    st, body = net.get_json('http://%s:%s/health' % (
        env.get('VPS_TAILSCALE_IP', '100.109.206.100'), env.get('CS_PORT', '9000')), timeout=10)
    mark = 'OK ' if st == 200 else 'DOWN'
    if mark == 'DOWN':
        ok = False
    print('  [%s] VPS 编排器   %s' % (mark, body[:70]))

    # 编排器鉴权验证 (带 token 应 200, 不带应 401)
    cs_token = env.get('CS_TOKEN', '')
    if cs_token:
        st_auth, _ = net.get_json('http://%s:%s/jobs' % (
            env.get('VPS_TAILSCALE_IP', '100.109.206.100'), env.get('CS_PORT', '9000')),
            timeout=10, headers={'Authorization': 'Bearer ' + cs_token})
        st_noauth, _ = net.get_json('http://%s:%s/jobs' % (
            env.get('VPS_TAILSCALE_IP', '100.109.206.100'), env.get('CS_PORT', '9000')),
            timeout=10)
        if st_auth == 200 and st_noauth == 401:
            print('  [OK ] 编排器鉴权   有token=200 / 无token=401')
        else:
            print('  [?? ] 编排器鉴权   有token=%s / 无token=%s (期望 200/401)'
                  % (st_auth, st_noauth))

    # 网关模型: 必须带 shell key (Caddy 层拦截, 不带就是 401 —— 那是安全的)
    shell_key = env.get('DSH_SHELL_KEY', '')
    st, body = net.get_json('http://127.0.0.1:8327/v1/models', timeout=10,
                             headers={'Authorization': 'Bearer ' + shell_key})
    if st == 200:
        try:
            n = len(json.loads(body)['data'])
            print('  [OK ] 网关模型     %d 个' % n)
        except Exception:
            print('  [?? ] 网关模型     解析失败')
    else:
        ok = False
        print('  [DOWN] 网关模型     http %s (key 可能不对)' % st)

    print()
    print('整体: %s' % ('ALL GREEN' if ok else 'SOME DOWN'))
    return 0 if ok else 1


def main():
    env = load_env()
    action = sys.argv[1] if len(sys.argv) > 1 else 'check'
    if action == 'vps':
        deploy_vps(env)
    elif action == 'pc':
        deploy_pc(env)
    elif action == 'all':
        deploy_vps(env)
        print()
        deploy_pc(env)
    elif action == 'check':
        sys.exit(check(env))
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == '__main__':
    main()
