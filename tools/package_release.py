"""Package a verified candidate into a distributable patch payload.

The payload is a ZIP holding manifest.json plus SHA-256 addressed diff objects
in the version_storage format: gzip streams whose first line is a JSON header
and whose body is a sequence of <offset, length> records carrying the changed
4 KiB blocks against the pristine original. End users apply the payload with
the standalone patcher under patcher/, so no Python and no private workspace is
needed on their machine. Diff objects are written into the cold store first, so
a release shares objects with the archived candidates instead of duplicating
them; the payload itself only ever leaves the private workspace when the
maintainer attaches it to a GitHub release.
"""
import argparse
import json
import shutil
import zipfile
from pathlib import Path

from candidate import stage_path, verify_candidate
from file_hash import sha256
from path_compat import WORK, local_path
from version_storage import encode, object_path, under_work

PAYLOAD_FORMAT = 1
PAYLOAD_KIND = 'farland-saga-patch'
GAME_TITLE = 'FARLAND SAGA I Saturn Tribute'
RECORD_NAME = 'install_report.json'
# 固定时间戳让同一候选重复打包得到字节一致的 ZIP。
ZIP_TIMESTAMP = (2020, 1, 1, 0, 0, 0)

USAGE_TEMPLATE = """FARLAND SAGA I Saturn Tribute 三语本地化补丁
版本：{version}（{language}）

使用方法
1. 完全退出游戏（Steam 客户端中也请关闭本游戏）。
2. 把 {program} 放进游戏根目录，也就是 Steam 库中的
   "FARLAND SAGA I Saturn Tribute" 文件夹，与 FarlandSaga.exe 同级。
3. 双击运行 {program}，第一屏选语言：简体中文、韩文、English。
4. 进入该语言页面后点「安装」，确认后等待写入与校验完成。

三个语言的补丁都内置在 {program} 里，换语言先点「返回」回到第一屏再选，
程序会自动处理好切换，不需要再找别的文件。

还原原版
打开 {program}，选好语言后点「还原」即可。
原版文件备份在游戏目录的 FarlandSagaPatcher-backup 文件夹中（整个生命周期只有
一个，不随安装次数增加），删除备份前请确认不再需要恢复。

本补丁修改以下文件：
{files}
"""


def load_install_record():
    path = WORK / RECORD_NAME
    if not path.is_file():
        raise FileNotFoundError(f'缺少安装记录 {RECORD_NAME}：差异载荷需要原版备份作为回放基准')
    return json.loads(path.read_text(encoding='utf-8-sig'))


def original_file(name, expected_sha256, record):
    """Locate the pristine original of a game file inside the protected backup."""
    backup = local_path(record['backup_directory']).resolve()
    if not backup.is_relative_to(WORK.resolve()) or backup == WORK.resolve():
        raise ValueError('原版备份必须位于私有工作区内')
    path = backup / name
    if not path.is_file():
        raise FileNotFoundError(f'原版备份缺少 {name}；请先安装一次该候选再打包')
    if sha256(path) != expected_sha256:
        raise ValueError(f'原版备份与候选报告不一致: {name}')
    return path


def build_manifest(stage, report, record):
    """Describe every patched file and encode its diff object against the original."""
    files = []
    cache = set()
    for name, item in sorted(report['files'].items()):
        original = original_file(name, item['original_sha256'], record)
        patched = stage / name
        base = {'path': original.relative_to(WORK).as_posix(),
                'sha256': item['original_sha256'], 'size': original.stat().st_size}
        # encode 写入冷存储并回放校验重建结果；对象按补丁后哈希寻址，与归档共享。
        encode(patched, item['patched_sha256'], base, cache)
        files.append({'path': name,
                      'original_sha256': item['original_sha256'],
                      'original_size': original.stat().st_size,
                      'patched_sha256': item['patched_sha256'],
                      'patched_size': item['size'],
                      'object': 'objects/' + item['patched_sha256'] + '.gz'})
    return {'format': PAYLOAD_FORMAT, 'kind': PAYLOAD_KIND, 'game': GAME_TITLE,
            'version': report.get('version') or stage.name,
            'language': report['language'],
            'files': files}


def zip_member(archive, name, source=None, data=None):
    """Write one member with a fixed timestamp so repeated packaging is reproducible."""
    info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    info.flag_bits |= 0x800  # 文件名按 UTF-8 解释
    if data is None:
        with open(source, 'rb') as stream, archive.open(info, 'w') as target:
            shutil.copyfileobj(stream, target)
    else:
        archive.writestr(info, data)


def write_payload(manifest, output_dir):
    path = output_dir / f"farland-patch-{manifest['version']}.zip"
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        zip_member(archive, 'manifest.json',
                   data=json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
        for entry in manifest['files']:
            zip_member(archive, entry['object'], source=object_path(entry['patched_sha256']))
    return path


def write_bundle(patcher, manifest, output_dir):
    """Bundle the self-contained patcher executable and an end-user readme.

    The patcher embeds every language payload at build time, so the bundle is
    just the one executable plus the readme; users never handle a payload file.
    Windows is the only target: the game is a Windows-only Steam build, so no
    Linux bundle is produced.
    """
    program = 'FarlandSagaPatcher.exe'
    listing = '\n'.join(f"  {entry['path']}" for entry in manifest['files'])
    usage = USAGE_TEMPLATE.format(version=manifest['version'], language=manifest['language'],
                                  program=program, files=listing)
    path = output_dir / f"FarlandSagaPatcher-{manifest['version']}-win64.zip"
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        zip_member(archive, program, source=patcher)
        zip_member(archive, '使用说明.txt', data=usage)
    return path


def package(stage, output=None, patcher=None):
    """Verify a candidate, then write its payload (and optional end-user bundle)."""
    stage = stage_path(stage)
    report = verify_candidate(stage, verify_resources=False)
    if not report.get('language'):
        raise ValueError('候选报告缺少 language，无法打包')
    record = load_install_record()
    manifest = build_manifest(stage, report, record)
    output_dir = under_work(WORK / 'release' if output is None else WORK / output)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = write_payload(manifest, output_dir)
    result = {'payload': str(payload), 'version': manifest['version'], 'language': manifest['language'],
              'files': len(manifest['files']), 'payload_bytes': payload.stat().st_size,
              'patched_bytes': sum(entry['patched_size'] for entry in manifest['files'])}
    if patcher:
        bundle = write_bundle(Path(patcher), manifest, output_dir)
        result['bundle'] = str(bundle)
        result['bundle_bytes'] = bundle.stat().st_size
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', required=True, help='候选目录名（work/staging 下）')
    parser.add_argument('--output', help='输出目录（相对于 work/），默认 work/release')
    parser.add_argument('--patcher', help='补丁器可执行文件；提供时同时生成终端用户整合包')
    args = parser.parse_args()
    result = package(args.stage, args.output, args.patcher)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
