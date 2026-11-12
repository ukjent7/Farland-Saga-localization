"""Build the self-contained Windows patcher with the compiled install plan embedded.

The release form mirrors the sequel's: tools/compile_recipe_fs1.py packs the
committed language payloads in recipe/fs1/payload/ into
patcher/plan/saga1/plan.bin (verified against recipe/fs1/plan.lock.json), the
default build embeds it via go:embed, and the cross-compiled Windows
executable needs no other data. Run this after the committed payloads are
reviewed; the compiler never reads the game or the private workspace.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from fls.core.path_compat import REPO, WORK


def compile_plan():
    """Run the plan compiler and return its JSON report."""
    result = subprocess.run(
        [sys.executable, str(REPO / 'tools' / 'compile_recipe_fs1.py')],
        cwd=REPO, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def build(output):
    """Compile the install plan and cross-compile the Windows patcher."""
    plan = compile_plan()
    output = Path(output)
    if not output.is_absolute():
        output = WORK / output
    output.parent.mkdir(parents=True, exist_ok=True)
    command = ['go', 'build', '-trimpath', '-ldflags=-s -w -H windowsgui',
               '-o', str(output), '.']
    # -H windowsgui：GUI 子系统，双击运行不弹控制台；从终端调用子命令时
    # 标准输出仍然可用。
    environment = dict(os.environ, GOOS='windows', GOARCH='amd64', CGO_ENABLED='0')
    print(' '.join(command), file=sys.stderr)
    subprocess.run(command, cwd=REPO / 'patcher', check=True, env=environment)
    return {'output': str(output), 'bytes': output.stat().st_size,
            'languages': plan['languages'], 'versions': plan['versions'],
            'plan_bytes': plan['plan_bytes']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='release/FarlandSagaPatcher-windows-amd64.exe',
                        help='输出路径（相对于 work/），或绝对路径')
    args = parser.parse_args()
    result = build(args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
