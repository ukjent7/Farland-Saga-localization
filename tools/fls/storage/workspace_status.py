"""Read workspace state without rebuilding, restoring archives, or installing."""
from importlib import metadata
import json
from pathlib import Path
import re
import sys
import tomllib

from file_hash import sha256 as digest
from path_compat import WORK, local_path
from version_storage import object_path

LANGUAGES = ('zh-CN', 'ko-KR', 'en-US')
# 全新工作区（公开仓库单独检出）的下一步提示；两个入口后续会接入 localize.py。
UNINITIALIZED_HINT = ('全新工作区尚未初始化：缺少 config/storage_policy.json 或 install_report.json。'
                      '请先运行 python tools/localize.py unpack --game <游戏根目录> 解包游戏资源，'
                      '再运行 python tools/localize.py assets freeze 冻结原版素材，'
                      '然后按 README.md 构建候选。')


def load(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def initialized(work=WORK):
    """有存储策略才算初始化；安装记录单独由 installation 字段反映。

    全新工作区跑完 build full 会生成存储策略，此时 active_candidates 已经可读，
    不能因为还没安装过就继续报未初始化。
    """
    return (work / 'config/storage_policy.json').is_file()


def active_stage(language, work=WORK):
    candidates = load(work / 'config/storage_policy.json').get('active_candidates', {})
    if language not in candidates:
        return None
    name = candidates[language]
    if not isinstance(name, str) or not name or '/' in name or '\\' in name or ':' in name or name in ('.', '..'):
        raise ValueError(f'Invalid active candidate for {language}: {name!r}')
    stage = (work / 'staging' / name).resolve()
    if stage.parent != (work / 'staging').resolve():
        raise ValueError(f'Active candidate escapes staging: {name}')
    return stage


def candidate_state(stage):
    report = stage / 'build_report.json'
    if not report.exists():
        return {'stage': stage.name, 'status': 'missing_build_report', 'complete_files': 0, 'archived_files': 0}
    data = load(report)
    archive = load(stage / 'storage_manifest.json').get('files', {}) if (stage / 'storage_manifest.json').exists() else {}
    complete = archived = 0
    missing = []
    for name, item in data.get('files', {}).items():
        path = (stage / name).resolve()
        if not path.is_relative_to(stage.resolve()):
            raise ValueError(f'Candidate path escapes stage: {name}')
        if path.is_file() and path.stat().st_size == item['size']:
            complete += 1
        elif not path.exists() and archive.get(name, {}).get('sha256') == item['patched_sha256'] and archive[name]['size'] == item['size'] and object_path(item['patched_sha256']).is_file():
            archived += 1
        else:
            missing.append(name)
    return {'stage': stage.name, 'version': data.get('version'),
            'status': data.get('build_status', 'candidate_ready' if data.get('verification') and data.get('files') else 'incomplete'),
            'complete_files': complete, 'archived_files': archived,
            'missing_or_wrong_size': missing, 'translated_texts': data.get('translated_texts')}


def active_candidates(work=WORK):
    """按存储策略报告各语言当前候选；尚未构建的语言记为 null。"""
    states = {}
    for language in LANGUAGES:
        stage = active_stage(language, work)
        states[language] = candidate_state(stage) if stage is not None else None
    return states


def status(work=WORK):
    record_path = work / 'install_report.json'
    record = load(record_path) if record_path.exists() else {}
    ready = initialized(work)
    result = {'work': str(work), 'installation': {
                'version': record.get('version'), 'status': record.get('status', 'unrecorded'),
                'timestamp_utc': record.get('timestamp_utc'), 'files': len(record.get('files', {}))},
            'active_candidates': active_candidates(work) if ready else {},
            'archived_snapshots': sum(1 for root in ('staging', 'backups') for _ in (work / root).glob('*/storage_manifest.json')),
            'note': 'Metadata only; run doctor for installed-file hashes and verify for candidate hashes.'}
    if not ready:
        # 全新工作区返回未初始化结构与中文提示，退出码仍为 0。
        result['initialized'] = False
        result['hint'] = UNINITIALIZED_HINT
    return result


def installed_checks(work=WORK):
    """Check every recorded installed file and original backup without writing."""
    record = load(work / 'install_report.json')
    if record.get('status') not in ('installed', 'restored'):
        raise ValueError(f"Unfinished installation state: {record.get('status')}")
    backup = local_path(record['backup_directory']).resolve()
    if not backup.is_relative_to(work.resolve()) or backup == work.resolve():
        raise ValueError('Original backup must be inside work')
    if not record.get('files'):
        raise ValueError('Install record has no files')
    results = []
    for name, item in record['files'].items():
        installed = (work.parent / name).resolve()
        original = (backup / name).resolve()
        if Path(name).is_absolute() or '\\' in name or ':' in name or '..' in Path(name).parts or not installed.is_relative_to(work.parent.resolve()) or not original.is_relative_to(backup):
            raise ValueError(f'Invalid installed file path: {name}')
        expected = item['patched_sha256'] if record['status'] == 'installed' else item['original_sha256']
        results.append({'check': 'installed:' + name, 'ok': digest(installed) == expected})
        results.append({'check': 'original_backup:' + name, 'ok': digest(original) == item['original_sha256']})
    return results


def doctor(work=WORK):
    checks = []

    def check(name, action):
        try:
            detail = action()
            checks.append({'check': name, 'ok': True, 'detail': detail})
        except Exception as error:
            checks.append({'check': name, 'ok': False, 'detail': str(error)})

    def environment():
        project = tomllib.loads((work / 'pyproject.toml').read_text(encoding='utf-8'))
        if sys.version_info[:2] != (3, 14) or Path(sys.prefix).resolve() != (work / '.venv').resolve():
            raise ValueError('Use the project Python 3.14 environment through bash flow.sh')
        versions = {}
        for requirement in project['project']['dependencies']:
            name, expected = requirement.split('==')
            name = re.sub(r'\[.*\]', '', name)
            actual = metadata.version(name)
            if actual != expected:
                raise ValueError(f'{name}: expected {expected}, got {actual}; run uv sync --locked')
            versions[name] = actual
        if not (work / 'uv.lock').is_file():
            raise FileNotFoundError('uv.lock is missing')
        return versions

    def candidate(language):
        result = candidate_state(active_stage(language, work))
        if result['status'] != 'candidate_ready' or result.get('missing_or_wrong_size') or result['complete_files'] + result['archived_files'] == 0:
            raise ValueError(f'Active candidate is incomplete: {result}')
        return result

    check('python_environment', environment)
    for language in LANGUAGES:
        check('active_candidate:' + language, lambda language=language: candidate(language))
    try:
        checks.extend(installed_checks(work))
    except Exception as error:
        checks.append({'check': 'installation', 'ok': False, 'detail': str(error)})
    # 全新工作区把缺失汇总成一项中文失败检查，其余检查行为不变。
    if not initialized(work):
        checks.append({'check': 'workspace_initialized', 'ok': False, 'detail': UNINITIALIZED_HINT})
    return {'ok': all(item['ok'] for item in checks), 'checks': checks,
            'note': 'Candidate contents need verify; archive contents need storage verify.'}
