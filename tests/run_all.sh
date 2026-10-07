#!/bin/bash
# 一条命令跑完全部验证：静态校验 → 演示三种模式 → Node 模板 → Java 代码（没有 JDK/Maven 时自动跳过）
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
fail=0
run() { echo; echo "──── $1"; shift; "$@" || fail=1; }
run "静态校验"        python3 "$ROOT/scripts/validate_skill.py"
run "演示：丢号/串号/修好" bash "$ROOT/skills/log-tracing/scripts/run_demo.sh" "$WORK/demo"
run "Node 模板"       bash "$ROOT/tests/doc-node/run.sh" "$WORK/node"
run "Java 代码"       bash "$ROOT/tests/doc-java/run.sh" "$WORK/java"
echo
if [ $fail -eq 0 ]; then echo "run_all：全部通过 ✓"; else echo "run_all：有失败 ✗（工作目录 ${WORK}）"; fi
exit $fail
