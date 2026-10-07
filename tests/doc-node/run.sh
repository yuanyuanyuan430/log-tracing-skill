#!/bin/bash
# 验证 references/node.md 里的 Node 模板：原样抽出第一个 ts 代码块，挂到一个 node:http 服务上，
# 每个请求在 await 前、await 后、定时器回调里各写 1 行日志，并发探测后每个号应恰好 3 行。
# 需要 Node 22.6+（22 需要 --experimental-strip-types，23.6 起默认支持直接运行 .ts）。
# 用法：bash tests/doc-node/run.sh [工作目录，默认临时目录]
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SKILL="$HERE/../../skills/log-tracing"
T="$SKILL/scripts/tracekit.py"
WORK="${1:-$(mktemp -d)}"

if ! command -v node >/dev/null; then echo "doc-node：跳过（没有 node）"; exit 0; fi
major="$(node -p 'process.versions.node.split(".")[0]')"
FLAGS=""
[ "$major" -lt 23 ] && FLAGS="--experimental-strip-types"

mkdir -p "$WORK"
python3 - "$SKILL/references/node.md" "$WORK/trace.ts" <<'EOF' || exit 1
import re, sys
blocks = re.findall(r'```ts\n(.*?)```', open(sys.argv[1], encoding='utf-8').read(), re.S)
assert blocks, 'node.md 里没有 ts 代码块'
open(sys.argv[2], 'w', encoding='utf-8').write(blocks[0])
EOF
cp "$HERE/server.ts" "$WORK/server.ts"

PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
# shellcheck disable=SC2086
(cd "$WORK" && GOOGLE_CLOUD_PROJECT=demo-project PORT="$PORT" exec node $FLAGS server.ts > "$WORK/app.log" 2> "$WORK/err.log") &
pid=$!
trap 'kill $pid 2>/dev/null' EXIT
for _ in $(seq 1 50); do grep -q ready "$WORK/err.log" 2>/dev/null && break; sleep 0.2; done
grep -q ready "$WORK/err.log" || { echo "doc-node：服务没有启动 ✗"; cat "$WORK/err.log"; exit 1; }

python3 "$T" probe --url "http://127.0.0.1:$PORT/" --count 30 --concurrency 10 --out "$WORK/sent.jsonl" >/dev/null
sleep 0.3
if python3 "$T" verify --log "$WORK/app.log" --sent "$WORK/sent.jsonl" --lines 3 > "$WORK/verify.txt"; then
  grep -q '"logging.googleapis.com/trace":"projects/demo-project/traces/' "$WORK/app.log" \
    || { echo "doc-node：缺少 Cloud Logging trace 字段 ✗"; exit 1; }
  echo "doc-node：每个号恰好 3 行，Cloud Logging 字段正确 ✓（Node $(node --version)）"
else
  cat "$WORK/verify.txt"; echo "doc-node：不通过 ✗"; exit 1
fi
