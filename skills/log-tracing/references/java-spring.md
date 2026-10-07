# Java / Spring 接入

目录
1. 先选方案
2. 原文方案（SpringAOP + Logback + MDC）与它的坑
3. 修正后的手写方案（可直接用）
4. Spring Boot 3：Micrometer Tracing
5. 验证

## 1. 先选方案

| 情况 | 选择 |
|---|---|
| Spring Boot 3 新项目，可以加依赖 | 第 4 节 Micrometer Tracing：少写代码，传播按 W3C 标准，以后接 Zipkin / OpenTelemetry 不用改 |
| Spring Boot 2、不想加依赖、或者在学原理 | 第 3 节手写方案（原文思路 + 修正） |
| 已经有原文那样的实现 | 对照第 2 节逐条检查，再跑验证 |

## 2. 原文方案与它的坑

原文（公众号「程序员三木」《互联网大厂都在使用的现网日志追踪方案 [SpringAop + Logback + MDC]》）的五步：

1. `logback.xml` 的 pattern 里加 `[%X{logId}]`，把 MDC 里的 logId 打进每行日志。
2. `MdcAspect`：用 `@Around` 切所有 `@GetMapping`、`@PostMapping`、`@PutMapping`、`@DeleteMapping` 方法。从请求头 `logId` 读上游的号，或者用 UUID 生成一个，`MDC.put` 放进去，`finally` 里 `MDC.remove`。
3. `MDCTaskDecorator implements TaskDecorator`：提交任务时用 `MDC.getCopyOfContextMap()` 拷贝父线程的上下文，在子线程里 `MDC.setContextMap`，`finally` 里 `MDC.clear()`。
4. `PoolConfig`：建一个 `ThreadPoolTaskExecutor`，执行 `setTaskDecorator(new MDCTaskDecorator())`，线程名前缀设为 `mdc-trace-`。
5. 业务代码里调用 `CompletableFuture.supplyAsync(..., executor)` 时把这个线程池传进去。

原理没问题，照抄会踩这几个坑：

- **入口位置太靠后。** AOP 只切 Controller 方法，过滤器、Spring Security、全局异常处理、`/error`、404 打的日志都在切面外面，没有号。入口应该放在最先执行的 Servlet Filter。
- **生成号那一行有优先级 bug。** 原文写的是 `logId = isNotBlank(logId) ? (logId + "_") : "" + UUID.randomUUID()...`。`+` 比 `?:` 先计算，有上游号时结果是 `abc_`，并不会追加新号。按作者原意应该写成 `(isNotBlank(logId) ? logId + "_" : "") + UUID...`。更好的做法是：上游号合法就原样沿用，不拼接。拼接以后号就变了，上下游按同一个号搜不到对方的日志。
- **子线程改号。** 原文的装饰器在子线程里拼上 `_线程名`。这样按号精确搜索时，子线程的日志就搜不到了。线程名本来就在 `%thread` 里，号要保持原值。
- **`finally` 里直接 `MDC.clear()`。** 如果线程池满了，拒绝策略是 `CallerRunsPolicy`，任务会在提交它的请求线程上执行，`clear()` 会把请求线程自己的上下文清空，之后这个请求剩下的日志全都没有号。应该在 `finally` 里恢复执行任务之前的上下文。
- **自定义头名 `logId`。** 各服务、网关、前端都得约定同一个名字。直接用 W3C 标准的 `traceparent`，现成的网关和 SDK 都认识它。
- **少了这几个落点：** 响应头回传号、调下游时透传、记录 uid。见第 3 节。

## 3. 修正后的手写方案

### 3.1 logback.xml

```xml
<configuration scan="true" scanPeriod="15 seconds">
  <property name="LOG_DIR" value="logs"/>
  <property name="LOG_FILE" value="detail.log"/>
  <!-- %X{key:-} 取不到时输出空串；traceId、uid 两个键与代码里的常量一致 -->
  <property name="PATTERN" value="%d{HH:mm:ss.SSS} [%thread] %-5level %logger{36} [%X{traceId:-}] [%X{uid:-}] - %msg%n"/>

  <appender name="STDOUT" class="ch.qos.logback.core.ConsoleAppender">
    <encoder><pattern>${PATTERN}</pattern></encoder>
  </appender>

  <appender name="FILE" class="ch.qos.logback.core.rolling.RollingFileAppender">
    <file>${LOG_DIR}/${LOG_FILE}</file>
    <rollingPolicy class="ch.qos.logback.core.rolling.TimeBasedRollingPolicy">
      <!-- 原文按分钟滚动只是演示；生产按天滚动并压缩 -->
      <fileNamePattern>${LOG_DIR}/${LOG_FILE}.%d{yyyy-MM-dd}.gz</fileNamePattern>
      <maxHistory>30</maxHistory>
      <totalSizeCap>1GB</totalSizeCap>
    </rollingPolicy>
    <encoder><pattern>${PATTERN}</pattern></encoder>
  </appender>

  <root level="INFO">
    <appender-ref ref="STDOUT"/>
    <appender-ref ref="FILE"/>
  </root>
</configuration>
```

日志要进 Cloud Logging、ELK 这类平台时，改用 JSON encoder（如 logstash-logback-encoder），MDC 会自动变成字段。上 Cloud Logging 时，要额外输出 `logging.googleapis.com/trace` 字段，见 `standards.md`。

### 3.2 号的读取与生成

```java
public final class TraceIds {
    public static final String TRACE_ID = "traceId";
    public static final String UID = "uid";
    private static final Pattern TRACEPARENT =
            Pattern.compile("^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$");
    private static final Pattern HEX32 = Pattern.compile("^[0-9a-f]{32}$");
    private static final String ZERO = "0".repeat(32);

    private TraceIds() {}

    /** 优先沿用上游的号；没有或不合法才生成。不拼接、不改写。 */
    public static String fromRequest(HttpServletRequest req) {
        String tp = lower(req.getHeader("traceparent"));
        if (tp != null) {
            Matcher m = TRACEPARENT.matcher(tp);
            if (m.matches() && !ZERO.equals(m.group(1))) return m.group(1);
        }
        String legacy = lower(req.getHeader("X-Trace-Id"));   // 过渡期兼容旧头，可删
        if (legacy != null && HEX32.matcher(legacy).matches()) return legacy;
        return newTraceId();
    }

    /** 32 位小写十六进制，与 W3C trace-id 兼容（带横线的 36 位 UUID 不兼容）。 */
    public static String newTraceId() {
        return UUID.randomUUID().toString().replace("-", "");
    }

    /** 调下游时用：同一个 trace-id，新的 span-id。 */
    public static String traceparent(String traceId) {
        long span;
        do { span = ThreadLocalRandom.current().nextLong(); } while (span == 0);
        return "00-" + traceId + "-" + String.format("%016x", span) + "-01";
    }

    private static String lower(String s) {
        return s == null ? null : s.trim().toLowerCase(Locale.ROOT);
    }
}
```

### 3.3 入口：最先执行的 Filter（替代原文的 AOP 切面）

```java
@Component
@Order(Ordered.HIGHEST_PRECEDENCE)
public class TraceFilter extends OncePerRequestFilter {
    @Override
    protected void doFilterInternal(HttpServletRequest req, HttpServletResponse res, FilterChain chain)
            throws ServletException, IOException {
        String traceId = TraceIds.fromRequest(req);
        MDC.put(TraceIds.TRACE_ID, traceId);
        res.setHeader("X-Trace-Id", traceId);          // 用户报错时能直接报这个号
        try {
            chain.doFilter(req, res);
        } finally {
            MDC.remove(TraceIds.TRACE_ID);              // 线程会复用：不清理就会串号
            MDC.remove(TraceIds.UID);
        }
    }
}
```

如果坚持用 AOP 切面，至少要修掉第 2 节说的优先级 bug，并且清楚切面之外的日志不会带号。

### 3.4 身份：鉴权之后放 uid

```java
@Component   // 不加 @Order：默认最低优先级，排在 Spring Security 过滤器链之后
public class UidFilter extends OncePerRequestFilter {
    @Override
    protected void doFilterInternal(HttpServletRequest req, HttpServletResponse res, FilterChain chain)
            throws ServletException, IOException {
        Authentication auth = SecurityContextHolder.getContext().getAuthentication();
        if (auth != null && auth.isAuthenticated() && auth.getPrincipal() instanceof AppUser user) {
            MDC.put(TraceIds.UID, user.getId());       // 只放不透明 ID，不放邮箱、手机号
        }
        chain.doFilter(req, res);                      // 清理由 TraceFilter 的 finally 统一做
    }
}
```

没用 Spring Security 的话，可以在 `HandlerInterceptor.preHandle` 里，拿到登录用户后再 put。

### 3.5 线程池：装饰器（修正版）

```java
public class MdcTaskDecorator implements TaskDecorator {
    @Override
    public Runnable decorate(Runnable runnable) {
        Map<String, String> captured = MDC.getCopyOfContextMap();   // 提交任务那一刻，父线程的上下文
        return () -> {
            Map<String, String> previous = MDC.getCopyOfContextMap(); // 执行线程原来的上下文
            if (captured != null) MDC.setContextMap(captured); else MDC.clear();
            try {
                runnable.run();
            } finally {
                // 恢复而不是 clear：CallerRunsPolicy 时任务跑在请求线程上，clear 会清掉请求自己的号
                if (previous != null) MDC.setContextMap(previous); else MDC.clear();
            }
        };
    }
}

@Configuration
@EnableAsync
public class PoolConfig {
    @Bean("taskExecutor")
    public ThreadPoolTaskExecutor taskExecutor() {
        ThreadPoolTaskExecutor ex = new ThreadPoolTaskExecutor();
        ex.setCorePoolSize(5);
        ex.setMaxPoolSize(10);
        ex.setQueueCapacity(100);
        ex.setThreadNamePrefix("mdc-trace-");
        ex.setTaskDecorator(new MdcTaskDecorator());
        ex.initialize();
        return ex;
    }
}
```

使用时注意：

- `CompletableFuture.supplyAsync(task, executor)`、`thenApplyAsync(fn, executor)` 都必须把 executor 传进去。不传的话，任务跑在 `ForkJoinPool.commonPool()` 上，不经过装饰器。`parallelStream()` 也是这个问题。
- `@Async` 默认用名为 `taskExecutor` 的 bean，也可以显式写成 `@Async("taskExecutor")`。Spring Boot 自动配置的执行器会自动套用容器里唯一的 `TaskDecorator` bean；自己 new 的线程池（包括第三方库内部的线程池）要手动设置装饰器，或者用 `MDC.getCopyOfContextMap()` 手动传进去。

### 3.6 出口：调下游、发消息时透传

```java
// RestTemplate
@Bean
RestTemplate restTemplate(RestTemplateBuilder builder) {
    return builder.additionalInterceptors((request, body, execution) -> {
        String t = MDC.get(TraceIds.TRACE_ID);
        if (t != null) request.getHeaders().set("traceparent", TraceIds.traceparent(t));
        return execution.execute(request, body);
    }).build();
}

// OpenFeign
@Bean
RequestInterceptor traceHeader() {
    return template -> {
        String t = MDC.get(TraceIds.TRACE_ID);
        if (t != null) template.header("traceparent", TraceIds.traceparent(t));
    };
}
```

MQ：生产者把 `traceparent` 写进消息头。消费者处理每条消息时，先从消息头恢复号（没有就生成新号），放进 MDC，`finally` 里移除。定时任务同理：每次执行生成一个新号。

## 4. Spring Boot 3：Micrometer Tracing

加上依赖 `spring-boot-starter-actuator` 和 `io.micrometer:micrometer-tracing-bridge-otel`（或 `micrometer-tracing-bridge-brave`）之后：

- 入口会自动读写 W3C `traceparent`，MDC 里自动有 `traceId` 和 `spanId`。
- 用自动配置的 `RestTemplateBuilder`、`RestClient.Builder`、`WebClient.Builder` 建出来的客户端，会自动透传。自己 `new RestTemplate()` 的不会。
- 日志关联：Spring Boot 3.2 起，可以用 `logging.pattern.correlation` 配置日志里显示的关联字段，自定义 pattern 时也可以直接写 `%X{traceId}`。
- 线程池：Spring Framework 6.1 提供 `ContextPropagatingTaskDecorator`（需要 `io.micrometer:context-propagation` 依赖），设到执行器上即可。
- uid 仍然要自己放：可以像 3.4 那样 `MDC.put`，或者用 Micrometer 的 baggage。

具体的属性名和行为以项目实际使用的 Spring Boot 版本的官方文档为准，接入后一样要跑第 5 节的验证。

## 5. 验证

先确定被测接口每处理一个请求会写几行日志，记为 N。以原文的 `/pool` 接口为例：入口 1 行、"线程池里面" 1 行、"线程池组合操作" 1 行，N=3。不确定的话，先手动请求一次，数一数日志。然后：

```bash
python3 <skill>/scripts/tracekit.py probe --url http://localhost:8080/pool --method POST --count 20 --concurrency 8 --out sent.jsonl
python3 <skill>/scripts/tracekit.py verify --log logs/detail.log --sent sent.jsonl --lines 3
```

每个号恰好出现 N 行才算通过。少了说明线程池或某一步丢了上下文；多了说明串号。用 `summary` 看没有号的日志行集中在哪个线程池（如 `mdc-trace-N`）。
