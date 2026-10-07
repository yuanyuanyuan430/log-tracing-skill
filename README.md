# log-tracing-skill

![Agent Skill](https://img.shields.io/badge/Agent-Skill-111111)
![Claude Code](https://img.shields.io/badge/Claude%20Code-Skill-d97757)
![Codex](https://img.shields.io/badge/Codex-Skill-2563eb)
![Language](https://img.shields.io/badge/Language-中文-red)
![License](https://img.shields.io/badge/License-MIT-black)

给服务接上**全链路日志追踪（traceId + uid）**，让一个请求、一个用户的所有日志能用一个号码串起来，并且用**并发测试**证明它真的没丢号、没串号。

线上并发一高，同一秒十几个请求的日志交错在一起，只能对着时间去猜哪几行是同一件事，经常对错。解决办法很朴素：请求进门时拿到一个号，后面每一行日志、每一次下游调用、每一条 SQL 都自动带上它；鉴权之后再带上用户 ID。出了问题，搜这个号，就能把这个请求的完整经过拼回来。

这个 Skill 从公众号「程序员三木」的文章《互联网大厂都在使用的现网日志追踪方案 [SpringAop + Logback + MDC]》出发，修正了原文代码里的几个问题，扩展到 Node 和 Python，并补上了一个原文没有、但最容易被跳过的环节：**验证**。

## 它能帮你做什么

| 你说 | 它做 |
| --- | --- |
| "给这个 Spring 项目加 traceId" | 按"七个落点"接入：入口、存储、输出、异步、出口、身份、清理，给出可直接用的代码 |
| "我们照网上文章写的日志追踪，帮我看看" | 对照审查清单找漏洞：线程池丢号、MDC 不清理串号、不透传下游、缺 uid…… |
| "接好了，怎么证明没问题" | 用自带的 `tracekit` 并发打请求，逐个核对每个号在日志里出现几行 |
| "昨晚那个慢 SQL 是谁触发的" | 顺着 traceId 把请求日志、应用日志、慢 SQL 串起来，定位到页面、接口和账号 |

## 相比原文修正了什么

原文的思路是对的（AOP 注入 → MDC 存储 → logback 输出 → TaskDecorator 传给线程池），照抄会踩这几个坑。下面每一条都在真实的 Spring Boot 项目里复现过，见下文「已验证」：

1. **生成号那一行有运算符优先级 bug**：`isNotBlank(logId) ? (logId + "_") : "" + UUID...` 里 `+` 先算，有上游号时得到 `abc_`，不会追加新号。上游号合法就应该原样沿用。
2. **AOP 只切 Controller**：过滤器、Spring Security、全局异常处理打的日志都没有号。入口应该放在最高优先级的 Servlet Filter。
3. **子线程里拼 `_线程名`**：号被改了，按号精确搜索就搜不到子线程的日志。
4. **装饰器 `finally` 里 `MDC.clear()`**：线程池满了、拒绝策略是 CallerRunsPolicy 时，任务在请求线程上执行，`clear()` 会把请求线程自己的号也清掉。实测 30 个并发请求里，请求线程上有 83 行日志丢了号。应该恢复执行前的上下文，而不是清空。

另外补上了原文没讲的：沿用 W3C `traceparent` 标准头、响应头回传号、调下游时透传、鉴权后放 uid、MQ 和定时任务的入口、SQL 注释（sqlcommenter）、Cloud Logging 字段，以及 Spring Boot 3 用 Micrometer Tracing 的选择。

## 自带工具：tracekit

日志追踪的毛病只在并发时暴露，一个一个地发请求永远看不出串号。`tracekit` 只用 Python 标准库：

```bash
T=skills/log-tracing/scripts/tracekit.py
python3 $T probe  --url http://localhost:8080/pool --count 20 --concurrency 8 --out sent.jsonl
python3 $T verify --log logs/detail.log --sent sent.jsonl --lines 3
```

`--lines` 是这个接口每处理一个请求写几行日志，每个号都应该恰好出现这么多行：

```text
verify：30 个号，每个号期望 恰好 5 行；日志共 150 行
  出现行数分布：2 行×26 个号，3 行×4 个号
  没有号的日志行 86 行（启动日志属正常），按线程：http-nio-N-exec-N×83，tiny-orig-N×3
结果：不通过 ✗ 行数偏少：有步骤丢了上下文 30 个
```

- 少于预期：某一步丢了上下文；
- 多于预期：别的请求的日志带上了这个号，也就是串号；
- `summary`：把没有号的日志行按线程分组，看是哪个线程池丢的；
- `show`：按号把一个请求的完整经过列出来。

日志支持三种格式：普通文本行、每行一个 JSON，以及 `gcloud logging read --format=json` 导出的 JSON 数组。

还附了一个教学用的小服务 `demo_server.py`，三种模式 `lost`、`leak`、`ok` 分别重现"线程池丢号""线程复用串号"和"修好之后"，适合拿来讲原理。

## 安装

Claude Code：

```bash
git clone https://github.com/yuanyuanyuan430/log-tracing-skill.git
cp -R log-tracing-skill/skills/log-tracing ~/.claude/skills/
```

Codex：

```bash
cp -R log-tracing-skill/skills/log-tracing ~/.codex/skills/
```

## 直接调用

```text
给 ~/work/order-service 这个 Spring Boot 项目加上 traceId，线程池里的日志也要有号，接完用 tracekit 验证。
```

```text
这是我们现在的 MdcAspect 和 MDCTaskDecorator，帮我审查有没有串号、丢号的问题。
```

```text
慢 SQL 末尾有 /*traceparent='00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01'*/，帮我查是谁、在干什么。
```

## 已验证

文档里的代码不是"看起来对"，而是从文档里原样抽出来，编译、运行、并发测试过：

| 测试 | 环境 | 结果 |
| --- | --- | --- |
| `java-spring.md` 全部 Java 代码 | JDK 17 + Spring Boot 3.3.5 | 编译通过；修正版 30 个并发请求，每个号的行数都对得上；不传 executor（落到 commonPool）和原文 `MDC.clear()` 写法都被抓出来 |
| `node.md` 模板 | Node 26 | 上下文跨 `await` 和定时器都不丢，30 个并发请求每个号恰好 3 行；Cloud Logging 字段格式正确 |
| `node.md` 的 Python 写法 | Python 3 | 直接把任务交给线程池会丢号，用 `copy_context` 提交就带着号 |
| `demo_server` 三种模式 | Python 3 | lost、leak 不通过，ok 通过，连续多轮结果稳定 |

一条命令全部复现（没有 JDK 或 Maven 时，Java 那一项自动跳过）：

```bash
bash tests/run_all.sh
```

## 目录结构

```text
skills/log-tracing/
├── SKILL.md                    # 任务分类、七个落点、验证、审查清单、排查
├── references/
│   ├── java-spring.md          # 原文方案 + 修正版代码 + Micrometer Tracing
│   ├── node.md                 # AsyncLocalStorage 模板（附 Python 写法）
│   ├── standards.md            # traceparent、Cloud Logging 字段、SQL 注释、隐私
│   └── investigate.md          # 拿到号以后怎么查（含 gcloud 模板和一次真实排查的脱敏案例）
├── scripts/
│   ├── tracekit.py             # probe / verify / summary / show / selftest
│   ├── demo_server.py          # 教学演示：lost / leak / ok
│   └── run_demo.sh             # 演示 + 回归
└── evals/evals.json            # 测试用例
scripts/validate_skill.py       # 结构、引用路径、隐私扫描、自检
tests/                          # 从文档抽代码实测的 Java / Node 验证
```

## 致谢与许可

方法思路来自公众号「程序员三木」的《互联网大厂都在使用的现网日志追踪方案 [SpringAop + Logback + MDC]》。本仓库是在它的基础上重新整理、修正和扩展的，没有转载原文正文。

MIT License.
