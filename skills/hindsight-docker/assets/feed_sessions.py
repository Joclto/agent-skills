# -*- coding: utf-8 -*-
r"""
feed_sessions.py v3 — 把本机 Hermes 会话历史(state.db)回灌给中央 Hindsight 记忆库

用法（在【要回灌的那台机器】上运行，API 指向中央服务）:
    python feed_sessions.py http://<host xEdge IP>:8888 --dry-run              # 只打印计划，不发请求
    python feed_sessions.py http://<host xEdge IP>:8888                       # 投喂
    python feed_sessions.py http://<host xEdge IP>:8888 --before 2026-11-23   # 只投 11-23 之前的历史
没有系统 python 时用 Hermes 自带的：%LOCALAPPDATA%\hermes\tools\python*\python.exe
仅标准库；state.db 以只读方式打开，Hermes 运行中也可以跑。

相对 v2 的变化：
  1. 长会话按 8 万字在消息边界分片，各片是独立文档。v2 整篇截到 8 万字，长会话只进了开头。
  2. 每片带固定 document_id（backfill-<主机名>-<会话id>-p<序号>）。服务端的增量写入只在同一个
     document_id 内按块 content_hash 去重，不跨文档：
       - 原样重跑、中断后重跑：0 新增、0 token（2026-11-24 临时库实测）；
       - 会话后来又有新消息：只有末尾变化的那一片被重提炼。
     v2 不带 id，服务端每次都新建文档并全量提取 —— 重跑必重复。
     同一台机器 v2、v3 不要混跑：两套 id，会各进一份。
  3. 主机名（COMPUTERNAME）写进文档 id、context、tags，后台可以按机器区分来源。
     所以要在源机器上跑；把 state.db 拷到别的机器代跑时，用 --host 指定真实主机名。
  4. --before YYYY-MM-DD 或 YYYY-MM-DDTHH:MM：只投该时刻之前的消息。已经在实时镜像的机器必须填
     它在中央库出现第一条记忆的时刻（主机上查，见技能文档），填到分：
       - 不填：接入之后的会话会「实时镜像一份 + 回灌一份」；
       - 只填到天：接入当天零点到接入时刻之间既没回灌也没镜像，会丢掉这段。

投喂内容：user/assistant 正文。滤掉压缩摘要（三种包装）、后台进程结束通知、失败轮的套话；
用户中途插话（被 [OUT-OF-BAND] 标记包裹）去掉包装、保留原话。
"""
import argparse
import collections
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BANK = 'hermes'
MAX_MSG_CHARS = 3000      # 单条消息上限，与 retain_chunk_size 对齐
MAX_DOC_CHARS = 80000     # 每片上限。v2 也是这个数，但 v2 是截断，这里是分片
NOISE_PREFIX = ('<system-reminder>', '[IMPORTANT: Background process',            # 后台进程结束通知
                'Your request was not processed.', 'This turn did not complete.')  # 失败轮的套话
NOISE_KIND = ('process_complete', 'failed_turn')                 # 上面两类在 messages.display_kind 里的值
SUMMARY_HEAD = '[CONTEXT COMPACTION'                              # 压缩摘要 = 同一段对话的重复
SUMMARY_WRAP = '[PRIOR CONTEXT — for reference only; not a new message]'     # 摘要的另一种包装头
SUMMARY_MARK = '\n[END OF PRIOR CONTEXT — COMPACTION SUMMARY BELOW]'        # 真实回复后面接摘要的位置
OOB_HEAD, OOB_TAIL = '[OUT-OF-BAND USER MESSAGE', '[/OUT-OF-BAND USER MESSAGE]'


def db_path():
    base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~/.local/share')
    return os.path.join(base, 'hermes', 'state.db')


def _epoch(ts):
    """消息时间戳 -> unix 秒；认不出来返回 None。"""
    try:
        return float(ts)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(ts)[:19]).timestamp()
        except ValueError:
            return None


def _keep(content, kind, stat):
    """返回要投的正文；不该投的返回 None，并在 stat 里记一笔。"""
    if not content or not isinstance(content, str) or not content.strip():
        stat['空内容'] += 1
        return None
    c = content.strip()
    if c.startswith(OOB_HEAD):                        # 用户中途插话：去掉包装，保留原话
        c = c.split('\n', 1)[1].strip() if '\n' in c else ''
        if c.endswith(OOB_TAIL):
            c = c[:-len(OOB_TAIL)].rstrip()
    if kind in NOISE_KIND or c.startswith(NOISE_PREFIX):
        stat['系统通知/套话'] += 1
        return None
    if c.startswith(SUMMARY_HEAD):
        stat['压缩摘要'] += 1
        return None
    if c.startswith(SUMMARY_WRAP):                    # 摘要的另一种存法：包装头 + （真实回复 +）摘要本体
        c = ('\n' + c[len(SUMMARY_WRAP):].lstrip('\n')).partition(SUMMARY_MARK)[0].lstrip('\n')
    i = c.find(SUMMARY_MARK)
    if i != -1:
        c = c[:i].rstrip()
    if not c:
        stat['压缩摘要'] += 1
        return None
    if len(c) > MAX_MSG_CHARS:
        stat['单条截断'] += 1
        c = c[:MAX_MSG_CHARS] + f'\n…[截断，原长 {len(c)} 字]'
    return c


def load_sessions(p, before_epoch=None):
    """-> ({session_id: [(role, content, ts), ...]}, 消息统计)。只含 user/assistant 正文，按写入顺序。"""
    if not os.path.exists(p):
        raise SystemExit(f'未找到 state.db: {p}')
    stat = collections.Counter()
    con = sqlite3.connect(Path(p).as_uri() + '?mode=ro', uri=True)
    cols = {r[1] for r in con.execute('PRAGMA table_info(messages)')}
    kind_col = 'display_kind' if 'display_kind' in cols else 'NULL'   # 旧版 Hermes 没有这一列
    rows = con.execute(f'SELECT session_id, role, content, timestamp, {kind_col} FROM messages '
                       "WHERE role IN ('user','assistant') ORDER BY id").fetchall()
    con.close()
    sess = {}
    for sid, role, content, ts, kind in rows:
        if before_epoch is not None:
            ep = _epoch(ts)
            if ep is not None and ep >= before_epoch:
                stat['截止日期之后（由实时镜像负责）'] += 1
                continue
        c = _keep(content, kind, stat)
        if c is not None:
            sess.setdefault(sid or 'nosession', []).append((role, c, ts))
    stat['保留'] = sum(len(v) for v in sess.values())
    return sess, stat


def session_date(ts):
    """会话首条消息的日期 YYYY-MM-DD。timestamp 是 unix 秒或 ISO 串。"""
    ep = _epoch(ts)
    try:
        return datetime.fromtimestamp(ep).strftime('%Y-%m-%d')
    except (TypeError, OSError, OverflowError):   # ep 为 None 或离谱
        return str(ts)[:10] or 'unknown'


def split_parts(msgs):
    """把一个会话的消息按 MAX_DOC_CHARS 在消息边界分片。

    从头贪心装填，前面的片不随会话变长而变化：新消息只会让最后一片变长，或另起一片。
    """
    parts, buf, size = [], [], 0
    for role, content, _ in msgs:
        line = ('用户: ' if role == 'user' else '助理: ') + content
        if buf and size + 2 + len(line) > MAX_DOC_CHARS:
            parts.append('\n\n'.join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line) + (2 if size else 0)
    if buf:
        parts.append('\n\n'.join(buf))
    return parts


def plan_items(sess, host):
    """-> [(document_id, context, text, sid_key)]，以及因消息不足被跳过的会话数。"""
    items, skipped = [], 0
    for sid, msgs in sorted(sess.items()):
        if len(msgs) < 2:
            skipped += 1
            continue
        if sid.startswith('local-'):
            sid_key = sid[6:14]
        else:
            sid_key = sid[:15]            # 定时任务等一次性会话：日期 + 时分秒
        tag = f'{host}:{sid_key}:{session_date(msgs[0][2])}'
        stem = re.sub(r'[^A-Za-z0-9_-]', '_', f'backfill-{host}-{sid}')
        parts = split_parts(msgs)
        for i, text in enumerate(parts, 1):
            suffix = f' · 分片 {i}/{len(parts)}' if len(parts) > 1 else ''
            items.append((f'{stem}-p{i:02d}', f'历史会话完整记录 {tag}{suffix}', text, sid_key))
    return items, skipped


def post(api, host, doc_id, context, text):
    """投一片，返回服务端的 operation_id（提取在服务端排队进行）。"""
    body = json.dumps({
        'items': [{'content': text, 'context': context, 'tags': ['历史会话', host], 'document_id': doc_id}],
        'async': True,
    }).encode('utf-8')
    req = urllib.request.Request(f'{api}/v1/default/banks/{BANK}/memories', data=body, method='POST')
    req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read() or b'{}')
    return str(data.get('operation_id') or '')


def main():
    ap = argparse.ArgumentParser(description='把本机 Hermes 会话历史回灌给中央 Hindsight 记忆库')
    ap.add_argument('api', nargs='?', default='http://127.0.0.1:8888', help='中央 Hindsight API 地址')
    ap.add_argument('--host', default=os.environ.get('COMPUTERNAME', 'device'),
                    help='源机器主机名（默认取环境变量 COMPUTERNAME）')
    ap.add_argument('--db', default=None, help='state.db 路径（默认 %%LOCALAPPDATA%%\\hermes\\state.db）')
    ap.add_argument('--before', default=None, metavar='YYYY-MM-DD[T]HH:MM',
                    help='只投该时刻之前的消息：YYYY-MM-DD 表示当天零点，也可以精确到分。'
                         '已接入实时镜像的机器填它在中央库出现第一条记忆的时刻')
    ap.add_argument('--dry-run', action='store_true', help='只打印投喂计划，不发请求')
    args = ap.parse_args()
    api = args.api.rstrip('/')

    before_epoch = None
    if args.before:
        try:
            before_epoch = datetime.fromisoformat(args.before).timestamp()   # YYYY-MM-DD 或 YYYY-MM-DDTHH:MM，按本机时区
        except ValueError:
            ap.error('--before 格式应为 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM')

    sess, stat = load_sessions(str(Path(args.db or db_path()).resolve()), before_epoch)
    items, skipped = plan_items(sess, args.host)
    total_chars = sum(len(t) for _, _, t, _ in items)
    print(f'源机器 {args.host}：{len(sess)} 个会话，切成 {len(items)} 片，共 {total_chars} 字；'
          f'消息不足跳过 {skipped} 个会话' + (f'；只投 {args.before} 之前的历史' if args.before else ''))
    print('消息：' + '，'.join(f'{k} {v}' for k, v in stat.most_common()))
    for doc_id, context, text, _ in items:
        print(f'  {doc_id}（{len(text)} 字）')
    if args.dry_run:
        print('dry-run：未发送任何请求')
        return

    ok = fail = 0
    for doc_id, context, text, _ in items:
        try:
            op = post(api, args.host, doc_id, context, text)
            print(f'已投 {doc_id}（操作 {op or "?"}）')
            ok += 1
        except urllib.error.HTTPError as e:
            print(f'失败 {doc_id}: HTTP {e.code} {e.read().decode(errors="replace")[:200]}')
            fail += 1
        except Exception as e:
            print(f'失败 {doc_id}: {e}')
            fail += 1
        time.sleep(1)
    print(f'完成: 投喂 {ok} 片 · 失败 {fail}。服务端后台提炼，官方 UI「操作」页可看进度。')
    print('失败的片直接重跑本脚本即可；已投过且内容没变的片不会重复入库。')
    sys.exit(1 if fail else 0)


if __name__ == '__main__':
    sys.stdout.reconfigure(errors='replace')  # 别的机器控制台编码不确定，别让打印中断投喂
    main()
