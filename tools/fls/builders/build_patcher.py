"""Build the self-contained Windows patcher with every language payload embedded.

The release form is a single executable: the payload ZIPs are copied into
patcher/payload/ and pulled in by go:embed, so end users never handle a separate
patch file. Run this after the candidates you want to ship are verified; missing
payloads are packaged on demand.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from package_release import package
from path_compat import REPO, WORK

# 与 patcher/embedded.go 的 languageRank 对应：中、韩、英。
DEFAULT_STAGES = ['zh-CN-unifont08', 'ko-KR-unifont-full07', 'en-US-unifont-full07']
PAYLOAD_DIR = REPO / 'patcher' / 'payload'


def ensure_payloads(stages):
    """Package each stage whose payload is missing, then return the ZIP paths."""
    paths = []
    for stage in stages:
        path = WORK / 'release' / f'farland-patch-{stage}.zip'
        if not path.is_file():
            print(f'打包 {stage} ...', file=sys.stderr)
            package(stage)
        if not path.is_file():
            raise FileNotFoundError(f'找不到补丁载荷 {path}')
        paths.append(path)
    return paths


def stage_payloads(stages):
    """Replace patcher/payload/ with exactly the payloads for the given stages."""
    PAYLOAD_DIR.mkdir(parents=True, exist_ok=True)
    for stale in PAYLOAD_DIR.glob('*.zip'):
        stale.unlink()
    copied = []
    for source in ensure_payloads(stages):
        target = PAYLOAD_DIR / source.name
        shutil.copyfile(source, target)
        copied.append(target)
    return copied


def build(output, stages):
    """Embed the payloads and cross-compile the Windows patcher."""
    stages = list(stages) if stages else list(DEFAULT_STAGES)
    copied = stage_payloads(stages)
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
            'payloads': [path.name for path in copied],
            'payload_bytes': sum(path.stat().st_size for path in copied)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='release/FarlandSagaPatcher-windows-amd64.exe',
                        help='输出路径（相对于 work/），或绝对路径')
    parser.add_argument('--stage', action='append', dest='stages',
                        help='要内嵌的候选目录名，可重复；默认中、韩、英三个')
    args = parser.parse_args()
    result = build(args.output, args.stages or DEFAULT_STAGES)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
