#!/usr/bin/env python3
"""tracekit：验证与排查日志追踪（traceId）的小工具，只用 Python 标准库。

子命令
  probe     并发发请求，每个请求带一个唯一 traceId，记录到 sent.jsonl
  verify    检查每个发出去的 traceId 在日志里出现几行（少了=丢上下文，多了=串号）
  summary   统计有/没有 traceId 的日志行，按线程分组找出丢上下文的位置
  show      按顺序打印某个 traceId 的全部日志
  selftest  用合成日志自检本脚本

日志文件支持三种形态：普通文本行（如 logback 输出）、每行一个 JSON、
以及 `gcloud logging read --format=json` 导出的 JSON 数组。
"""
import argparse
import collections
import json
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HEX32 = r'[0-9a-fA-F]{32}'
UUID = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
TRACEPARENT_RE = re.compile(r'00-(' + HEX32 + r')-[0-9a-fA-F]{16}-[0-9a-fA-F]{2}')
ANY_ID_RE = re.compile(r'(?<![0-9a-fA-F-])(' + HEX32 + '|' + UUID + r')(?![0-9a-fA-F-])')
THREAD_RE = re.compile(r'\[([^\[\]]{1,80})\]')
JSON_TRACE_KEYS = ('logging.googleapis.com/trace', 'trace', 'traceId', 'trace_id', 'logId')


# ── 读日志 ────────────────────────────────────────────────────────────────

class Line:
    __slots__ = ('no', 'text', 'obj')

    def __init__(self, no, text, obj=None):
        self.no, self.text, self.obj = no, text, obj


def _entry_text(e):
    """把一条 Cloud Logging 条目压成一行可读文本。"""
    http = e.get('httpRequest') or {}
    payload = e.get('jsonPayload') or {}
    msg = e.get('textPayload') or payload.get('message') or http.get('requestUrl') or ''
    parts = [e.get('timestamp', ''), e.get('severity', ''), e.get('trace', '') or payload.get('logging.googleapis.com/trace', '')]
    if http:
        parts.append(f"{http.get('status', '')} {http.get('latency', '')}")
    uid = payload.get('user_id') or payload.get('uid')
    if uid:
        parts.append(f'user_id={uid}')
    parts.append(str(msg)[:400])
    return ' '.join(p for p in parts if p)


def load_lines(path):
    with open(path, encoding='utf-8', errors='replace') as f:
        raw = f.read()
    if raw.lstrip().startswith('['):
        entries = json.loads(raw)
        entries.sort(key=lambda e: e.get('timestamp', ''))
        return [Line(i + 1, _entry_text(e), e) for i, e in enumerate(entries)]
    out = []
    for i, text in enumerate(raw.splitlines()):
        if not text.strip():
            continue
        obj = None
        if text.lstrip().startswith('{'):
            try:
                obj = json.loads(text)
            except ValueError:
                obj = None
        out.append(Line(i + 1, text, obj))
    return out


def _norm(trace):
    """`projects/p/traces/<id>` → `<id>`；统一小写。"""
    if not trace:
        return None
    trace = str(trace)
    if '/traces/' in trace:
        trace = trace.rsplit('/traces/', 1)[1]
    m = TRACEPARENT_RE.search(trace)
    if m:
        return m.group(1).lower()
    return trace.strip().lower() or None


def trace_of(line, pattern=None):
    """取一行日志的 traceId；取不到返回 None。"""
    if pattern is not None:
        m = pattern.search(line.text)
        return _norm(m.group(1)) if m else None
    if isinstance(line.obj, dict):
        payload = line.obj.get('jsonPayload') if isinstance(line.obj.get('jsonPayload'), dict) else {}
        for src in (line.obj, payload):
            for key in JSON_TRACE_KEYS:
                if src.get(key):
                    return _norm(src[key])
    m = TRACEPARENT_RE.search(line.text)
    if m:
        return m.group(1).lower()
    m = ANY_ID_RE.search(line.text)
    return m.group(1).lower() if m else None


def thread_of(line):
    if isinstance(line.obj, dict):
        for key in ('thread', 'thread_name', 'logger', 'service'):
            if line.obj.get(key):
                return str(line.obj[key])
    for m in THREAD_RE.finditer(line.text):
        token = m.group(1).strip()
        if token and token != '-' and not ANY_ID_RE.fullmatch(token):
            return token
    return '(未知线程)'


def thread_group(name):
    """mdc-trace-1 / mdc-trace-2 → mdc-trace-N，便于按线程池归类。"""
    return re.sub(r'\d+', 'N', name)


# ── probe ─────────────────────────────────────────────────────────────────

def build_headers(style, trace, span):
    if style == 'w3c':
        return {'traceparent': f'00-{trace}-{span}-01'}
    if style == 'cloud':
        return {'X-Cloud-Trace-Context': f'{trace}/{int(span, 16)};o=1'}
    if style.startswith('header:'):
        return {style.split(':', 1)[1]: trace}
    raise SystemExit(f'未知 --style：{style}（可选 w3c、cloud、header:<头名>）')


def cmd_probe(a):
    if a.count > 500:
        raise SystemExit('--count 超过 500：这是验证工具，不是压测工具')

    def one(i):
        trace, span = secrets.token_hex(16), secrets.token_hex(8)
        headers = build_headers(a.style, trace, span)
        req = urllib.request.Request(a.url, method=a.method, headers=headers,
                                     data=b'' if a.method == 'POST' else None)
        t0 = time.time()
        rec = {'i': i, 'trace': trace, 'status': 0, 'ms': 0, 'echo': None}
        try:
            with urllib.request.urlopen(req, timeout=a.timeout) as resp:
                rec['status'] = resp.status
                for h in a.echo_header:
                    v = resp.headers.get(h)
                    if v:
                        rec['echo'] = _norm(v)
                        break
        except urllib.error.HTTPError as e:
            rec['status'] = e.code
        except Exception as e:  # 连接失败也要记下来，verify 会把它当失败
            rec['error'] = f'{type(e).__name__}: {e}'
        rec['ms'] = round((time.time() - t0) * 1000)
        return rec

    with ThreadPoolExecutor(max_workers=a.concurrency) as pool:
        recs = list(pool.map(one, range(a.count)))
    with open(a.out, 'w', encoding='utf-8') as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    ok = sum(1 for r in recs if 200 <= r['status'] < 400)
    echoed = sum(1 for r in recs if r['echo'] == r['trace'])
    print(f'probe：发出 {len(recs)} 个请求（并发 {a.concurrency}，头格式 {a.style}），成功 {ok}，'
          f'响应头回传了同一个号的 {echoed}')
    bad = [r for r in recs if not 200 <= r['status'] < 400]
    for r in bad[:5]:
        print(f'  失败：status={r["status"]} {r.get("error", "")}')
    print(f'已写入 {a.out}。等日志落盘后运行 verify。')


# ── verify ────────────────────────────────────────────────────────────────

def verify(lines, sent, exact=None, minimum=1):
    """返回 (problems, report_rows)。sent 是 probe 记录列表。"""
    want = {r['trace'].lower(): r for r in sent}
    hits = collections.defaultdict(list)
    for ln in lines:
        low = ln.text.lower()
        for t in want:
            if t in low:
                hits[t].append(ln)
    problems, rows = [], []
    for t, r in want.items():
        n = len(hits[t])
        if not 200 <= r.get('status', 0) < 400:
            problems.append(('请求失败', t, n, r))
        elif n == 0:
            problems.append(('日志里找不到', t, n, r))
        elif exact is not None and n < exact:
            problems.append(('行数偏少：有步骤丢了上下文', t, n, r))
        elif exact is not None and n > exact:
            problems.append(('行数偏多：别的请求的日志带上了这个号（串号）', t, n, r))
        elif exact is None and n < minimum:
            problems.append(('行数偏少', t, n, r))
        rows.append((t, n))
    return problems, rows, hits


def cmd_verify(a):
    lines = load_lines(a.log)
    with open(a.sent, encoding='utf-8') as f:
        sent = [json.loads(x) for x in f if x.strip()]
    problems, rows, hits = verify(lines, sent, a.lines, a.min_lines)
    expect = f'恰好 {a.lines} 行' if a.lines is not None else f'至少 {a.min_lines} 行'
    dist = collections.Counter(n for _, n in rows)
    print(f'verify：{len(sent)} 个号，每个号期望 {expect}；日志共 {len(lines)} 行')
    print('  出现行数分布：' + '，'.join(f'{k} 行×{v} 个号' for k, v in sorted(dist.items())))
    untraced = [ln for ln in lines if trace_of(ln) is None]
    if untraced:
        by = collections.Counter(thread_group(thread_of(ln)) for ln in untraced)
        print(f'  没有号的日志行 {len(untraced)} 行（启动日志属正常），按线程：'
              + '，'.join(f'{k}×{v}' for k, v in by.most_common(5)))
    if not problems:
        print('结果：通过 ✓ 每个号的行数都符合预期')
        return 0
    kinds = collections.Counter(p[0] for p in problems)
    print('结果：不通过 ✗ ' + '；'.join(f'{k} {v} 个' for k, v in kinds.items()))
    for kind, t, n, r in problems[:6]:
        print(f'  - {t}  出现 {n} 行  [{kind}]')
        for ln in hits[t][:3]:
            print(f'      L{ln.no}: {ln.text[:160]}')
    return 1


# ── summary / show ────────────────────────────────────────────────────────

def cmd_summary(a):
    pattern = re.compile(a.pattern) if a.pattern else None
    lines = load_lines(a.log)
    traced = collections.Counter()
    untraced = collections.defaultdict(list)
    for ln in lines:
        t = trace_of(ln, pattern)
        if t:
            traced[t] += 1
        else:
            untraced[thread_group(thread_of(ln))].append(ln)
    n_un = sum(len(v) for v in untraced.values())
    print(f'summary：{len(lines)} 行；带号 {len(lines) - n_un} 行，涉及 {len(traced)} 个号；没有号 {n_un} 行')
    if traced:
        print('  行数最多的号：')
        for t, n in traced.most_common(a.top):
            print(f'    {t}  {n} 行')
    if untraced:
        print('  没有号的行，按线程分组（线程池丢上下文时，这里会集中出现某个线程池的名字）：')
        for g, lns in sorted(untraced.items(), key=lambda kv: -len(kv[1])):
            print(f'    {g}  {len(lns)} 行')
            for ln in lns[:2]:
                print(f'      L{ln.no}: {ln.text[:140]}')
    return 0


def cmd_show(a):
    want = a.trace.lower()
    if '/traces/' in want:
        want = want.rsplit('/traces/', 1)[1]
    if len(want) < 8:
        raise SystemExit('--trace 至少给 8 位，避免误匹配')
    lines = [ln for ln in load_lines(a.log) if want in ln.text.lower()]
    print(f'show：{want} 共 {len(lines)} 行')
    for ln in lines[:a.limit]:
        print(f'L{ln.no}: {ln.text[:a.width]}')
    if len(lines) > a.limit:
        print(f'……还有 {len(lines) - a.limit} 行，用 --limit 调大')
    return 0


# ── selftest ──────────────────────────────────────────────────────────────

def cmd_selftest(_a):
    import os
    import tempfile
    checks = 0

    def check(cond, what):
        nonlocal checks
        checks += 1
        if not cond:
            raise SystemExit(f'selftest 失败：{what}')

    ids = [secrets.token_hex(16) for _ in range(4)]
    sent = [{'trace': t, 'status': 200} for t in ids]

    def mk(rows):
        return [Line(i + 1, r) for i, r in enumerate(rows)]

    ok = mk(['10:00:00 [main] INFO [-] - started']
            + [f'10:00:01 [exec-{i}] INFO [{t}] - 收到请求' for i, t in enumerate(ids)]
            + [f'10:00:01 [mdc-trace-{i % 2}] INFO [{t}] - 线程池里面' for i, t in enumerate(ids)])
    p, _, _ = verify(ok, sent, exact=2)
    check(not p, 'ok 场景应通过')

    lost = mk([f'10:00:01 [exec-{i}] INFO [{t}] - 收到请求' for i, t in enumerate(ids)]
              + ['10:00:01 [mdc-trace-1] INFO [-] - 线程池里面'] * 4)
    p, _, _ = verify(lost, sent, exact=2)
    check(len(p) == 4 and all('偏少' in k for k, *_ in p), 'lost 场景应报 4 个偏少')

    leak = mk([f'10:00:01 [exec-{i}] INFO [{t}] - 收到请求' for i, t in enumerate(ids)]
              + [f'10:00:01 [mdc-trace-1] INFO [{ids[0]}] - 线程池里面'] * 4)
    p, _, _ = verify(leak, sent, exact=2)
    kinds = {k for k, *_ in p}
    check(any('串号' in k for k in kinds) and any('偏少' in k for k in kinds), 'leak 场景应同时报串号与偏少')

    failed = [{'trace': ids[0], 'status': 0}]
    p, _, _ = verify(ok, failed, exact=2)
    check(p and p[0][0] == '请求失败', '请求失败要单独报告')

    tp = f'00-{ids[1]}-{secrets.token_hex(8)}-01'
    check(trace_of(Line(1, f'SELECT 1 /*traceparent=\'{tp}\',user_id=\'u1\'*/')) == ids[1], 'SQL 注释里的 traceparent')
    check(trace_of(Line(1, '{"x":1}', {'logging.googleapis.com/trace': f'projects/p/traces/{ids[2]}'})) == ids[2], 'Cloud Logging JSON 字段')
    check(trace_of(Line(1, '10:00 [main] INFO [-] - started')) is None, '没有号的行')
    check(thread_group(thread_of(Line(1, '10:00 [mdc-trace-12] INFO [-] - x'))) == 'mdc-trace-N', '线程分组')

    entries = [{'timestamp': '2026-10-06T15:33:34Z', 'trace': f'projects/p/traces/{ids[3]}',
                'httpRequest': {'requestUrl': '/api/x?page=2', 'status': 200}},
               {'timestamp': '2026-10-06T15:33:33Z', 'trace': f'projects/p/traces/{ids[3]}',
                'httpRequest': {'requestUrl': '/api/x?page=1', 'status': 200}}]
    fd, path = tempfile.mkstemp(suffix='.json')
    with os.fdopen(fd, 'w') as f:
        json.dump(entries, f)
    try:
        lines = load_lines(path)
        check(len(lines) == 2 and 'page=1' in lines[0].text, 'gcloud JSON 数组按时间排序')
        check(trace_of(lines[0]) == ids[3], 'gcloud 条目的 trace 字段')
    finally:
        os.unlink(path)

    print(f'selftest 通过 ✓（{checks} 项检查）')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog='tracekit', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('probe', help='并发发请求，每个带唯一 traceId')
    p.add_argument('--url', required=True)
    p.add_argument('--count', type=int, default=20)
    p.add_argument('--concurrency', type=int, default=8)
    p.add_argument('--style', default='w3c', help='w3c（traceparent）| cloud（X-Cloud-Trace-Context）| header:<头名>')
    p.add_argument('--method', default='GET', choices=['GET', 'POST'])
    p.add_argument('--timeout', type=float, default=15)
    p.add_argument('--echo-header', action='append', default=None,
                   help='响应里回传号的头名，可写多次；默认检查 X-Trace-Id、traceparent')
    p.add_argument('--out', default='sent.jsonl')

    v = sub.add_parser('verify', help='核对每个号在日志里的行数')
    v.add_argument('--log', required=True)
    v.add_argument('--sent', required=True)
    g = v.add_mutually_exclusive_group()
    g.add_argument('--lines', type=int, help='每个请求应写的日志行数（含线程池里写的），要求恰好相等')
    g.add_argument('--min-lines', type=int, default=1, help='不清楚确切行数时，至少出现几行')

    s = sub.add_parser('summary', help='统计有/没有号的行，按线程分组')
    s.add_argument('--log', required=True)
    s.add_argument('--pattern', help='自定义提取号的正则，第 1 个分组是号')
    s.add_argument('--top', type=int, default=5)

    w = sub.add_parser('show', help='打印某个号的全部日志')
    w.add_argument('--log', required=True)
    w.add_argument('--trace', required=True)
    w.add_argument('--limit', type=int, default=200)
    w.add_argument('--width', type=int, default=220)

    sub.add_parser('selftest', help='自检')

    a = ap.parse_args(argv)
    if a.cmd == 'probe' and a.echo_header is None:
        a.echo_header = ['X-Trace-Id', 'traceparent']
    return {'probe': cmd_probe, 'verify': cmd_verify, 'summary': cmd_summary,
            'show': cmd_show, 'selftest': cmd_selftest}[a.cmd](a) or 0


if __name__ == '__main__':
    sys.exit(main())
