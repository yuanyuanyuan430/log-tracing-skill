package demo.trace;
import java.util.Map;
import org.slf4j.MDC;
import org.springframework.core.task.TaskDecorator;
/** 对照组：原文的装饰器写法（子线程拼线程名、finally 里 MDC.clear()）。 */
public class OriginalMdcTaskDecorator implements TaskDecorator {
    @Override
    public Runnable decorate(Runnable runnable) {
        Map<String, String> contextMap = MDC.getCopyOfContextMap();
        return () -> {
            try {
                if (contextMap != null && !contextMap.isEmpty()) {
                    String logId = contextMap.getOrDefault(TraceIds.TRACE_ID, "");
                    logId += "_" + Thread.currentThread().getName();
                    contextMap.put(TraceIds.TRACE_ID, logId);
                    MDC.setContextMap(contextMap);
                }
                runnable.run();
            } finally {
                MDC.clear();
            }
        };
    }
}
