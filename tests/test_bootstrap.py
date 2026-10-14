"""Bootstrap command checks; fixtures stay inside work and never touch the game."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO/'tools'))
import bootstrap
import disc_decode
import disc_extract
import path_compat
import steam_container

WORK = path_compat.WORK
GAME_EXE = path_compat.WORKSPACE/'FarlandSaga.exe'


def fake_convert(source, output, exe, logical_name, template=None):
    """Stand in for steam_container.convert without the cipher tables."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b'decoded:' + logical_name.encode('ascii'))
    return {'output': str(output)}


def fake_decode(source, output, exe, sectors, file_index=None):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b'track')
    return {'output': str(output)}


def fake_extract(source, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    return {'volume': 'FIXTURE', 'files': [{'path': '1ST.BIN', 'size': 7, 'sha256': '0'*64}]}


class BootstrapFixture(unittest.TestCase):
    def setUp(self):
        (WORK/'.tmp').mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=WORK/'.tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root/'work'
        self.workspace = self.root/'install'
        self.workspace.mkdir()

    def game_directory(self, complete=True):
        game = self.root/'game'
        (game/'S-Tribute_Data/Managed').mkdir(parents=True, exist_ok=True)
        for name in bootstrap.REQUIRED_FILES:
            (game/name).write_bytes(name.encode('ascii'))
        for name in bootstrap.UNITY_FILES:
            (game/'S-Tribute_Data'/name).write_bytes(b'unity:' + name.encode('ascii'))
        (game/'S-Tribute_Data/globalgamemanagers').write_bytes(
            b'\x00'*0x30 + b'2022.3.52f1\x00' + b'\xff'*16)
        for name in ('Assembly-CSharp.dll', 'mscorlib.dll'):
            (game/'S-Tribute_Data/Managed'/name).write_bytes(b'dll:' + name.encode('ascii'))
        if not complete:
            # freeze 只需要可执行文件、Texture.tex 与 Unity 数据
            for name in ('FarlandSaga.cue', 'FarlandSaga_Steam.ini', 'FarlandSaga.bin'):
                (game/name).unlink()
        return game

    def patch_roots(self, game):
        return patch.object(bootstrap, 'WORK', self.work), patch.object(bootstrap, 'WORKSPACE', game)


class GameRootResolution(BootstrapFixture):
    def test_option_wins_over_workspace_default(self):
        elsewhere = self.root/'elsewhere'
        elsewhere.mkdir()
        with patch.object(bootstrap, 'WORKSPACE', self.workspace):
            self.assertEqual(bootstrap.resolve_game_root(), self.workspace)
            self.assertEqual(bootstrap.resolve_game_root(str(elsewhere)), elsewhere)
            self.assertEqual(bootstrap.resolve_game_root(elsewhere), elsewhere)

    def test_missing_game_root_is_reported(self):
        missing = self.root/'not-a-game'
        with patch.object(bootstrap, 'WORKSPACE', missing):
            with self.assertRaisesRegex(ValueError, '游戏根目录不存在'):
                bootstrap.resolve_game_root()

    def test_missing_required_files_are_listed(self):
        game = self.game_directory(complete=False)
        with self.assertRaises(ValueError) as error:
            bootstrap.require_game_files(game)
        message = str(error.exception)
        for name in ('FarlandSaga.cue', 'FarlandSaga_Steam.ini', 'FarlandSaga.bin'):
            self.assertIn(name, message)
        self.assertIn(str(game), message)

    def test_freeze_requires_only_its_own_inputs(self):
        game = self.game_directory(complete=False)
        bootstrap.require_game_files(game, ('FarlandSaga.exe', 'Texture.tex'))


class OriginalGuard(BootstrapFixture):
    def setUp(self):
        super().setUp()
        (self.work/'backups/alpha01').mkdir(parents=True)

    def patched_record(self):
        return {'version': 'fixture', 'status': 'installed',
                'backup_directory': str(self.work/'backups/alpha01'),
                'files': {'FarlandSaga.bin': {'original_sha256': '0'*64,
                                              'patched_sha256': '1'*64, 'size': 8}}}

    def write_report(self, record):
        (self.work/'install_report.json').write_text(json.dumps(record), encoding='utf-8')

    def guard(self, game, force=False):
        with patch.object(bootstrap, 'WORK', self.work), patch.object(bootstrap, 'WORKSPACE', self.workspace):
            return bootstrap.guard_original(game, force)

    def test_patched_install_is_refused(self):
        self.write_report(self.patched_record())
        with self.assertRaisesRegex(ValueError, 'restore-original'):
            self.guard(self.workspace)

    def test_force_freezes_a_patched_install_with_a_warning(self):
        self.write_report(self.patched_record())
        with contextlib.redirect_stderr(io.StringIO()) as warning:
            record = self.guard(self.workspace, force=True)
        self.assertEqual(record['version'], 'fixture')
        self.assertIn('warning', warning.getvalue())

    def test_other_game_directory_is_accepted(self):
        self.write_report(self.patched_record())
        clean = self.root/'clean-install'
        clean.mkdir()
        self.assertEqual(self.guard(clean)['version'], 'fixture')

    def test_record_without_installed_files_is_accepted(self):
        self.write_report({'status': 'restored', 'files': {},
                           'backup_directory': str(self.work/'backups/alpha01')})
        self.assertIsNone(self.guard(self.workspace))

    def test_record_with_foreign_backup_is_accepted(self):
        record = self.patched_record()
        record['backup_directory'] = str(self.root/'elsewhere/backups')
        self.write_report(record)
        self.assertIsNone(self.guard(self.workspace))

    def test_missing_report_is_accepted(self):
        self.assertIsNone(self.guard(self.workspace))


class ManagedArchive(BootstrapFixture):
    def setUp(self):
        super().setUp()
        self.game = self.game_directory()
        self.managed = self.game/'S-Tribute_Data/Managed'

    def build(self, name='unity-managed.zip'):
        output = self.work/'assets/sources'/name
        with patch.object(bootstrap, 'WORK', self.work):
            manifest = bootstrap.build_managed_archive(self.managed, output)
        return output, manifest

    def test_manifest_rows_match_archive_entries(self):
        output, manifest = self.build()
        with zipfile.ZipFile(output) as archive:
            names = archive.namelist()
            rows = {row['archive_path']: row for row in manifest['files']}
            self.assertEqual(names, ['Managed/Assembly-CSharp.dll', 'Managed/mscorlib.dll', 'source.json'])
            self.assertEqual(set(names), set(rows) | {'source.json'})
            for entry, row in rows.items():
                payload = archive.read(entry)
                self.assertEqual(len(payload), row['size'])
                self.assertEqual(hashlib.sha256(payload).hexdigest(), row['sha256'])
                self.assertEqual(row['source_path'], 'S-Tribute_Data/Managed/' + Path(entry).name)
            self.assertEqual(json.loads(archive.read('source.json')), manifest)
        self.assertEqual(manifest['schema_version'], 1)
        self.assertEqual(manifest['unity_version'], '2022.3.52f1')
        self.assertEqual(manifest['source_directory'], 'S-Tribute_Data/Managed')

    def test_repeated_builds_are_byte_identical(self):
        first, _ = self.build('first.zip')
        second, _ = self.build('second.zip')
        self.assertEqual(first.read_bytes(), second.read_bytes())
        again, _ = self.build('first.zip')
        self.assertEqual(first.read_bytes(), again.read_bytes())

    def test_entries_use_fixed_metadata(self):
        output, _ = self.build()
        with zipfile.ZipFile(output) as archive:
            for info in archive.infolist():
                self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
                self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)
                self.assertEqual(info.external_attr, 0o100644 << 16)

    def test_missing_managed_directory_is_reported(self):
        empty = self.root/'empty/S-Tribute_Data/Managed'
        empty.mkdir(parents=True)
        with patch.object(bootstrap, 'WORK', self.work):
            with self.assertRaisesRegex(ValueError, 'Managed DLL'):
                bootstrap.build_managed_archive(empty, self.work/'assets/sources/x.zip')


class UnpackSummary(BootstrapFixture):
    def run_unpack(self, force=False):
        game = self.game_directory()
        work_patch, workspace_patch = self.patch_roots(game)
        with work_patch, workspace_patch:
            with patch.object(steam_container, 'convert', fake_convert):
                with patch.object(disc_decode, 'decode', fake_decode):
                    with patch.object(disc_extract, 'extract', fake_extract):
                        with contextlib.redirect_stdout(io.StringIO()):
                            return bootstrap.unpack(game, force)

    def test_summary_reports_every_step(self):
        summary = self.run_unpack()
        self.assertEqual(summary['operation'], 'unpack')
        self.assertEqual(summary['work'], str(self.work))
        steps = summary['steps']
        self.assertEqual([step['step'] for step in steps],
                         ['steam_container']*4 + ['disc_decode', 'disc_extract', 'analysis_exe'])
        for step in steps:
            self.assertIn('output', step)
            self.assertGreater(step['bytes'], 0)
        self.assertEqual(steps[5]['files'], 1)
        self.assertEqual(steps[-1]['status'], 'copied')
        self.assertTrue(summary['next'])
        for name in ('.tmp', 'staging', 'config'):
            self.assertTrue((self.work/name).is_dir())

    def test_repeated_unpack_keeps_the_analysis_exe(self):
        first = self.run_unpack()
        second = self.run_unpack()
        self.assertEqual(second['steps'][-1]['status'], 'unchanged')
        self.assertEqual([step['bytes'] for step in first['steps']],
                         [step['bytes'] for step in second['steps']])

    def test_changed_analysis_exe_requires_force(self):
        self.run_unpack()
        (self.work/'analysis/FarlandSaga.exe').write_bytes(b'something else')
        with self.assertRaisesRegex(ValueError, '--force'):
            self.run_unpack()
        summary = self.run_unpack(force=True)
        self.assertEqual(summary['steps'][-1]['status'], 'replaced')


class FreezeSummary(BootstrapFixture):
    def run_freeze(self, force=False):
        game = self.game_directory()
        work_patch, workspace_patch = self.patch_roots(game)
        with work_patch, workspace_patch:
            with patch.object(steam_container, 'convert', fake_convert):
                with contextlib.redirect_stdout(io.StringIO()):
                    return bootstrap.freeze(game, force)

    def test_original_file_record_matches_the_frozen_sources(self):
        summary = self.run_freeze()
        self.assertEqual(summary['operation'], 'freeze')
        self.assertEqual(summary['unity_version'], '2022.3.52f1')
        unity_dir = self.work/'assets/sources/unity/S-Tribute_Data'
        archive = self.work/'assets/sources/unity-managed.zip'
        record_path = self.work/'assets/sources/unity/unity_original_files.json'
        record = json.loads(record_path.read_text(encoding='utf-8'))
        self.assertEqual(list(record), [f'S-Tribute_Data/{name}' for name in bootstrap.UNITY_FILES]
                         + ['../unity-managed.zip'])
        for name in bootstrap.UNITY_FILES:
            self.assertEqual(record[f'S-Tribute_Data/{name}'],
                             hashlib.sha256((unity_dir/name).read_bytes()).hexdigest())
        self.assertEqual(record['../unity-managed.zip'], hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertTrue(record_path.read_text(encoding='utf-8').endswith('\n'))

    def test_repeated_freeze_keeps_the_same_record(self):
        self.run_freeze()
        first = (self.work/'assets/sources/unity/unity_original_files.json').read_bytes()
        archive_first = (self.work/'assets/sources/unity-managed.zip').read_bytes()
        self.run_freeze()
        self.assertEqual((self.work/'assets/sources/unity/unity_original_files.json').read_bytes(), first)
        self.assertEqual((self.work/'assets/sources/unity-managed.zip').read_bytes(), archive_first)

    def test_missing_unity_sources_are_reported(self):
        game = self.game_directory()
        (game/'S-Tribute_Data/level0').unlink()
        work_patch, workspace_patch = self.patch_roots(game)
        with work_patch, workspace_patch:
            with patch.object(steam_container, 'convert', fake_convert):
                with self.assertRaisesRegex(ValueError, 'level0'):
                    bootstrap.freeze(game)


@unittest.skipUnless(GAME_EXE.is_file(), 'No purchased game installation next to the workspace')
class GameReplay(unittest.TestCase):
    """End-to-end replay against the real installation into a scratch workspace."""

    def setUp(self):
        (WORK/'.tmp').mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=WORK/'.tmp')
        self.addCleanup(self.temp.cleanup)
        self.scratch = Path(self.temp.name)

    def run_bootstrap(self, *arguments):
        environment = dict(os.environ, FARLAND_WORK=str(self.scratch))
        return subprocess.run([sys.executable, str(REPO/'tools/bootstrap.py'), *arguments],
                              capture_output=True, text=True, env=environment, cwd=REPO)

    def test_unpack_and_freeze_replay(self):
        game = str(path_compat.WORKSPACE)
        unpacked = self.run_bootstrap('unpack', '--game', game)
        self.assertEqual(unpacked.returncode, 0, unpacked.stderr)
        summary = json.loads(unpacked.stdout[unpacked.stdout.rindex('{\n  "operation"'):])
        self.assertEqual(summary['operation'], 'unpack')
        for name in ('FarlandSaga.cue', 'FarlandSaga_Steam.ini', 'FarlandSaga.bin',
                     'FarlandSaga_track1.bin', 'Texture.tex'):
            self.assertTrue((self.scratch/'decoded'/name).is_file(), name)
        self.assertTrue((self.scratch/'extracted/steam/_manifest.json').is_file())
        self.assertTrue((self.scratch/'analysis/FarlandSaga.exe').is_file())
        self.assertEqual(summary['steps'][5]['files'],
                         len(json.loads((self.scratch/'extracted/steam/_manifest.json').read_text())['files']))
        frozen = self.run_bootstrap('freeze', '--game', game)
        self.assertEqual(frozen.returncode, 0, frozen.stderr)
        # 冻结的图集必须是 SSF 容器本身：container_atlases.decode_container 需要
        # 2048 字节头，补丁配方钉的 original_sha256 也是这个容器的哈希。
        self.assertEqual((self.scratch/'assets/sources/Texture.tex').read_bytes(),
                         (Path(game)/'Texture.tex').read_bytes())
        record = json.loads((self.scratch/'assets/sources/unity/unity_original_files.json').read_text())
        self.assertIn('../unity-managed.zip', record)
        with zipfile.ZipFile(self.scratch/'assets/sources/unity-managed.zip') as archive:
            self.assertIn('source.json', archive.namelist())


if __name__ == '__main__':
    unittest.main(verbosity=2)
