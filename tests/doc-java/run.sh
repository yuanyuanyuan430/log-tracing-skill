#!/bin/bash
# 验证 references/java-spring.md 里的 Java 代码：原样抽进一个 Spring Boot 项目，编译、启动、并发探测。
# 期望：修正版（/pool、/callerruns?impl=fixed）通过；
#       不传 executor（/common）与原文 MDC.clear() 写法（/callerruns?impl=original）被 verify 抓出来。
# 需要 JDK 17+ 与 Maven；第一次运行会从 Maven 中央仓库下载依赖。
# 用法：bash tests/doc-java/run.sh [工作目录，默认临时目录]
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SKILL="$HERE/../../skills/log-tracing"
T="$SKILL/scripts/tracekit.py"
WORK="${1:-$(mktemp -d)}"

if ! command -v java >/dev/null || ! command -v mvn >/dev/null; then
  echo "doc-java：跳过（没有 java 或 mvn）"; exit 0
fi

mkdir -p "$WORK"
cp -R "$HERE/project/." "$WORK/"
python3 "$HERE/extract.py" "$SKILL/references/java-spring.md" "$WORK" || exit 1
(cd "$WORK" && mvn -q -B -DskipTests package) || { echo "doc-java：编译失败 ✗"; exit 1; }

PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
(cd "$WORK" && exec java -jar target/app.jar --server.port="$PORT" > "$WORK/stdout.txt" 2>&1) &
pid=$!
trap 'kill $pid 2>/dev/null' EXIT
for _ in $(seq 1 90); do
  curl -s -o /dev/null "http://127.0.0.1:$PORT/pool" && break
  sleep 1
done
LOG="$WORK/logs/detail.log"
[ -f "$LOG" ] || { echo "doc-java：服务没有启动 ✗（见 $WORK/stdout.txt）"; exit 1; }

fail=0
check() {  # 名字 路径 每请求行数 期望(pass|fail) [probe 额外参数...]
  local name=$1 path=$2 lines=$3 expect=$4; shift 4
  : > "$LOG"
  python3 "$T" probe --url "http://127.0.0.1:$PORT$path" --count 30 --concurrency 10 --out "$WORK/sent-$name.jsonl" "$@" >/dev/null
  sleep 0.5
  python3 "$T" verify --log "$LOG" --sent "$WORK/sent-$name.jsonl" --lines "$lines" > "$WORK/verify-$name.txt"
  local rc=$?
  local got=pass; [ $rc -ne 0 ] && got=fail
  local mark="✓"; [ "$got" != "$expect" ] && { mark="✗"; fail=1; }
  printf '  %s %-22s 期望 %-4s 实际 %-4s  %s\n' "$mark" "$name" "$expect" "$got" "$(grep '出现行数分布' "$WORK/verify-$name.txt" | sed 's/^ *//')"
}

echo "doc-java：Spring Boot 已启动（端口 ${PORT}），开始并发探测"
check pool-w3c            /pool                       3 pass
check pool-legacy-header  /pool                       3 pass --style header:X-Trace-Id
check common-pool         /common                     2 fail
check callerruns-fixed    "/callerruns?impl=fixed"    5 pass
check callerruns-original "/callerruns?impl=original" 5 fail

if [ $fail -eq 0 ]; then echo "doc-java：结论都符合预期 ✓（工作目录 ${WORK}）"; else echo "doc-java：有结论不符合预期 ✗（工作目录 ${WORK}）"; fi
exit $fail
