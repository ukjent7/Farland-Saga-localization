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


def load(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def active_stage(language, work=WORK):
    name = load(work / 'config/storage_policy.json')['active_candidates'][language]
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


def status(work=WORK):
    record_path = work / 'install_report.json'
    record = load(record_path) if record_path.exists() else {}
    return {'work': str(work), 'installation': {
                'version': record.get('version'), 'status': record.get('status', 'unrecorded'),
                'timestamp_utc': record.get('timestamp_utc'), 'files': len(record.get('files', {}))},
            'active_candidates': {language: candidate_state(active_stage(language, work)) for language in LANGUAGES},
            'archived_snapshots': sum(1 for root in ('staging', 'backups') for _ in (work / root).glob('*/storage_manifest.json')),
            'note': 'Metadata only; run doctor for installed-file hashes and verify for candidate hashes.'}


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
            raise ValueError('Use the project Python 3.14 environment through bash work/flow.sh')
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
    return {'ok': all(item['ok'] for item in checks), 'checks': checks,
            'note': 'Candidate contents need verify; archive contents need storage verify.'}
