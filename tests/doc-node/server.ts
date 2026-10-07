// tests/doc-node 的测试服务：用 node.md 里的 trace.ts，每个请求写 3 行日志（await 前、await 后、定时器回调里）。
import http from 'node:http';
import { traceIdFrom, runWithTrace, log, outgoingTraceparent } from './trace.ts';

http.createServer((req, res) => {
  const traceId = traceIdFrom((h) => {
    const v = req.headers[h.toLowerCase()];
    return Array.isArray(v) ? v[0] : v;
  });
  res.setHeader('X-Trace-Id', traceId);
  runWithTrace(traceId, async () => {
    log('INFO', '收到请求');
    await new Promise((r) => setTimeout(r, 10));
    log('INFO', 'await 之后', { downstream: outgoingTraceparent()?.slice(0, 35) });
    await new Promise<void>((r) => setTimeout(() => { log('INFO', '定时器回调里'); r(); }, 5));
    res.end('ok');
  });
}).listen(Number(process.env.PORT ?? 18082), '127.0.0.1', () => process.stderr.write('node test server ready\n'));
