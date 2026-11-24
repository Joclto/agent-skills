# -*- coding: utf-8 -*-
"""
feed_sessions.py v2 — 把本机 Hermes 完整会话历史(state.db)投喂给中央 Hindsight 记忆库
用法（各设备均可，API 指向中央服务）:
    python feed_sessions.py http://<host xEdge IP>:8888
仅标准库；SQLite 以只读模式打开，Hermes 运行中也可安全执行。
"""
import json
import os
import sqlite3
import sys
import time
import urllib.request

MAX_MSG_CHARS = 3000
MAX_DOC_CHARS = 80000
SKIP_PREFIX = ('<system-reminder>', '[OUT-OF-BAND', '[CONTEXT COMPACTION')

def db_path():
    base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~/.local/share')
    return os.path.join(base, 'hermes', 'state.db')

def load_sessions():
    p = db_path()
    if not os.path.exists(p):
        raise SystemExit(f'未找到 state.db: {p}')
    con = sqlite3.connect(f'file:{p}?mode=ro', uri=True)
    rows = con.execute(
        "SELECT session_id, role, content, timestamp FROM messages "
        "WHERE role IN ('user','assistant') ORDER BY id").fetchall()
    con.close()
    sess = {}
    for sid, role, content, ts in rows:
        if not content or not isinstance(content, str):
            continue
        c = content.strip()
        if not c or c.startswith(SKIP_PREFIX):
            continue
        if len(c) > MAX_MSG_CHARS:
            c = c[:MAX_MSG_CHARS] + f'\n…[截断，原长 {len(c)} 字]'
        sess.setdefault(sid, []).append((role, c, ts))
    return sess

def main():
    api = (sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:8888').rstrip('/')
    host = os.environ.get('COMPUTERNAME', 'device')
    sess = load_sessions()
    print(f'共 {len(sess)} 个会话')
    ok = fail = skip = 0
    for sid, msgs in sorted(sess.items()):
        if len(msgs) < 2:
            skip += 1
            continue
        text = '\n\n'.join(('用户: ' if r == 'user' else '助理: ') + c for r, c, _ in msgs)
        date = str(msgs[0][2] or '')[:10].replace(':', '').replace('-', '') or 'unknown'
        if len(text) > MAX_DOC_CHARS:
            text = text[:MAX_DOC_CHARS] + f'\n…[文档截断，原长 {len(text)} 字]'
        tag = f'{host}:{sid[6:14]}:{date}' if sid.startswith('local-') else f'{host}:{sid[:8]}:{date}'
        body = json.dumps({
            'items': [{'content': text, 'context': f'历史会话完整记录 {tag}', 'tags': ['历史会话', host]}],
            'async': True,
        }).encode('utf-8')
        req = urllib.request.Request(f'{api}/v1/default/banks/hermes/memories', data=body, method='POST')
        req.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                json.loads(r.read().decode(errors='replace'))
            print(f'  已投喂 {tag}（{len(msgs)} 条消息 / {len(text)} 字）')
            ok += 1
        except Exception as e:
            print(f'  失败 {tag}: {e}')
            fail += 1
        time.sleep(2)
    print(f'完成: 投喂 {ok} · 跳过 {skip} · 失败 {fail}。服务端后台慢慢提炼，官方 UI「操作」页可看进度。')

if __name__ == '__main__':
    main()
