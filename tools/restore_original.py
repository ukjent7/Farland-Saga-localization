"""Restore the protected original files using only Linux/WSL file operations."""
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile

from build_alpha import digest, ROOT, WORK
from process_guard import require_no_linux_game_process
from version_storage import atomic_json, local_path


def preflight(record_path):
    record = json.loads(record_path.read_text(encoding='utf-8-sig'))
    if record.get('status') != 'installed':
        raise ValueError(f"Expected an installed release, found {record.get('status')!r}")
    backup = local_path(record['backup_directory']).resolve()
    if not backup.is_relative_to(WORK.resolve()) or backup == WORK.resolve():
        raise ValueError('Original backup must be inside work')
    entries = []
    for name, item in record.get('files', {}).items():
        relative = Path(name)
        target = (ROOT / relative).resolve()
        original = (backup / relative).resolve()
        if (relative.is_absolute() or '\\' in name or ':' in name or '..' in relative.parts
                or not target.is_relative_to(ROOT.resolve()) or not original.is_relative_to(backup)):
            raise ValueError(f'Invalid installed file path: {name}')
        if digest(original) != item['original_sha256']:
            raise ValueError(f'Original backup hash mismatch: {name}')
        current_hash = digest(target)
        if current_hash not in {item['patched_sha256'], item['original_sha256']}:
            raise ValueError(f'Installed file changed since installation; preserve it before restoring: {name}')
        entries.append((name, target, original, current_hash, item['original_sha256']))
    if not entries:
        raise ValueError('Install record has no files')
    return record, backup, entries


def stage_copy(source, destination, temporary_root):
    fd, temporary = tempfile.mkstemp(prefix=destination.name + '.', suffix='.restore', dir=temporary_root)
    os.close(fd)
    temporary = Path(temporary)
    try:
        shutil.copy2(source, temporary)
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def restore(check_only=False, game_is_closed=False):
    record_path = WORK / 'install_report.json'
    record, backup, entries = preflight(record_path)
    require_no_linux_game_process()
    if check_only:
        return {'status': 'preflight_passed', 'checked_files': len(entries), 'game_files_modified': False}
    if not game_is_closed:
        raise ValueError('WSL cannot inspect Windows processes; close the game and pass --game-is-closed before restoring')

    timestamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    rollback = WORK / 'backups' / ('restore-rollback-' + timestamp)
    rollback.mkdir(parents=True, exist_ok=False)
    rollback_files = {}
    restore_files = {}
    temporary_root = WORK / '.tmp'
    temporary_root.mkdir(parents=True, exist_ok=True)
    try:
        for name, target, original, current_hash, original_hash in entries:
            rollback_path = rollback / name
            rollback_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, rollback_path)
            if digest(rollback_path) != current_hash:
                raise ValueError(f'Could not preserve current installed file: {name}')
            rollback_files[name] = {'sha256': current_hash, 'size': rollback_path.stat().st_size}
            restore_files[name] = stage_copy(original, target, temporary_root)

        atomic_json(rollback / 'manifest.json', {
            'kind': 'restore_rollback', 'timestamp_utc': timestamp,
            'restores_install_record': str(record_path), 'files': rollback_files,
        })
        replaced = []
        try:
            for name, target, original, current_hash, original_hash in entries:
                os.replace(restore_files[name], target)
                replaced.append((name, target, current_hash))
                if digest(target) != original_hash:
                    raise ValueError(f'Restored file hash mismatch: {name}')
            record['status'] = 'restored'
            record['restored_timestamp_utc'] = timestamp
            record['restore_rollback_backup'] = str(rollback)
            record['backup_directory'] = str(backup)
            if record.get('previous_release_backup'):
                record['previous_release_backup'] = str(local_path(record['previous_release_backup']).resolve())
            atomic_json(record_path, record)
        except BaseException:
            for name, target, current_hash in reversed(replaced):
                rollback_copy = stage_copy(rollback / name, target, temporary_root)
                os.replace(rollback_copy, target)
                if digest(target) != current_hash:
                    raise RuntimeError(f'Could not roll back partially restored file: {name}')
            raise
    finally:
        for temporary in restore_files.values():
            temporary.unlink(missing_ok=True)

    return {'status': 'restored', 'restored_files': len(entries),
            'rollback_backup': str(rollback), 'game_launched': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only', action='store_true', help='Check hashes without writing game files')
    parser.add_argument('--game-is-closed', action='store_true',
                        help='Acknowledge that the Windows game is closed; WSL cannot inspect Windows processes')
    args = parser.parse_args()
    result = restore(args.check_only, args.game_is_closed)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
