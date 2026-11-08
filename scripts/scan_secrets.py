#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/scan_secrets.py —— 入库前强制密钥扫描 (门禁)。

高置信度规则 + 已知安全常量白名单。0 命中才算通过。
退出码: 0 = CLEAN, 1 = 有泄漏
"""
import io
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RULES = [
    ('NVIDIA NIM key', re.compile(r'nvapi-[A-Za-z0-9_\-]{20,}')),
    ('DeepSeek/OpenAI key', re.compile(r'\bsk-[A-Za-z0-9]{20,}')),
    ('Anthropic key', re.compile(r'\bsk-ant-[A-Za-z0-9_\-]{20,}')),
    ('JWT', re.compile(r'\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}')),
    ('GitHub PAT', re.compile(r'\bgh[pousr]_[A-Za-z0-9]{20,}')),
    ('AWS key', re.compile(r'\bAKIA[0-9A-Z]{16}\b')),
    ('VPS password', re.compile(r'\bqet5eG1Acg\b')),
    ('Hysteria2 pass', re.compile(r'\bB6avZNl4i9Q46AkHuV9qOg==')),
    ('TUIC password', re.compile(r'\b1kCAqEDM65yxnDOBmJHDug==')),
    ('Caddy gateway key', re.compile(r'ofm-[A-Za-z0-9_\-]{20,}')),
    ('Reality private key', re.compile(r'\bECEbwOuA2oeHiZ4mDHUICztbRsFjfdU9Y4P4wtAiqV4\b')),
    ('Reality public key', re.compile(r'\buIJAngLbccDZAUM3ptt5VYKLIbmtRb8e4aWXUr-ebXU\b')),
    ('Reality uuid', re.compile(r'\baa45d12b-2863-49f9-84c4-214f509d3c67\b')),
]

# 设计上的公开常量, 不是真密钥
ALLOW = {
    'u2mcp-mobile-2026',      # MCP bearer (写死在配置里, 非敏感)
    'LOBE-2026-SHELL',        # 网关 shell key (公开的对外面具)
}

hits = []
scanned = 0
for root, dirs, files in os.walk(REPO):
    dirs[:] = [d for d in dirs if d not in ('.git', '__pycache__', 'queue')]
    for f in files:
        p = os.path.join(root, f)
        try:
            raw = io.open(p, 'rb').read().decode('utf-8', 'replace')
        except Exception:
            continue
        scanned += 1
        rel = os.path.relpath(p, REPO)
        for name, pat in RULES:
            for m in pat.finditer(raw):
                val = m.group(0)
                if val in ALLOW:
                    continue
                ln = raw[:m.start()].count('\n') + 1
                hits.append((rel, ln, name, val[:14] + '...'))

print('scanned files: %d' % scanned)
print()
if not hits:
    print('*** CLEAN: no real secrets found in repo ***')
    sys.exit(0)
print('!!! %d SECRET HIT(S) !!!' % len(hits))
for rel, ln, name, val in hits:
    print('  %-46s line %-5s %-20s %s' % (rel, ln, name, val))
sys.exit(1)
