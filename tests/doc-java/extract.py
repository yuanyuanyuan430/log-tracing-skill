"""从 java-spring.md 原样抽出代码块，组装成可编译的 Spring Boot 项目，用来验证文档代码。"""
import re, sys
from pathlib import Path

doc = Path(sys.argv[1]).read_text(encoding='utf-8')
root = Path(sys.argv[2])
java = re.findall(r'```java\n(.*?)```', doc, re.S)
xml = re.findall(r'```xml\n(.*?)```', doc, re.S)
assert len(java) >= 5, len(java)
pkg = 'demo.trace'
src = root / 'src/main/java/demo/trace'
res = root / 'src/main/resources'
src.mkdir(parents=True, exist_ok=True); res.mkdir(parents=True, exist_ok=True)

IMPORTS = """package demo.trace;

import feign.RequestInterceptor;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ThreadLocalRandom;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.slf4j.MDC;
import org.springframework.boot.web.client.RestTemplateBuilder;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.core.task.TaskDecorator;
import org.springframework.scheduling.annotation.EnableAsync;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestTemplate;
import org.springframework.web.filter.OncePerRequestFilter;

"""
# 文档里的代码块原样写进去（一个块一个文件；块 5 是几个 @Bean 方法，包进一个配置类）
(src / 'TraceIds.java').write_text(IMPORTS + java[0], encoding='utf-8')
(src / 'TraceFilter.java').write_text(IMPORTS + java[1], encoding='utf-8')
(src / 'UidFilter.java').write_text(IMPORTS + java[2], encoding='utf-8')
dec, pool = java[3].split('@Configuration', 1)
(src / 'MdcTaskDecorator.java').write_text(IMPORTS + dec, encoding='utf-8')
(src / 'PoolConfig.java').write_text(IMPORTS + '@Configuration' + pool, encoding='utf-8')
(src / 'ClientConfig.java').write_text(IMPORTS + '@Configuration\npublic class ClientConfig {\n' + java[4] + '\n}\n', encoding='utf-8')
(res / 'logback.xml').write_text(xml[0], encoding='utf-8')
print('blocks', len(java), 'xml', len(xml))
