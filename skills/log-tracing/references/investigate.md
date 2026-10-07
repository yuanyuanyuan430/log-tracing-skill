# 排查：拿到号之后怎么查

## 号从哪来

| 来源 | 位置 |
|---|---|
| 用户报错 | 响应头 `X-Trace-Id`、错误页上显示的号、前端错误上报（如 Sentry 的 trace_id） |
| 一行日志 | `[%X{traceId}]` 那一段，或者 JSON 里的 `trace_id` / `logging.googleapis.com/trace` |
| 慢 SQL | 语句末尾的 `/*…traceparent='00-<trace-id>-…'…*/`，中间那段 32 位就是号 |
| 平台请求日志 | Cloud Run / GCLB 请求日志的 `trace` 字段（`projects/<P>/traces/<id>`） |
| 告警 | 告警只报数量，没有号：先按时间窗口找出代表性的慢 SQL 或请求，再从它身上拿号 |

## 本地或文件日志

```bash
T=<skill>/scripts/tracekit.py
python3 $T show --log logs/detail.log --trace 9dbe79bb1b46        # 至少 8 位前缀
python3 $T summary --log logs/detail.log                          # 整体情况、没有号的行
```

## Cloud Logging

先确认当前环境对 gcloud 的约定（例如每条命令先查 `gcloud help logging read`、带上 `--quiet`、显式 `--project`、限制 `--limit` 和 `--format`）。以下是查询模板：

```bash
# 1) 按号拉出整条链路（请求日志 + 应用日志）
gcloud logging read 'trace="projects/<PROJECT>/traces/<TRACE_ID>"' \
  --project=<PROJECT> --order=asc --limit=200 --quiet \
  --format="value(timestamp,logName.basename(),httpRequest.status,httpRequest.requestUrl,jsonPayload.message,jsonPayload.user_id)"

# 2) 从某个请求拿号
gcloud logging read 'resource.type="cloud_run_revision" AND httpRequest.requestUrl:"/api/admin/items?page=1" AND timestamp>="…" AND timestamp<"…"' \
  --project=<PROJECT> --limit=1 --quiet --format="value(timestamp,trace)"

# 3) 带了 SQL 注释的慢查询（Cloud SQL for PostgreSQL）
gcloud logging read 'resource.type="cloudsql_database" AND textPayload:"duration:" AND textPayload:"traceparent=" AND timestamp>="…"' \
  --project=<PROJECT> --limit=20 --quiet --format="value(timestamp,textPayload)"

# 4) 导出成 JSON，再用 tracekit 归类分析
gcloud logging read 'trace="projects/<PROJECT>/traces/<TRACE_ID>"' --project=<PROJECT> --limit=500 --quiet --format=json > trace.json
python3 $T summary --log trace.json
python3 $T show --log trace.json --trace <TRACE_ID>
```

## 怎么读结果

- **一个号对应的范围不一定只是一个请求。** 浏览器里的前端 SDK（如 Sentry）常把整个页面的 trace 带进这个页面发出的每个 fetch，这时一个号代表"一个页面会话"。这反而很有用：脚本请求和页面自己的请求共用一个号，就能证明脚本是在这个页面里跑的。
- **200 的请求往往没有应用日志。** 为了省钱，很多服务只在出错时写应用日志。这时身份要从别处找：SQL 注释里的 `user_id`、同一个 trace 里带着账号参数的请求（比如加载用户偏好设置的请求）、或者鉴权日志。
- **IP 往往不是用户的。** 经过 Cloudflare 或其他 CDN 时，请求日志里的 `remoteIp` 只是边缘节点的地址。UA 也只能缩小范围。所以不要凭 IP 和 UA 就下"查不出是谁"的结论，先顺着号查到底。
- **ID 只是 ID。** 查到 `user_id` 以后，要对应到具体是谁，需要查用户表。没有这个权限就如实说明，把 ID 交给有权限的人。

## 案例：一次后台慢查询告警（已脱敏）

1. 告警：生产数据库一小时内有 175 条慢查询，阈值是 60。慢查询日志显示，同一条后台列表 SQL 并发执行，最慢 9.2 秒。
2. 那时 SQL 还没有注释，所以先查 Cloud Run 请求日志：23:33–23:40 有 `/api/admin/items?page=1..89&pageSize=100` 并发请求，IP 是 Cloudflare 的边缘节点。
3. 取其中一个请求的 `trace` 字段，用 `trace="…"` 一查，拉出 369 条记录：23:31 打开后台品牌页，23:33 开始脚本翻页，之后每分钟轮询一次，一直到 02:00。都是同一个页面会话。
4. 页面加载时有一个读取用户表格偏好的请求，参数里带着 `accountId=<uid>`，从这里拿到账号 ID。
5. 第二天上线了 SQL 注释。此后再出现慢 SQL，直接看注释里的 `user_id` 和 `traceparent` 就行。

事后回看：一开始只看 IP 和 UA，就下了"查不出是谁"的结论，这是错的。顺着 trace 查，三步就能定位到账号。
