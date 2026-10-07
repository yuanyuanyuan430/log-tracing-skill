#!/usr/bin/env python3
"""教学用的小服务：重现原文要解决的问题，并演示修好后的样子。

  --mode lost   线程池里丢号：任务直接丢进线程池，没带上请求上下文
  --mode leak   线程复用串号：工作线程第一次拿到号后一直留着、从不清理（模拟 MDC 用完没 clear）
  --mode ok     修好：提交任务时复制请求上下文（相当于 Java 的 TaskDecorator + finally 清理）

每个请求写 2 行日志：处理请求的线程 1 行，线程池里 1 行。日志格式仿照原文的 logback：
  HH:MM:SS.mmm [线程名] INFO [traceId] - 消息

用法：
  python3 demo_server.py --mode lost --port 18080 --log /tmp/demo.log
  然后用 tracekit.py probe / verify --lines 2 去测。只监听 127.0.0.1。
"""
import argparse
import contextvars
import json
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TRACE = contextvars.ContextVar('trace', default='-')   # 相当于 MDC 里的 traceId
_stale = threading.local()                              # leak 模式用：模拟没清理的 ThreadLocal
_write_lock = threading.Lock()
TRACEPARENT = re.compile(r'^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$')
CLOUD = re.compile(r'^([0-9a-f]{32})(?:/\d+)?(?:;o=[01])?$')
HEX32 = re.compile(r'^[0-9a-f]{32}$')


def make_logger(path):
    f = open(path, 'a', encoding='utf-8', buffering=1)

    def log(msg, trace=None):
        now = time.time()
        stamp = time.strftime('%H:%M:%S', time.localtime(now)) + f'.{int(now % 1 * 1000):03d}'
        line = f'{stamp} [{threading.current_thread().name}] INFO [{trace or TRACE.get()}] - {msg}\n'
        with _write_lock:
            f.write(line)
    return log


def incoming_trace(headers):
    """入口：沿用上游的号；没有或不合法才生成（落点 1）。"""
    m = TRACEPARENT.match((headers.get('traceparent') or '').strip().lower())
    if m and m.group(1) != '0' * 32:
        return m.group(1)
    m = CLOUD.match((headers.get('X-Cloud-Trace-Context') or '').strip().lower())
    if m and m.group(1) != '0' * 32:
        return m.group(1)
    for h in ('X-Trace-Id', 'logId'):
        v = (headers.get(h) or '').strip().lower()
        if HEX32.match(v):
            return v
    return secrets.token_hex(16)


def build_handler(mode, pool, log):
    def pool_task():
        if mode == 'leak':
            log('线程池里面', trace=getattr(_stale, 'trace', '-'))
        else:
            log('线程池里面')
        time.sleep(0.03)
        return 'done'

    def submit(trace):
        if mode == 'ok':
            # 复制当前请求的上下文给子任务；任务结束上下文随之丢弃，不会留在线程上
            return pool.submit(contextvars.copy_context().run, pool_task)
        if mode == 'leak':
            def task():
                if not getattr(_stale, 'trace', None):   # bug：只在第一次设置，之后从不清理
                    _stale.trace = trace
                return pool_task()
            return pool.submit(task)
        return pool.submit(pool_task)                      # lost：什么都不带

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):   # 关掉默认访问日志，只看我们的日志
            pass

        def do_GET(self):
            if not self.path.startswith('/work'):
                self.send_response(404)
                self.end_headers()
                return
            trace = incoming_trace(self.headers)
            token = TRACE.set(trace)                       # 落点 2：放进请求上下文
            try:
                log('收到请求')
                result = submit(trace).result(timeout=10)
                body = json.dumps({'trace': trace, 'result': result}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('X-Trace-Id', trace)      # 落点 5：响应头回传
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            finally:
                TRACE.reset(token)                         # 落点 7：清理

    return Handler


class DemoServer(ThreadingHTTPServer):
    request_queue_size = 128   # 默认 5，并发探测时会被内核直接重置连接
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--mode', choices=['lost', 'leak', 'ok'], required=True)
    ap.add_argument('--port', type=int, default=18080)
    ap.add_argument('--log', default='demo.log')
    ap.add_argument('--workers', type=int, default=2, help='线程池大小；小一点更容易看到线程复用')
    a = ap.parse_args()
    log = make_logger(a.log)
    pool = ThreadPoolExecutor(max_workers=a.workers, thread_name_prefix='mdc-trace')
    server = DemoServer(('127.0.0.1', a.port), build_handler(a.mode, pool, log))
    log(f'demo started mode={a.mode} port={a.port}', trace='-')
    print(f'demo_server mode={a.mode} 监听 http://127.0.0.1:{a.port}/work ，日志写到 {a.log}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        pool.shutdown(wait=False)


if __name__ == '__main__':
    main()
