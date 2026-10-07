---
name: log-tracing
description: 全链路日志追踪（traceId + spanId + uid）：给服务接入请求追踪号，让一个请求、一个用户的所有日志能用一个号码串起来；审查已有实现的漏洞（线程池丢 trace、MDC 不清理导致串号、不透传下游、缺 uid）；拿到 traceId 后串日志，定位到具体请求和账号。方法源自"SpringAOP + Logback + MDC 现网日志追踪方案"，并扩展到 Node（AsyncLocalStorage）、Python（contextvars）、W3C traceparent、Cloud Logging 和 SQL 注释。用户提到 traceId、trace、logId、MDC、全链路日志、日志追踪、请求 ID、串日志、"这个请求是谁发的"、"并发时日志对不上"、"线程池里日志没有 traceId"，要给 Spring/Node/Python 服务加日志追踪，或要按 trace 查 Cloud Logging 和慢 SQL 时，都应使用本技能，即使用户没有直接说"trace"。Use when adding, reviewing, verifying, or investigating with request trace IDs in logs.
---

# 全链路日志追踪（traceId + uid）

请求一进门就拿到或生成一个 traceId，放进"当前请求上下文"。这条请求经过的每一行日志、每一次下游调用、每一条 SQL 都自动带上它，鉴权后再放进 uid。排查时搜这个号，散落在各处的日志就能拼回一条完整的经过：哪个人、哪个页面、做了什么、慢在哪。

没有 traceId 时，只能按时间去对日志。并发一高，同一秒有十几个请求交错，就会对错。这是整套方案要解决的问题。

## 先判断任务类型

| 类型 | 典型说法 | 做什么 |
|---|---|---|
| A 接入 | "给这个 Spring 项目加 traceId" | 按下面「七个落点」接入，读对应语言的 reference，最后跑验证 |
| B 审查 | "我们的日志追踪有没有问题" | 对照「审查清单」读代码，再跑验证拿证据 |
| C 排查 | "昨晚那个慢请求是谁触发的" | 读 `references/investigate.md` |

## 七个落点（任何语言都一样，缺一个就是一个漏洞）

1. **入口**：先读上游带来的追踪头。优先 W3C `traceparent`，其次平台头（如 `X-Cloud-Trace-Context`）或团队约定的头。号合法就沿用，没有或不合法才生成。要沿用是因为网关、前端、上游服务已经有号，自己再生成一个，一条链就断成了两截。入口要放在最先执行的位置（Servlet Filter 或最外层中间件），否则鉴权、过滤器、全局异常处理打的日志都不带号。
2. **存储**：放进请求级上下文，不要一层层传参。Java 用 MDC（基于 ThreadLocal），Node 用 AsyncLocalStorage，Python 用 contextvars。
3. **输出**：让日志格式自动带上号。logback 在 pattern 里写 `%X{traceId}`，JSON 日志写成字段。上 Cloud Logging 时要用 `logging.googleapis.com/trace` 这个字段名，才能和平台自动生成的请求日志关联起来。
4. **异步**：换线程、换任务时把上下文一起带过去。线程池配 TaskDecorator；`@Async` 要用配好装饰器的 executor；`CompletableFuture.supplyAsync` 必须显式传 executor，默认的 commonPool 不经过装饰器。Node 的上下文会自动跟着 async/await 走，但在请求之外启动的后台任务和队列消费者，需要自己建上下文。
5. **出口**：调下游 HTTP、发 MQ 消息时把追踪头带出去；响应头回传号（如 `X-Trace-Id`），这样用户报错时能直接报出这个号。
6. **身份**：鉴权通过后，把 uid 放进上下文，和 traceId 一起打进日志。只放不透明的用户 ID，不放邮箱、手机号、IP。uid 必须来自服务端会话，不能信请求头里传来的值。
7. **清理**：Java 在 `finally` 里执行 `MDC.remove` 或 `MDC.clear`。线程池里的线程会复用，不清理的话，下一个请求会带着上一个人的号（串号），这比没有号更糟，因为它会把查询引到错误的人身上。Node 的 `run()` 作用域一结束，上下文自动失效。

可选的第 8 点是**数据库**：用 sqlcommenter 在 SQL 末尾追加 `/*traceparent='…',user_id='…'*/`。慢查询日志本身就能说出是谁发的、属于哪条链路。做法见 `references/standards.md`。

按语言读对应的 reference，里面有可直接改的代码模板：

- Java / Spring：`references/java-spring.md`。含原文方案整理、原文代码里的几个坑，以及 Spring Boot 3 推荐的 Micrometer Tracing 方案。
- Node / TypeScript（以及 Python 的写法）：`references/node.md`
- 头格式、字段名、隐私要求、SQL 注释格式：`references/standards.md`

## 验证（接入和审查都要做，没跑过不算完成）

日志追踪的毛病只在**并发**时暴露：一个个串行地发请求，永远看不出串号。所以用自带脚本 `scripts/tracekit.py` 并发打请求，再去日志里逐个核对。脚本只用 Python 标准库，不需要安装依赖。

```bash
S=<本技能目录>/scripts
T=$S/tracekit.py
python3 $T selftest                                    # 脚本自检
python3 $T probe --url http://localhost:8080/pool --count 20 --concurrency 8 --out sent.jsonl
python3 $T verify --log logs/detail.log --sent sent.jsonl --lines 2
python3 $T summary --log logs/detail.log               # 哪个线程的日志丢了号
```

- `probe`：给每个请求生成一个唯一的 traceId，默认放在 `traceparent` 头里；用 `--style header:logId` 可以改成团队自己的头名。只对本地或测试环境使用；要压生产环境，先取得负责人同意。
- `verify --lines N`：N 是这个接口每处理一个请求会写几行日志（包括线程池里写的）。每个号都应该恰好出现 N 次：
  - 少于 N：某一步丢了上下文，通常是线程池；
  - 多于 N：别的请求的日志带上了这个号，说明有串号，通常是 MDC 没清理。
  - 脚本发现问题时退出码为 1。
- `summary`：把没有号的日志行按线程分组列出来，用来定位是哪个线程池丢了上下文。

交付时把 `verify` 的输出贴出来作为证据。退出码为 0、所有号都对上，才算通过。

**教学演示**：`scripts/demo_server.py` 是一个很小的 Python 服务，能重现原文要解决的问题。`--mode lost` 模拟线程池丢号，`--mode leak` 模拟线程复用不清理导致串号，`--mode ok` 是修好之后的样子。`scripts/run_demo.sh` 会把三种模式依次跑一遍，并核对结论（lost、leak 应该不通过，ok 应该通过）。它既能给人讲解原理，也是本技能自己的回归测试：改了 `tracekit.py` 之后跑一次。

```bash
bash $S/run_demo.sh                 # 三种模式全跑，最后一行显示"都符合预期 ✓"
# 单独看某一种：
python3 $S/demo_server.py --mode lost --port 18080 --log /tmp/demo.log &
python3 $T probe --url http://127.0.0.1:18080/work --count 20 --concurrency 8 --out /tmp/sent.jsonl
python3 $T verify --log /tmp/demo.log --sent /tmp/sent.jsonl --lines 2
```

## 审查清单（常见漏洞：症状 → 改法）

- **入口只用 AOP 切 Controller 注解**（原文就是这么做的）：过滤器、Spring Security、全局异常处理、404/`/error` 打的日志都没有号。改成最高优先级的 Servlet Filter（`OncePerRequestFilter`）。
- **原文 logId 那一行有运算符优先级 bug**：`isNotBlank(logId) ? (logId + "_") : "" + UUID...`，`+` 比 `?:` 先算，所以有上游号时结果变成 `abc_`，不会追加新号。上游号合法就原样沿用，不要拼接。
- **子线程里改号**（原文在后面拼了 `_线程名`）：按号精确搜索时，子线程的日志会漏掉。线程名已经在 `%thread` 里了，号保持原值。
- **`CompletableFuture` / `parallelStream` 没传 executor**：任务落在 commonPool 上，不经过装饰器，号就丢了。
- **`@Async` 用的 executor 没配装饰器**：这些日志全都不带号。
- **下游调用不透传**：链路在服务边界断开，上下游各有各的号。
- **各服务用的头名不统一**（这个叫 logId，那个叫 X-Request-Id）：统一用 `traceparent`。
- **没有 uid，或者 uid 记成了邮箱、手机号**：能串起请求，但说不出是谁；或者在日志里泄露了个人信息。
- **只在出错时才写应用日志**：成功的请求只剩平台访问日志里的 trace 字段，查不到身份。可以在 SQL 注释里带上 uid，或者在关键业务事件上打一行带身份的日志。
- **定时任务、MQ 消费者没有入口**：这类任务没有 HTTP 请求，要在任务开头自己生成号；消费消息时从消息头里恢复号。

## 排查（拿到号之后）

号可以从这些地方来：响应头、任意一行日志、慢 SQL 末尾的注释、前端错误上报（如 Sentry）、平台请求日志的 `trace` 字段。拿到号以后，按号拉出全部日志，按时间排好，读出经过了哪些服务、是哪个 uid。具体步骤、Cloud Logging 查询写法和真实案例见 `references/investigate.md`。

结论要以证据为准：在没顺着号查到底之前，不要下"查不出是谁"的结论。只看 IP 和 UA 往往不够，比如经过 Cloudflare 的请求，记下来的 IP 只是边缘节点。而顺着 trace 查，常常能查到同一个页面会话里带着账号 ID 的请求。
