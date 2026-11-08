#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
orchestrator/worker.py —— 中控站执行器 (PC 侧)

轮询 VPS 编排器领任务 -> 调本地 DSH 网关 (:8326) 调度模型执行 -> 回报结果。

零外部依赖 (只用标准库)。

用法:
  python worker.py --server 100.109.206.100:9000 --interval 5
  python worker.py --once          # 只跑一轮 (测试用)
"""
import argparse
import json
import os
import sys
import time

# 计划任务下 stdout 被 cmd 重定向到文件, Windows 默认给 GBK 句柄,
# reconfigure 对文件句柄无效 -> 中文日志乱码。
# 可靠解法: 所有日志先转义成 ASCII-safe 的 \\uXXXX, 写出去就不会乱码。
def log(*args, **kwargs):
    """安全打印: 非 ASCII 转义, 保证任何句柄/编码下都不乱码"""
    try:
        text = ' '.join(str(a) for a in args)
        text = text.encode('unicode_escape').decode('ascii')
        print(text, flush=kwargs.get('flush', True))
    except Exception:
        try:
            print(' '.join(str(a) for a in args).encode('ascii', 'backslashreplace').decode('ascii'),
                  flush=kwargs.get('flush', True))
        except Exception:
            pass


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import net  # noqa: E402  (import 时清掉系统代理)
post_json = net.post_json
get_json = net.get_json

BASE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_ENV = r'D:\000\dsh\.env'


def load_env(path=DEFAULT_ENV):
    """从 .env 读配置 (不入库)"""
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


def http_json(url, payload=None, timeout=180, headers=None):
    """兼容包装: 走 net 模块 (import 时已清掉系统代理)"""
    if payload is None:
        return get_json(url, timeout=timeout)
    return post_json(url, payload, timeout=timeout, headers=headers)


class Worker:
    def __init__(self, server, dsh_url, dsh_key, name='pc-worker'):
        self.server = server.rstrip('/')
        self.dsh_url = dsh_url.rstrip('/')
        self.dsh_key = dsh_key
        self.name = name
        self.stats = {'claimed': 0, 'done': 0, 'failed': 0}

    def claim(self):
        url = self.server + '/claim'
        st, body = http_json(url, {'worker': self.name}, timeout=30)
        if st == 204:
            return None
        if st != 200:
            log('[claim] http %s %s' % (st, body[:150]))
            return None
        try:
            return json.loads(body)
        except Exception:
            return None

    def execute(self, job):
        """调 DSH 网关调度模型执行"""
        model = job.get('model') or 'nvidia_nim/stepfun-ai/step-5-preview'
        prompt = job.get('prompt')
        payload = {
            'model': model,
            'messages': [{'role': 'user', 'content': prompt}],
            'stream': False,
        }
        headers = {'Authorization': 'Bearer ' + self.dsh_key}
        st, body = http_json(self.dsh_url + '/v1/chat/completions',
                             payload, timeout=300, headers=headers)
        if st != 200:
            return 'failed', None, 'gateway http %s: %s' % (st, body[:300])
        try:
            j = json.loads(body)
            content = j['choices'][0]['message'].get('content', '')
            usage = j.get('usage', {})
            return 'done', {'content': content, 'usage': usage,
                            'model': model}, None
        except Exception as e:
            return 'failed', None, 'parse error: %s' % e

    def report(self, job_id, status, result=None, error=None):
        payload = {'job_id': job_id, 'status': status,
                   'result': result, 'error': error}
        st, body = http_json(self.server + '/result', payload, timeout=30)
        if st != 200:
            log('[report] http %s %s' % (st, body[:150]))

    def run_once(self):
        job = self.claim()
        if not job:
            return False
        jid = job.get('job_id')
        self.stats['claimed'] += 1
        log('[claim] %s  model=%s  prompt=%r' % (
            jid, job.get('model'), str(job.get('prompt'))[:80]), flush=True)
        status, result, error = self.execute(job)
        self.report(jid, status, result, error)
        if status == 'done':
            self.stats['done'] += 1
            log('[done ] %s  -> %r' % (jid, str(result.get('content'))[:120]))
        else:
            self.stats['failed'] += 1
            log('[fail ] %s  -> %s' % (jid, error))
        return True


def main():
    env = load_env()
    # 把 .env 里的值写进 os.environ, 让 argparse 的 default 能取到
    for k, v in env.items():
        os.environ.setdefault(k, v)
    ap = argparse.ArgumentParser()
    ap.add_argument('--server', default=os.environ.get('CS_SERVER',
                    'http://%s:%s' % (env.get('VPS_TAILSCALE_IP', '100.109.206.100'),
                                      env.get('CS_PORT', '9000'))))
    ap.add_argument('--dsh', default=os.environ.get('DSH_GATEWAY_URL',
                    'http://127.0.0.1:%s' % env.get('DSH_GATEWAY_PORT', '8326')))
    ap.add_argument('--key', default=os.environ.get('DSH_GATEWAY_KEY', ''))
    ap.add_argument('--interval', type=int, default=5)
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--name', default='pc-worker')
    a = ap.parse_args()

    if not a.key:
        print('ERROR: DSH gateway key missing (check %s)' % DEFAULT_ENV, file=sys.stderr)
        sys.exit(2)

    w = Worker(a.server, a.dsh, a.key, a.name)
    log('worker up: server=%s  dsh=%s' % (a.server, a.dsh))
    if a.once:
        w.run_once()
        log('stats:', w.stats)
        return
    log('polling every %ds (Ctrl-C to stop)' % a.interval)
    try:
        while True:
            try:
                w.run_once()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log('[loop] %s' % e)
            time.sleep(a.interval)
    except KeyboardInterrupt:
        log('\nbye  stats=%s' % w.stats)


if __name__ == '__main__':
    main()
