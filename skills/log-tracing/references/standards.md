# 格式标准、字段名与隐私

## 追踪头

| 头 | 格式 | 说明 |
|---|---|---|
| `traceparent`（W3C，首选） | `00-<trace-id 32位十六进制>-<parent-id 16位十六进制>-<flags 2位>` | 全部小写；trace-id 或 parent-id 全为 0 时无效；flags 为 `01` 表示已采样。可以附带 `tracestate` 头，原样透传即可 |
| `X-Cloud-Trace-Context`（Google 旧格式） | `TRACE_ID/SPAN_ID;o=1` | SPAN_ID 是**十进制**；Cloud Run、GCLB 会自动注入 |
| `X-B3-TraceId` 等（Zipkin B3） | 16 或 32 位十六进制 | 老的 Brave / Zipkin 体系在用，新系统不要再引入 |
| 自定义头（`logId`、`X-Request-Id`） | 团队约定 | 只在过渡期兼容；所有服务统一以后改用 `traceparent` |

生成号的规则：trace-id 用 32 位小写十六进制（16 字节随机数，或去掉横线的 UUID）。带横线的 36 位 UUID 不符合 W3C 格式，上游、下游、平台都不认。

入口的处理顺序：`traceparent` → 平台头 → 过渡期的自定义头 → 都没有或都不合法时才生成。不要在别人的号后面拼接字符，号必须端到端完全一致，才能被精确搜索到。

## 日志字段

| 场景 | 字段 |
|---|---|
| 文本日志（logback 等） | pattern 里写 `[%X{traceId:-}] [%X{uid:-}]`，和线程名 `[%thread]` 放在一起 |
| JSON 日志（通用） | `trace_id`、`span_id`、`user_id`；团队统一一套命名，不要一个服务用 `traceId`、另一个用 `trace_id` |
| Cloud Logging | 写 `logging.googleapis.com/trace` = `projects/<PROJECT_ID>/traces/<trace-id>`，以及 `logging.googleapis.com/spanId`。写对了，应用日志就会和 Cloud Run 自动生成的请求日志串在一起，Logs Explorer 里点一条日志就能用 "Show entries for this trace" 拉出整条链路。`severity` 字段决定日志级别；stdout 上的纯文本一律按 INFO 记录 |

## SQL 注释（sqlcommenter）

在 SQL 末尾追加（追加前先换行）：

```
SELECT … FROM orders … LIMIT $7
/*tenant_id='t_42',traceparent='00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01',user_id='u_8f3a2c'*/
```

- 键按字典序排列，值做 URL 编码后用单引号包起来，多个键值用逗号分隔。
- Postgres、MySQL 记录慢查询时，会把注释原样写进日志，慢 SQL 自己就能说出是谁发的、属于哪条链路。Cloud SQL 的 Query Insights 也能识别（实例需要开启 `recordApplicationTags`）。
- `pg_stat_statements` 计算 queryid 时不算注释，同一条语句仍然归并成一行。使用具名 prepared statement 时，要确认注释不会让语句缓存失效。

## 隐私与安全

- uid 只用不透明 ID（数据库主键、UUID）。不要把邮箱、手机号、IP、完整 UA、完整 referrer URL 写进日志或 SQL 注释。
- uid 只能在服务端鉴权之后设置。请求头里传来的 uid 不可信，否则任何人都能把日志栽到别人头上。
- 往注释或日志里写的值要校验字符集，比如 `^[\w-]{1,64}$`，不合格的整项丢弃。这样能防止有人借注释注入 SQL，或者伪造日志行。
- traceId 可以回传给客户端（响应头）。它本身不含敏感信息，但不要拿它当鉴权凭证使用。
