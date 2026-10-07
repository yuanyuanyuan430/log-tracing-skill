# Node / TypeScript 接入（附 Python 写法）

Node 没有"一个请求一个线程"，所以不用 ThreadLocal，用 `AsyncLocalStorage`。它会自动跟着 async/await、Promise、定时器传下去，所以 Java 里最麻烦的两件事——给线程池加装饰器和用完清理——在这里都不需要做。

下面的写法和一套已经在生产上跑的实现是同一思路：请求上下文 + 单行 JSON 结构化日志 + SQL 注释。照着它接入以后，用 `scripts/tracekit.py` 验证。

## 最小实现

```ts
// trace.ts
import { AsyncLocalStorage } from 'node:async_hooks';
import { randomBytes } from 'node:crypto';

type TraceContext = { traceId: string; spanId: string; uid?: string };
const als = new AsyncLocalStorage<TraceContext>();

const TRACEPARENT = /^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$/;
const CLOUD = /^([0-9a-f]{32})(?:\/\d+)?(?:;o=[01])?$/;
const isZero = (s: string) => /^0+$/.test(s);

/** 入口：优先沿用上游的号，没有或不合法才生成。 */
export function traceIdFrom(get: (name: string) => string | null | undefined): string {
  const tp = TRACEPARENT.exec((get('traceparent') ?? '').trim().toLowerCase());
  if (tp && !isZero(tp[1])) return tp[1];
  const cloud = CLOUD.exec((get('x-cloud-trace-context') ?? '').trim().toLowerCase());
  if (cloud && !isZero(cloud[1])) return cloud[1];
  return randomBytes(16).toString('hex');
}

export function runWithTrace<T>(traceId: string, fn: () => T): T {
  return als.run({ traceId, spanId: randomBytes(8).toString('hex') }, fn);
}

/** 只在服务端鉴权之后调用；uid 不能来自请求头。 */
export function setUid(uid: string) {
  const ctx = als.getStore();
  if (ctx) ctx.uid = uid;
}

export const traceContext = () => als.getStore();

/** 调下游时用：同一个 trace-id，新的 span-id。 */
export function outgoingTraceparent(): string | undefined {
  const ctx = als.getStore();
  return ctx ? `00-${ctx.traceId}-${randomBytes(8).toString('hex')}-01` : undefined;
}

/** 单行 JSON 写 stdout；Cloud Run / GKE 会自动解析成 jsonPayload。 */
export function log(severity: 'INFO' | 'WARNING' | 'ERROR', message: string, fields: Record<string, unknown> = {}) {
  const ctx = als.getStore();
  const project = process.env.GOOGLE_CLOUD_PROJECT;
  const line = {
    severity,
    message,
    ...fields,
    ...(ctx && { trace_id: ctx.traceId, ...(ctx.uid && { user_id: ctx.uid }) }),
    ...(ctx && project && {
      'logging.googleapis.com/trace': `projects/${project}/traces/${ctx.traceId}`,
      'logging.googleapis.com/spanId': ctx.spanId,
    }),
  };
  process[severity === 'INFO' ? 'stdout' : 'stderr'].write(JSON.stringify(line) + '\n');
}
```

挂到框架上，以 Express 为例（Fastify、Hono、TanStack Start 同理，挂在最外层）：

```ts
app.use((req, res, next) => {
  const traceId = traceIdFrom((h) => req.get(h));
  res.setHeader('X-Trace-Id', traceId);
  runWithTrace(traceId, next);
});

// 鉴权中间件里：拿到会话之后
setUid(session.userId);
```

调下游：

```ts
const tp = outgoingTraceparent();
await fetch(url, { headers: { ...headers, ...(tp && { traceparent: tp }) } });
```

已经在用 pino 的话，不需要自己写 `log`，用 mixin 把上下文合进每一行就行：

```ts
const logger = pino({ mixin: () => {
  const c = traceContext();
  return c ? { trace_id: c.traceId, user_id: c.uid } : {};
} });
```

## SQL 注释（落点 8，可选）

在数据库驱动的查询入口包一层，把 `/*traceparent='…',user_id='…'*/` 追加到 SQL 末尾。格式规则见 `standards.md`。实现时注意：

- 追加前先换行。如果原语句以 `-- 行注释` 结尾，同一行追加的注释会被吞掉，静默失效。
- 值要校验字符集，并做 URL 编码。值里如果出现 `*/`，后面的内容就会被当成 SQL 解析。
- 不在请求里执行的 SQL（迁移、后台任务）没有上下文，原样执行，不要硬塞号。

## 常见漏洞

- **后台任务没有上下文。** 启动时就开始跑的 `setInterval`、队列消费者，不在任何请求里，日志自然没有号。每轮执行时自己 `runWithTrace(newId, ...)`。
- **监听器在请求之外注册。** EventEmitter 的回调跑在注册它时的上下文里，不是触发它时的上下文。
- **worker_threads 不会自动传。** 需要把号作为消息的一部分传过去。
- **多处入口各自生成号。** SSR 页面内部又调用自己的 API handler 时，要复用外层的号，不要再生成一个。
- **只在出错时写日志。** 成功的请求就只剩平台访问日志里的 trace 字段，看不到 uid。可以在关键业务事件上打一行，或者用 SQL 注释把 uid 带出去。

## Python

用 `contextvars` 存号，再用 `logging.Filter` 把号塞进每条日志记录：

```python
import contextvars, logging
TRACE = contextvars.ContextVar('trace', default='-')
UID = contextvars.ContextVar('uid', default='-')

class TraceFilter(logging.Filter):
    def filter(self, record):
        record.trace_id, record.uid = TRACE.get(), UID.get()
        return True

handler = logging.StreamHandler()
handler.addFilter(TraceFilter())
handler.setFormatter(logging.Formatter('%(asctime)s [%(threadName)s] %(levelname)s [%(trace_id)s] [%(uid)s] - %(message)s'))
```

- asyncio 的 Task 会自动复制上下文。
- `ThreadPoolExecutor` **不会**：要用 `pool.submit(contextvars.copy_context().run, fn)`，这就相当于 Java 的 TaskDecorator。`scripts/demo_server.py` 里的 `lost` 和 `ok` 两种模式，演示的正是这个差别。
