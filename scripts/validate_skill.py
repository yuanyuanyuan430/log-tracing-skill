#!/usr/bin/env python3
"""静态校验 log-tracing skill：结构、frontmatter、引用路径、JSON、脚本语法、隐私泄漏，最后跑 tracekit 自检。

用法：python3 scripts/validate_skill.py
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / 'skills' / 'log-tracing'
REQUIRED = ['SKILL.md', 'manifest.json', 'references/java-spring.md', 'references/node.md',
            'references/standards.md', 'references/investigate.md', 'scripts/tracekit.py',
            'scripts/demo_server.py', 'scripts/run_demo.sh', 'evals/evals.json']
# 不该出现在公开仓库里的东西：本机绝对路径、邮箱、私钥、常见云凭据前缀
LEAKS = [(r'/Users/[A-Za-z]', '本机绝对路径'), (r'/home/[a-z]', '本机绝对路径'),
         (r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}', '邮箱'),
         (r'-----BEGIN [A-Z ]*PRIVATE KEY-----', '私钥'), (r'AKIA[0-9A-Z]{16}', 'AWS key'),
         (r'AIza[0-9A-Za-z_-]{35}', 'Google API key'), (r'gh[pousr]_[A-Za-z0-9]{30,}', 'GitHub token')]

results = []


def check(ok, what, detail=''):
    results.append((ok, what, detail))


for rel in REQUIRED:
    check((SKILL / rel).is_file(), f'必需文件 {rel}')

text = (SKILL / 'SKILL.md').read_text(encoding='utf-8')
m = re.match(r'^---\n(.*?)\n---\n', text, re.S)
check(bool(m), 'SKILL.md 有 frontmatter')
if m:
    fm = dict(re.findall(r'^([\w-]+):\s*(.*)$', m.group(1), re.M))
    check(fm.get('name') == SKILL.name, 'frontmatter name 与目录名一致', fm.get('name', ''))
    desc = fm.get('description', '')
    check(0 < len(desc) <= 1024, 'description 长度 1–1024', str(len(desc)))
    check('<' not in desc and '>' not in desc, 'description 不含尖括号')
check(len(text.splitlines()) < 500, 'SKILL.md 少于 500 行', str(len(text.splitlines())))

for md in [SKILL / 'SKILL.md', *sorted((SKILL / 'references').glob('*.md'))]:
    body = md.read_text(encoding='utf-8')
    for ref in sorted(set(re.findall(r'`((?:references|scripts)/[\w./-]+)`', body))):
        check((SKILL / ref).exists(), f'{md.name} 引用的 {ref} 存在')

for js in [ROOT / 'manifest.json', SKILL / 'manifest.json', SKILL / 'evals' / 'evals.json']:
    try:
        json.loads(js.read_text(encoding='utf-8'))
        check(True, f'JSON 合法 {js.relative_to(ROOT)}')
    except ValueError as e:
        check(False, f'JSON 合法 {js.relative_to(ROOT)}', str(e))
manifest = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))
for s in manifest.get('skills', []):
    check((ROOT / s['path']).is_file(), f'manifest 路径存在 {s["path"]}')

for py in [*SKILL.glob('scripts/*.py'), *ROOT.glob('scripts/*.py'), *ROOT.glob('tests/*/*.py')]:
    try:
        compile(py.read_text(encoding='utf-8'), str(py), 'exec')   # 只检查语法，不写 .pyc
        check(True, f'Python 语法 {py.relative_to(ROOT)}')
    except SyntaxError as e:
        check(False, f'Python 语法 {py.relative_to(ROOT)}', str(e))

skip_dirs = {'.git', '__pycache__', 'target', 'node_modules'}
# bash 会把紧跟在 $VAR 后面的全角字符（如"（"）当成变量名的一部分，set -u 下直接报"未绑定的变量"。
# 这个错在本仓库出现过两次，所以做成检查：中文紧跟变量时必须写成 ${VAR}。
for sh in [*ROOT.rglob('*.sh')]:
    if skip_dirs & set(sh.parts):
        continue
    for no, line in enumerate(sh.read_text(encoding='utf-8').splitlines(), 1):
        if re.search(r'\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7F]', line):
            check(False, f'shell 变量后紧跟非 ASCII 字符 {sh.relative_to(ROOT)}:{no}', line.strip()[:60])
check(True, 'shell 变量写法检查完成')

for f in ROOT.rglob('*'):
    if f.is_dir() or skip_dirs & set(f.parts) or f.suffix in {'.png', '.jpg', '.jar'}:
        continue
    body = f.read_text(encoding='utf-8', errors='ignore')
    for pat, label in LEAKS:
        hit = re.search(pat, body)
        if hit and not (label == '邮箱' and hit.group(0).endswith('@users.noreply.github.com')):
            check(False, f'无{label} {f.relative_to(ROOT)}', hit.group(0)[:40])
check(True, '隐私扫描完成')

r = subprocess.run([sys.executable, str(SKILL / 'scripts' / 'tracekit.py'), 'selftest'], capture_output=True, text=True)
check(r.returncode == 0, 'tracekit selftest', (r.stdout + r.stderr).strip())

bad = [x for x in results if not x[0]]
for ok, what, detail in results:
    if not ok or '--verbose' in sys.argv:
        print(f'{"✓" if ok else "✗"} {what}' + (f'  ({detail})' if detail else ''))
print(f'validate_skill：{len(results) - len(bad)}/{len(results)} 项通过' + (' ✓' if not bad else ' ✗'))
sys.exit(1 if bad else 0)
