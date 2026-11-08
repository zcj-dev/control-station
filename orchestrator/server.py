#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
orchestrator/server.py —— 中控站指令编排器 (VPS 侧)

零外部依赖 (只用标准库), 直接 `python3 server.py` 就能跑。

安全:
  - Bearer token 鉴权 (CS_TOKEN)。未配置时自动生成随机 token 并落盘,
    只打印一次, 写入 state 文件供 worker/运维取用。
  - /health 无鉴权但只返回最简信息 (不泄露 job 数量/内容)。
  - 配合 VPS 的 UFW (9000 未放行) 形成双保险: 内网可达 + 必须持 token。

职责:
  1. 接收指令 (POST /submit)
  2. 落盘队列 (JSONL, 防丢)
  3. 轮询执行器 (PC 侧 worker) 来拉取并回报结果
  4. 状态查询 (GET /status/<job_id>)
"""
import http.server
import json
import os
import secrets
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
QUEUE_DIR = os.path.join(BASE, 'queue')
PENDING = os.path.join(QUEUE_DIR, 'pending.jsonl')
RESULTS = os.path.join(QUEUE_DIR, 'results.jsonl')
STATE = os.path.join(QUEUE_DIR, 'state.json')
TOKEN_FILE = os.path.join(QUEUE_DIR, 'token.txt')

os.makedirs(QUEUE_DIR, exist_ok=True)


def load_or_make_token():
    """CS_TOKEN 优先; 否则用落盘的随机 token (重启不变)"""
    env_token = (os.environ.get('CS_TOKEN') or '').strip()
    if env_token:
        return env_token, 'env'
    if os.path.exists(TOKEN_FILE):
        try:
            t = io_read(TOKEN_FILE).strip()
            if t:
                return t, 'file'
        except Exception:
            pass
    t = secrets.token_urlsafe(32)
    try:
        with open(TOKEN_FILE, 'w', encoding='utf-8') as f:
            f.write(t)
        os.chmod(TOKEN_FILE, 0o600)
    except Exception:
        pass
    return t, 'generated'


def io_read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


TOKEN, TOKEN_SOURCE = load_or_make_token()

_lock = threading.Lock()
_state = {}
if os.path.exists(STATE):
    try:
        _state = json.load(open(STATE, encoding='utf-8'))
    except Exception:
        _state = {}


def _save_state():
    tmp = STATE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(_state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def _append(path, obj):
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(obj, ensure_ascii=False) + '\n')
        f.flush()
        os.fsync(f.fileno())


class Handler(BaseHTTPRequestHandler):
    server_version = 'control-station/1.1'

    def log_message(self, fmt, *args):
        try:
            msg = fmt % args
        except Exception:
            msg = str(args)
        print('[%s] %s' % (time.strftime('%H:%M:%S'), msg), flush=True)

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        """校验 Bearer token"""
        h = self.headers.get('Authorization', '') or ''
        if not h.startswith('Bearer '):
            return False
        got = h[7:].strip()
        # 常量时间比较, 防时序侧信道
        return secrets.compare_digest(got, TOKEN)

    def do_GET(self):
        path = self.path.split('?')[0].rstrip('/')

        # /health 无鉴权, 但只返回最简信息 (不泄露 jobs 数量)
        if path in ('', '/health'):
            self._json(200, {'ok': True, 'service': 'control-station'})
            return

        if not self._authed():
            self._json(401, {'error': 'unauthorized', 'hint': 'Authorization: Bearer <CS_TOKEN>'})
            return

        if path.startswith('/status/'):
            jid = path.split('/status/', 1)[1]
            with _lock:
                st = _state.get(jid)
            if not st:
                self._json(404, {'error': 'unknown job', 'job_id': jid})
            else:
                self._json(200, st)
        elif path == '/jobs':
            with _lock:
                items = sorted(_state.values(), key=lambda x: x.get('created_at', 0),
                               reverse=True)[:50]
            self._json(200, {'jobs': items})
        else:
            self._json(404, {'error': 'not found'})

    def do_POST(self):
        path = self.path.split('?')[0].rstrip('/')
        length = int(self.headers.get('Content-Length', 0) or 0)
        raw = self.rfile.read(length) if length else b'{}'
        try:
            payload = json.loads(raw.decode('utf-8'))
        except Exception as e:
            self._json(400, {'error': 'bad json', 'detail': str(e)})
            return

        if not self._authed():
            self._json(401, {'error': 'unauthorized', 'hint': 'Authorization: Bearer <CS_TOKEN>'})
            return

        if path == '/submit':
            jid = payload.get('job_id') or ('job-' + uuid.uuid4().hex[:12])
            prompt = payload.get('prompt') or payload.get('instruction') or ''
            model = payload.get('model') or 'nvidia_nim/stepfun-ai/step-5-preview'
            if not prompt:
                self._json(400, {'error': 'prompt required'})
                return
            job = {
                'job_id': jid,
                'prompt': prompt,
                'model': model,
                'status': 'queued',
                'created_at': time.time(),
                'created_at_h': time.strftime('%Y-%m-%d %H:%M:%S'),
                'source': payload.get('source') or self.client_address[0],
                'meta': payload.get('meta') or {},
            }
            with _lock:
                _state[jid] = job
                _save_state()
            _append(PENDING, job)
            self._json(202, {'ok': True, 'job_id': jid, 'status': 'queued'})

        elif path == '/claim':
            # PC worker 来领任务
            with _lock:
                jid = payload.get('job_id')
                if jid:
                    job = _state.get(jid)
                    if not job:
                        self._json(404, {'error': 'unknown job'})
                        return
                    if job.get('status') not in ('queued', 'claimed'):
                        self._json(409, {'error': 'job not claimable',
                                        'status': job.get('status')})
                        return
                    job['status'] = 'claimed'
                    job['claimed_at'] = time.time()
                    job['worker'] = payload.get('worker') or 'unknown'
                    _save_state()
                    self._json(200, job)
                    return
                # 领任意一个 queued
                for k, v in sorted(_state.items(), key=lambda x: x[1].get('created_at', 0)):
                    if v.get('status') == 'queued':
                        v['status'] = 'claimed'
                        v['claimed_at'] = time.time()
                        v['worker'] = payload.get('worker') or 'unknown'
                        _save_state()
                        self._json(200, v)
                        return
                self._json(204, {'ok': True, 'message': 'no queued job'})

        elif path == '/result':
            jid = payload.get('job_id')
            with _lock:
                job = _state.get(jid)
                if not job:
                    self._json(404, {'error': 'unknown job'})
                    return
                job['status'] = payload.get('status') or 'done'
                job['result'] = payload.get('result')
                job['error'] = payload.get('error')
                job['finished_at'] = time.time()
                job['finished_at_h'] = time.strftime('%Y-%m-%d %H:%M:%S')
                job['duration_s'] = round(time.time() - job.get('created_at', time.time()), 2)
                _save_state()
                snap = dict(job)
            _append(RESULTS, snap)
            self._json(200, {'ok': True, 'job_id': jid, 'status': snap['status']})
        else:
            self._json(404, {'error': 'not found'})


def main():
    port = int(os.environ.get('CS_PORT', '9000'))
    # 只绑 Tailscale 网卡更安全, 但 VPS 的 Tailscale IP 可能变;
    # 这里绑 0.0.0.0, 由 UFW (9000 未放行) + token 双保险保护。
    bind = os.environ.get('CS_BIND', '0.0.0.0')
    srv = ThreadingHTTPServer((bind, port), Handler)
    print('control-station orchestrator listening on %s:%d' % (bind, port), flush=True)
    print('  token source: %s' % TOKEN_SOURCE, flush=True)
    print('  auth: Authorization: Bearer <CS_TOKEN>', flush=True)
    print('  /health is public (minimal info only)', flush=True)
    print('  POST /submit   {"prompt": "...", "model": "..."}', flush=True)
    print('  POST /claim    {} (worker 领任务)', flush=True)
    print('  POST /result   {"job_id": "...", "status": "done", "result": "..."}', flush=True)
    print('  GET  /status/<job_id>', flush=True)
    print('  GET  /jobs     (最近 50 条)', flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\nbye', flush=True)


if __name__ == '__main__':
    main()
