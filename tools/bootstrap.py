"""Bootstrap a private workspace from a purchased game installation; read-only.

``unpack`` decodes the SSF Steam containers and the encrypted disc into
``decoded/`` and ``extracted/steam/`` and stages the analysis executable.
``freeze`` snapshots the original Unity resources and Managed assemblies into
``assets/sources/`` so later candidate builds can verify their inputs. The game
directory is only ever read; the game is never launched.
"""
import argparse
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

from path_compat import WORK, WORKSPACE, local_path
from file_hash import sha256
import disc_decode
import disc_extract
import steam_container

CONTAINERS = ('FarlandSaga.cue', 'FarlandSaga_Steam.ini', 'FarlandSaga.bin', 'Texture.tex')
REQUIRED_FILES = CONTAINERS + ('FarlandSaga.exe',)
UNITY_FILES = ('resources.assets', 'sharedassets0.assets', 'level0',
               'globalgamemanagers', 'globalgamemanagers.assets')
TRACK_SECTORS = 128075
COPY_SIZE = 1024 * 1024
# 与历史冻结归档一致的 deflate 级别；配合固定时间戳保证重复生成的 zip 字节相同。
ZIP_LEVEL = 9
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
UNPACK_NEXT = ['python tools/localize.py assets freeze --game <游戏根目录>',
               'python tools/localize.py extract-unity --check',
               '参考 README.md 继续翻译与构建候选']
FREEZE_NEXT = ['python tools/localize.py extract-unity',
               'python tools/localize.py build-first --output zh-CN-seed01 --game <游戏根目录>',
               'python tools/localize.py status']


def resolve_game_root(value=None):
    """--game 优先，否则取私有工作区同级的安装根。"""
    root = Path(value).expanduser() if value else WORKSPACE
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f'游戏根目录不存在或不是目录：{root}')
    return root


def require_game_files(game, names=REQUIRED_FILES):
    missing = [name for name in names if not (game / name).is_file()]
    if missing:
        raise ValueError('游戏安装缺少必需文件：' + '、'.join(missing)
                         + f'（游戏根：{game}）；请用 --game 指向完整的游戏安装目录')


def work_path(*parts):
    """所有产物必须落在私有工作区内。"""
    target = WORK.joinpath(*parts).resolve()
    if not target.is_relative_to(WORK):
        raise ValueError(f'输出必须位于私有工作区内：{target}')
    return target


def copy_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(source, 'rb') as src, open(target, 'wb') as dst:
        shutil.copyfileobj(src, dst, COPY_SIZE)
    return target.stat().st_size


def same_path(left, right):
    try:
        return left.samefile(right)
    except OSError:
        return left == right


def install_analysis_exe(game, force):
    source = game / 'FarlandSaga.exe'
    target = work_path('analysis', 'FarlandSaga.exe')
    entry = {'step': 'analysis_exe', 'source': str(source), 'output': str(target)}
    if target.is_file():
        if sha256(source) == sha256(target):
            entry['status'] = 'unchanged'
            entry['bytes'] = target.stat().st_size
            return entry
        if not force:
            raise ValueError(f'{target} 已存在且与游戏可执行文件不一致；'
                             '确认游戏安装未被修改后加 --force 覆盖，或先删除该文件')
        entry['status'] = 'replaced'
    else:
        entry['status'] = 'copied'
    entry['bytes'] = copy_file(source, target)
    return entry


def unpack(game=None, force=False):
    game = resolve_game_root(game)
    require_game_files(game)
    guard_original(game, force)
    exe = game / 'FarlandSaga.exe'
    steps = []
    for name in CONTAINERS:
        source = game / name
        output = work_path('decoded', name)
        steam_container.convert(source, output, exe, name)
        steps.append({'step': 'steam_container', 'source': str(source), 'output': str(output),
                      'bytes': output.stat().st_size})
    track_source = work_path('decoded', 'FarlandSaga.bin')
    track = work_path('decoded', 'FarlandSaga_track1.bin')
    disc_decode.decode(track_source, track, exe, TRACK_SECTORS, None)
    steps.append({'step': 'disc_decode', 'source': str(track_source), 'output': str(track),
                  'bytes': track.stat().st_size})
    extracted = work_path('extracted', 'steam')
    report = disc_extract.extract(track, extracted)
    # 指向数据轨之外音轨的条目不会落盘，只统计实际写出的文件。
    written = [entry for entry in report['files'] if entry.get('sha256')]
    steps.append({'step': 'disc_extract', 'source': str(track), 'output': str(extracted),
                  'files': len(report['files']), 'bytes': sum(entry['size'] for entry in written)})
    steps.append(install_analysis_exe(game, force))
    for name in ('.tmp', 'staging', 'config'):
        work_path(name).mkdir(parents=True, exist_ok=True)
    summary = {'operation': 'unpack', 'game_root': str(game), 'work': str(WORK),
               'steps': steps, 'next': UNPACK_NEXT}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def installed_candidate_record():
    """install_report.json 记录过补丁安装时返回该记录，否则返回 None。"""
    path = WORK / 'install_report.json'
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding='utf-8-sig'))
    except json.JSONDecodeError as error:
        raise ValueError(f'无法解析 {path}：{error}；请先修复或移除该安装记录')
    backup = record.get('backup_directory')
    if not record.get('files') or not isinstance(backup, str) or not backup:
        return None
    try:
        backup_path = local_path(backup).resolve()
    except OSError:
        return None
    if not backup_path.is_relative_to((WORK / 'backups').resolve()):
        return None
    return record


def guard_original(game, force):
    """游戏目录安装过补丁时拒绝解包与冻结，除非显式 --force。

    已打补丁的安装解包出来的是补丁后的资源，会覆盖私有工作区里的原始解包
    产物与冻结原版，后续候选就建立在错误的基础上了。
    """
    record = installed_candidate_record()
    if record is None or not same_path(game, WORKSPACE):
        return record
    if not force:
        raise ValueError('游戏目录已被补丁修改（install_report.json 记录了一次安装）。'
                         '请先恢复原版：bash work/flow.sh restore-original --game-is-closed，'
                         '或用 --game 指向一份干净的游戏安装，或加 --force 继续处理当前文件')
    print(json.dumps({'warning': '游戏目录已被补丁修改，按 --force 继续处理当前文件',
                      'install_report': str(WORK / 'install_report.json')}, ensure_ascii=False),
          file=sys.stderr)
    return record


def unity_version(data_dir):
    """从 globalgamemanagers 头部读取 Unity 版本号。"""
    with (data_dir / 'globalgamemanagers').open('rb') as stream:
        header = stream.read(0x200)
    match = re.search(rb'\d{4}\.\d+\.\d+[a-zA-Z]+\d*', header)
    if not match:
        raise ValueError(f'无法从 {data_dir}/globalgamemanagers 识别 Unity 版本号')
    return match.group().decode('ascii')


def build_managed_archive(source_dir, output):
    """把 Managed/*.dll 打成字节可复现的 zip；source.json 最后写入。"""
    dlls = sorted((path for path in source_dir.glob('*.dll') if path.is_file()),
                  key=lambda path: path.name.casefold())
    if not dlls:
        raise ValueError(f'未在 {source_dir} 找到 Managed DLL；请确认游戏安装完整')
    rows = [{'archive_path': 'Managed/' + path.name,
             'source_path': 'S-Tribute_Data/Managed/' + path.name,
             'size': path.stat().st_size, 'sha256': sha256(path)} for path in dlls]
    manifest = {'schema_version': 1, 'unity_version': unity_version(source_dir.parent),
                'source_directory': 'S-Tribute_Data/Managed', 'files': rows}
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path, row in zip(dlls, rows):
            info = zipfile.ZipInfo(row['archive_path'], date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.compress_level = ZIP_LEVEL
            info.external_attr = 0o100644 << 16
            with path.open('rb') as src, archive.open(info, 'w') as dst:
                shutil.copyfileobj(src, dst, COPY_SIZE)
        payload = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        info = zipfile.ZipInfo('source.json', date_time=ZIP_EPOCH)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.compress_level = ZIP_LEVEL
        info.external_attr = 0o100644 << 16
        archive.writestr(info, payload)
    return manifest


def freeze(game=None, force=False):
    game = resolve_game_root(game)
    require_game_files(game, ('FarlandSaga.exe', 'Texture.tex'))
    guard_original(game, force)
    steps = []
    # 冻结的是 SSF 容器本身：container_atlases.decode_container 需要 2048 字节头加
    # 变换后的载荷，补丁配方里的 original_sha256 钉的也是这个容器的哈希。
    texture = work_path('assets', 'sources', 'Texture.tex')
    steps.append({'step': 'texture_container', 'source': str(game / 'Texture.tex'),
                  'output': str(texture), 'bytes': copy_file(game / 'Texture.tex', texture),
                  'sha256': sha256(texture)})
    data_dir = game / 'S-Tribute_Data'
    missing = [name for name in UNITY_FILES if not (data_dir / name).is_file()]
    if missing:
        raise ValueError('游戏 S-Tribute_Data 缺少 Unity 原版文件：' + '、'.join(missing)
                         + f'（目录：{data_dir}）')
    unity_dir = work_path('assets', 'sources', 'unity', 'S-Tribute_Data')
    for name in UNITY_FILES:
        source = data_dir / name
        target = unity_dir / name
        copy_file(source, target)
        steps.append({'step': 'unity_source', 'source': str(source), 'output': str(target),
                      'bytes': target.stat().st_size, 'sha256': sha256(target)})
    archive = work_path('assets', 'sources', 'unity-managed.zip')
    manifest = build_managed_archive(data_dir / 'Managed', archive)
    steps.append({'step': 'unity_managed_zip', 'source': str(data_dir / 'Managed'),
                  'output': str(archive), 'bytes': archive.stat().st_size,
                  'files': len(manifest['files']), 'sha256': sha256(archive)})
    record = {f'S-Tribute_Data/{name}': sha256(unity_dir / name) for name in UNITY_FILES}
    record['../unity-managed.zip'] = sha256(archive)
    record_path = work_path('assets', 'sources', 'unity', 'unity_original_files.json')
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    steps.append({'step': 'unity_original_files', 'output': str(record_path),
                  'bytes': record_path.stat().st_size})
    summary = {'operation': 'freeze', 'game_root': str(game), 'work': str(WORK),
               'unity_version': manifest['unity_version'], 'steps': steps, 'next': FREEZE_NEXT}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def _utf8_stdout():
    """命令行输出中文；测试里重定向的 stdout 没有 reconfigure。"""
    reconfigure = getattr(sys.stdout, 'reconfigure', None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding='utf-8')
    except (ValueError, OSError):
        pass


def main(argv=None):
    _utf8_stdout()
    parser = argparse.ArgumentParser(prog='bootstrap.py')
    commands = parser.add_subparsers(dest='operation', required=True)
    unpack_command = commands.add_parser('unpack', help='解包 SSF 容器与加密光盘到 decoded/ 与 extracted/steam')
    unpack_command.add_argument('--game', help='游戏安装根目录；默认为私有工作区同级目录')
    unpack_command.add_argument('--force', action='store_true', help='覆盖已存在且不一致的分析用可执行文件')
    freeze_command = commands.add_parser('freeze', help='冻结原版 Unity 资源与 Managed 程序集到 assets/sources/')
    freeze_command.add_argument('--game', help='游戏安装根目录；默认为私有工作区同级目录')
    freeze_command.add_argument('--force', action='store_true', help='即使游戏目录已被补丁修改也继续冻结')
    args = parser.parse_args(argv)
    try:
        if args.operation == 'unpack':
            unpack(args.game, args.force)
        else:
            freeze(args.game, args.force)
    except (ValueError, OSError) as error:
        print(f'错误：{error}', file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == '__main__':
    main()
