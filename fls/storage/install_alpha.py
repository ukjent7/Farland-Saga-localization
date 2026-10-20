"""Install the user-requested alpha with verified original backups and rollback."""
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
import shutil
from fls.core.path_compat import WORK, local_path
from fls.core.env import ROOT, STAGE
from fls.core.file_hash import sha256 as digest
from fls.core.process_guard import require_no_linux_game_process
from fls.storage.version_storage import compact


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', default='alpha')
    parser.add_argument('--check-only', action='store_true', help='Verify readiness and hashes without writing game files or backups')
    parser.add_argument('--game-is-closed', action='store_true',
                        help='Acknowledge that the Windows game is closed; WSL cannot inspect Windows processes')
    args = parser.parse_args()
    stage = (WORK / 'staging' / args.stage).resolve()
    if stage.parent != (WORK / 'staging').resolve():
        parser.error('--stage must name a direct child of work/staging')
    require_no_linux_game_process()
    from fls.storage.candidate import verify_candidate
    report = verify_candidate(stage)
    names = list(report['files'])
    record = WORK / 'install_report.json'
    prior_record = record.read_bytes() if record.exists() else None
    prior = json.loads(prior_record.decode('utf-8-sig')) if prior_record else None
    # Switching to an older release also restores any extra files that release
    # does not patch, so the install record always covers every changed file.
    candidate_paths = {name:stage/name for name in names}
    if prior:
        for name,item in prior['files'].items():
            if name not in report['files']:
                report['files'][name]={'original_sha256':item['original_sha256'],
                                       'patched_sha256':item['original_sha256'],
                                       'size':local_path(prior['backup_directory']).joinpath(name).stat().st_size}
                candidate_paths[name]=local_path(prior['backup_directory'])/name
        names=list(report['files'])
    for name, item in report['files'].items():
        if Path(name).is_absolute() or '..' in Path(name).parts or ':' in name or '\\' in name or not (ROOT/name).resolve().is_relative_to(ROOT.resolve()):
            raise ValueError(f'Invalid install file path: {name}')
        allowed = {item['original_sha256']}
        if prior and name in prior['files']:
            if prior['files'][name]['original_sha256'] != item['original_sha256']:
                raise ValueError(f'Original file identity changed: {name}')
            allowed.add(prior['files'][name]['patched_sha256'])
        if digest(ROOT / name) not in allowed:
            raise ValueError(f'Installed file has changed: {name}')
        if digest(candidate_paths[name]) != item['patched_sha256']:
            raise ValueError(f'Candidate has changed: {name}')
    if args.check_only:
        print(json.dumps({'stage': stage.name, 'version': report.get('version'),
                          'status': 'preflight_passed', 'checked_files': len(names),
                          'game_files_modified': False}, ensure_ascii=False, indent=2))
        return
    if not args.game_is_closed:
        parser.error('WSL cannot inspect Windows processes; close the game and pass --game-is-closed before installing')
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    backup = WORK / 'backups' / (stage.name + '-prior-' + timestamp)
    assert backup.resolve().is_relative_to(WORK.resolve())
    backup.mkdir(parents=True, exist_ok=False)
    for name in names:
        current_hash = digest(ROOT / name)
        (backup/name).parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(ROOT / name, backup / name)
        assert digest(backup / name) == current_hash
    original_backup = local_path(prior['backup_directory']).resolve() if prior else backup
    assert original_backup.is_relative_to(WORK.resolve())
    for name in names:
        if not (original_backup/name).exists():
            assert digest(backup/name)==report['files'][name]['original_sha256']
            (original_backup/name).parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(backup/name,original_backup/name)
        assert digest(original_backup / name) == report['files'][name]['original_sha256']
    manifest = {'version': report.get('version', 'zh-CN alpha01'), 'timestamp_utc': timestamp,
                'backup_directory': str(original_backup), 'previous_release_backup': str(backup),
                'files': report['files'], 'status': 'backed_up'}
    if prior_record:
        (backup / 'previous_install_report.json').write_bytes(prior_record)
    (backup / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    record.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        for name in names:
            shutil.copyfile(candidate_paths[name], ROOT / name)
            assert digest(ROOT / name) == report['files'][name]['patched_sha256'], f'Installed hash mismatch: {name}'
        manifest['status'] = 'installed'
        record.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    except BaseException:
        for name in names:
            shutil.copyfile(backup / name, ROOT / name)
            assert digest(ROOT / name) == digest(backup / name)
        if prior_record:
            record.write_bytes(prior_record)
        else:
            manifest['status'] = 'rolled_back'
            record.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        raise
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    # Installation and rollback are already complete; storage maintenance must
    # never turn a successful installation into a misleading install failure.
    try:
        storage_result = compact(apply=True)
        print(json.dumps({'storage_bytes_reclaimed': storage_result['net_bytes_reclaimed']}))
    except Exception as error:
        print(f'Installation succeeded; storage maintenance pending: {error}')


if __name__ == '__main__':
    main()
