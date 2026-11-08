"""
net.py —— 共享网络层。

关键点: 本机装了 v2rayN, 系统环境有 HTTP_PROXY/HTTPS_PROXY 指向 127.0.0.1:10808。
Tailscale 内网地址 (100.x / 192.168.x) 必须直连, 走代理会被丢给代理服务器 -> 502。

所以: 任何要访问内网的脚本, 必须先 import 这个模块 (它会在 import 时清掉代理)。
"""
import os
import json
import urllib.error
import urllib.request

# 清掉系统代理 (必须在任何 urllib 请求之前)
for _k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy',
           'ALL_PROXY', 'all_proxy'):
    os.environ.pop(_k, None)
os.environ['NO_PROXY'] = '*'
os.environ['no_proxy'] = '*'


def post_json(url, payload, timeout=60, headers=None):
    h = {'Content-Type': 'application/json'}
    if headers:
        h.update(headers)
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=h,
                                 method='POST' if payload is not None else 'GET')
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')[:500]
    except Exception as e:
        return 0, str(e)[:300]


def get_json(url, timeout=30, headers=None):
    h = {}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h, method='GET')
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')[:500]
    except Exception as e:
        return 0, str(e)[:300]
