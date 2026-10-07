package demo.trace;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.Executor;
import java.util.concurrent.ThreadPoolExecutor;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.core.task.TaskDecorator;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class TestController {
    private static final Logger log = LoggerFactory.getLogger(TestController.class);
    private final Executor executor;
    private final ThreadPoolTaskExecutor tinyFixed = tiny("tiny-fixed-", new MdcTaskDecorator());
    private final ThreadPoolTaskExecutor tinyOriginal = tiny("tiny-orig-", new OriginalMdcTaskDecorator());

    public TestController(@Qualifier("taskExecutor") Executor executor) { this.executor = executor; }

    /** 1 个线程、0 队列、CallerRunsPolicy：池子一忙，任务就在请求线程上执行。 */
    private static ThreadPoolTaskExecutor tiny(String prefix, TaskDecorator decorator) {
        ThreadPoolTaskExecutor ex = new ThreadPoolTaskExecutor();
        ex.setCorePoolSize(1); ex.setMaxPoolSize(1); ex.setQueueCapacity(0);
        ex.setThreadNamePrefix(prefix);
        ex.setRejectedExecutionHandler(new ThreadPoolExecutor.CallerRunsPolicy());
        ex.setTaskDecorator(decorator);
        ex.initialize();
        return ex;
    }

    /** 仿原文 /pool：每个请求 3 行。 */
    @GetMapping("/pool")
    public String pool() throws Exception {
        log.info("收到请求");
        CompletableFuture<String> f = CompletableFuture.supplyAsync(() -> { log.info("线程池里面"); sleep(20); return ""; }, executor)
                .thenApplyAsync(v -> { log.info("线程池组合操作"); sleep(20); return v + "1"; }, executor);
        return f.get();
    }

    /** 反例：不传 executor，落在 commonPool。期望 2 行，实际线程池那行没有号。 */
    @GetMapping("/common")
    public String common() throws Exception {
        log.info("收到请求");
        return CompletableFuture.supplyAsync(() -> { log.info("commonPool 里面"); return "ok"; }).get();
    }

    /** CallerRunsPolicy 场景：每个请求 1 + 3 + 1 = 5 行。impl=fixed 用文档的修正版，impl=original 用原文写法。 */
    @GetMapping("/callerruns")
    public String callerRuns(@RequestParam(defaultValue = "fixed") String impl) {
        ThreadPoolTaskExecutor ex = "original".equals(impl) ? tinyOriginal : tinyFixed;
        log.info("收到请求");
        CompletableFuture<?>[] fs = new CompletableFuture<?>[3];
        for (int i = 0; i < 3; i++) fs[i] = CompletableFuture.runAsync(() -> { log.info("小线程池任务"); sleep(30); }, ex);
        CompletableFuture.allOf(fs).join();
        log.info("请求结束");
        return "ok";
    }

    private static void sleep(long ms) { try { Thread.sleep(ms); } catch (InterruptedException e) { Thread.currentThread().interrupt(); } }
}
