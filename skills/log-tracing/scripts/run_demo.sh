#!/bin/bash
# 演示 + 回归：依次以 lost / leak / ok 三种模式启动 demo_server，
# 用 tracekit 并发打 20 个请求，核对 verify 结论是否符合预期：
#   lost → 不通过（行数偏少）  leak → 不通过（串号）  ok → 通过
# 用法：bash run_demo.sh [工作目录，默认临时目录]
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="${1:-$(mktemp -d)}"
mkdir -p "$WORK"
fail=0

python3 "$HERE/tracekit.py" selftest || exit 1

for mode in lost leak ok; do
  log="$WORK/demo-$mode.log"; sent="$WORK/sent-$mode.jsonl"; : > "$log"
  # 每轮现找一个空闲端口，避免探测打到本机别的服务上
  PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
  python3 "$HERE/demo_server.py" --mode "$mode" --port "$PORT" --log "$log" >/dev/null 2>&1 &
  pid=$!
  for _ in $(seq 1 50); do
    grep -q "demo started" "$log" 2>/dev/null && break
    sleep 0.1
  done
  if ! kill -0 "$pid" 2>/dev/null || ! grep -q "demo started" "$log"; then
    echo "!! demo_server（mode=${mode}）没能启动"; fail=1; continue
  fi
  : > "$log"   # 只看 probe 产生的日志
  echo; echo "======== mode=$mode ========"
  python3 "$HERE/tracekit.py" probe --url "http://127.0.0.1:$PORT/work" --count 20 --concurrency 8 --out "$sent"
  sleep 0.3
  python3 "$HERE/tracekit.py" verify --log "$log" --sent "$sent" --lines 2
  rc=$?
  kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
  if [ "$mode" = ok ] && [ $rc -ne 0 ]; then echo "!! 期望 ok 通过，实际不通过"; fail=1; fi
  if [ "$mode" != ok ] && [ $rc -eq 0 ]; then echo "!! 期望 $mode 不通过，实际通过"; fail=1; fi
done

echo
if [ $fail -eq 0 ]; then
  echo "run_demo：三种模式的结论都符合预期 ✓（日志在 ${WORK}）"
else
  echo "run_demo：有模式的结论不符合预期 ✗（日志在 ${WORK}）"
fi
exit $fail
