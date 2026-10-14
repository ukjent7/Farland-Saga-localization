"""Fast entry-point and install-preflight checks; all fixtures stay inside work."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO/'tools'))
import localize
import workspace_status as state
import path_compat

WORK = path_compat.WORK


class EntryPoints(unittest.TestCase):
    def test_saved_workspace_case_alias(self):
        root = path_compat.WORKSPACE
        alias = Path(str(root).swapcase()) / 'work/backups/original'
        with patch.object(Path, 'samefile', return_value=True):
            self.assertEqual(path_compat.local_path(alias), root / 'work/backups/original')
        with patch.object(Path, 'samefile', return_value=False):
            self.assertEqual(path_compat.local_path(alias), alias)
        external = Path('/unrelated/work/backups/original')
        self.assertEqual(path_compat.local_path(external), external)

    def test_help_without_site_packages(self):
        result = subprocess.run([sys.executable, '-S', str(REPO/'tools/localize.py'), '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('doctor', result.stdout)

    @unittest.skipUnless((path_compat.WORK/'flow.sh').is_file(), '私有工作区入口脚本不存在，公开仓库单独检出时跳过')
    def test_wrapper_from_another_directory(self):
        result = subprocess.run(['bash', str(WORK/'flow.sh'), 'status'], cwd=WORK/'.tmp', capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['work'], str(WORK))
        self.assertEqual(set(data['active_candidates']), set(state.LANGUAGES))

    @unittest.skipUnless((path_compat.WORK/'config/storage_policy.json').is_file(), '私有工作区配置不存在，公开仓库单独检出时跳过')
    def test_installer_selection_and_check_only(self):
        with patch.object(sys, 'argv', ['localize.py', 'install', '--language', 'ko-KR', '--check-only']), patch.object(localize, 'invoke') as invoke:
            localize.main()
        invoke.assert_called_once_with('install_alpha', ['--stage', state.active_stage('ko-KR').name, '--check-only'])

    def test_install_requires_exactly_one_source(self):
        for options in ([], ['--stage', 'alpha04', '--language', 'ko-KR']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                localize.parser().parse_args(['install', *options])
            self.assertEqual(error.exception.code, 2)

    def test_doctor_failure_returns_nonzero(self):
        with patch.object(sys, 'argv', ['localize.py', 'doctor']), patch.object(state, 'doctor', return_value={'ok': False}), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as error:
            localize.main()
        self.assertEqual(error.exception.code, 1)


class PreflightSafety(unittest.TestCase):
    def setUp(self):
        (WORK/'.tmp').mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=WORK/'.tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root/'work'
        self.stage = self.work/'staging/release'
        self.stage.mkdir(parents=True)
        self.backup = self.work/'backups/original'
        self.backup.mkdir(parents=True)
        (self.root/'sample.bin').write_bytes(b'original')
        (self.backup/'sample.bin').write_bytes(b'original')
        (self.stage/'sample.bin').write_bytes(b'patched!')
        self.report = {'version': 'fixture', 'build_status': 'candidate_ready', 'files': {'sample.bin': {
            'original_sha256': state.digest(self.root/'sample.bin'),
            'patched_sha256': state.digest(self.stage/'sample.bin'), 'size': 8}}}
        (self.stage/'build_report.json').write_text(json.dumps(self.report))
        record = {'status': 'restored', 'backup_directory': str(self.backup), 'files': self.report['files']}
        (self.work/'install_report.json').write_text(json.dumps(record))

    def test_installed_drift_and_restored_state(self):
        self.assertTrue(all(item['ok'] for item in state.installed_checks(self.work)))
        (self.root/'sample.bin').write_bytes(b'changed!')
        self.assertFalse(state.installed_checks(self.work)[0]['ok'])

    def test_record_cannot_escape_backup(self):
        record = state.load(self.work/'install_report.json')
        record['files'] = {'../outside.bin': next(iter(record['files'].values()))}
        (self.work/'install_report.json').write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, 'Invalid installed file path'):
            state.installed_checks(self.work)

    def test_active_policy_rejects_traversal(self):
        (self.work/'config').mkdir()
        (self.work/'config/storage_policy.json').write_text(json.dumps({'active_candidates': {'ko-KR': '../escape'}}))
        with self.assertRaisesRegex(ValueError, 'Invalid active candidate'):
            state.active_stage('ko-KR', self.work)

    def test_archive_is_reported_without_materializing(self):
        item = self.report['files']['sample.bin']
        (self.stage/'sample.bin').unlink()
        (self.stage/'storage_manifest.json').write_text(json.dumps({'files': {'sample.bin': {'sha256': item['patched_sha256'], 'size': 8}}}))
        blob = self.work/'object.gz'
        blob.write_bytes(b'fixture')
        with patch.object(state, 'object_path', return_value=blob):
            result = state.candidate_state(self.stage)
        self.assertEqual(result['archived_files'], 1)
        self.assertFalse((self.stage/'sample.bin').exists())
        blob.unlink()
        with patch.object(state, 'object_path', return_value=blob):
            self.assertEqual(state.candidate_state(self.stage)['missing_or_wrong_size'], ['sample.bin'])

    def run_preflight(self, stage='release', processes='0'):
        import install_alpha
        import candidate
        process_check = RuntimeError('The game is running in Linux/Wine') if processes != '0' else None
        with patch.object(install_alpha, 'WORK', self.work), patch.object(install_alpha, 'ROOT', self.root), patch.object(install_alpha, 'require_no_linux_game_process', side_effect=process_check), patch.object(sys, 'argv', ['install_alpha.py', '--stage', stage, '--check-only']), patch.object(candidate, 'verify_candidate', return_value=json.loads(json.dumps(self.report))), contextlib.redirect_stdout(io.StringIO()):
            install_alpha.main()

    def test_preflight_makes_no_backup_or_install_record_change(self):
        before = {str(p.relative_to(self.root)): state.digest(p) for p in self.root.rglob('*') if p.is_file()}
        self.run_preflight()
        after = {str(p.relative_to(self.root)): state.digest(p) for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(list((self.work/'backups').iterdir()), [self.backup])

    def test_preflight_rejects_changed_installed_file(self):
        (self.root/'sample.bin').write_bytes(b'changed!')
        with self.assertRaisesRegex(ValueError, 'Installed file has changed'):
            self.run_preflight()

    def test_preflight_new_frontend_file_and_switching_to_older_release(self):
        name='Texture.tex'
        original=b'original atlas'
        patched=b'localized atlas'
        (self.root/name).write_bytes(original)
        (self.stage/name).write_bytes(patched)
        entry={'original_sha256':state.digest(self.root/name),
               'patched_sha256':state.digest(self.stage/name),'size':len(patched)}
        self.report['files'][name]=entry
        # A newly covered file can pass preflight before its original backup exists.
        self.run_preflight()
        self.assertFalse((self.backup/name).exists())
        # Older candidates must restore the extra file rather than leave this atlas installed.
        record=state.load(self.work/'install_report.json')
        record['files'][name]=entry
        (self.backup/name).write_bytes(original)
        (self.root/name).write_bytes(patched)
        (self.work/'install_report.json').write_text(json.dumps(record))
        del self.report['files'][name]
        self.run_preflight()
        self.assertEqual((self.root/name).read_bytes(),patched)

    def test_preflight_rejects_running_game_and_traversal(self):
        with self.assertRaisesRegex(RuntimeError, 'game is running'):
            self.run_preflight(processes='1')
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.run_preflight(stage='../escape')


if __name__ == '__main__':
    unittest.main(verbosity=2)
