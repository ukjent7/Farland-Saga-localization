"""Focused safety and round-trip tests for historical cold bundles."""
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import fls.storage.version_storage as storage
import fls.core.path_compat as path_compat

WORK = path_compat.WORK


class StorageBundles(unittest.TestCase):
    def setUp(self):
        self.original = (storage.WORK, storage.STORE, storage.BUNDLE_STORE,
                         storage.BUNDLE_INDEX, storage.POLICY)
        self.temporary = tempfile.TemporaryDirectory(dir=WORK / '.tmp')
        self.root = Path(self.temporary.name)
        storage.WORK = self.root
        storage.STORE = self.root / 'storage/objects'
        storage.BUNDLE_STORE = self.root / 'storage/bundles'
        storage.BUNDLE_INDEX = self.root / 'storage/bundles.json'
        storage.POLICY = self.root / 'config/storage_policy.json'
        (self.root / 'staging').mkdir()
        (self.root / 'backups').mkdir()
        (self.root / 'storage').mkdir(exist_ok=True)

    def tearDown(self):
        (storage.WORK, storage.STORE, storage.BUNDLE_STORE,
         storage.BUNDLE_INDEX, storage.POLICY) = self.original
        self.temporary.cleanup()

    def make_snapshot(self):
        root = self.root / 'staging/example'
        (root / 'inputs').mkdir(parents=True)
        (root / 'build_report.json').write_text('{"files":{}}\n')
        (root / 'storage_manifest.json').write_text('{"format":1,"files":{}}\n')
        (root / 'inputs/source.json').write_text('{"value":"frozen"}\n')
        (root / 'resources/data.bin').parent.mkdir()
        (root / 'resources/data.bin').write_bytes(bytes(range(256)) * 8)
        return root

    def test_bundle_round_trip_and_metadata_stays_loose(self):
        root = self.make_snapshot()
        result = storage.bundle_directory(root, apply=True, protected=set())
        self.assertEqual(result['status'], 'archived')
        self.assertTrue((root / 'build_report.json').is_file())
        self.assertTrue((root / 'storage_manifest.json').is_file())
        self.assertFalse((root / 'inputs/source.json').exists())
        self.assertFalse((root / 'resources/data.bin').exists())
        self.assertEqual(storage.verify_bundle('staging/example')['verified_files'], 2)

        restored = storage.materialize('staging/example')
        self.assertEqual(restored['bundle_restored_files'], 2)
        self.assertEqual((root / 'inputs/source.json').read_text(), '{"value":"frozen"}\n')
        self.assertEqual((root / 'resources/data.bin').read_bytes(), bytes(range(256)) * 8)

    def test_registered_bundle_id_restores_original_root(self):
        root = self.make_snapshot()
        result = storage.bundle_directory(root, apply=True, protected=set())
        bundle_id = Path(result['archive']).name.removesuffix('.tar.gz')
        self.assertEqual(storage.verify_all(bundle_id)['verified_bundle_files'], 2)
        restored = storage.materialize(bundle_id)
        self.assertEqual(restored['bundle_restored_files'], 2)
        self.assertEqual((root / 'inputs/source.json').read_text(), '{"value":"frozen"}\n')

    def test_empty_history_root_and_nested_directories_are_removed(self):
        root = self.root / 'analysis/frontend-localization'
        (root / 'tables').mkdir(parents=True)
        (root / 'empty/branch').mkdir(parents=True)
        (root / 'tables/data.json').write_text('{"frozen":true}\n')
        storage.bundle_directory(root, apply=True)
        self.assertFalse(root.exists())
        self.assertFalse((self.root / 'analysis/frontend-localization/empty').exists())
        storage.materialize('analysis/frontend-localization')
        self.assertEqual((root / 'tables/data.json').read_text(), '{"frozen":true}\n')

    def test_reapply_reuses_immutable_bundle_and_only_removes_matching_copies(self):
        root = self.make_snapshot()
        first = storage.bundle_directory(root, apply=True, protected=set())
        index_before = json.loads(storage.BUNDLE_INDEX.read_text())
        archive_before = storage.WORK / index_before['bundles']['staging/example']['archive']
        nested = root / 'inputs/source.json'
        nested.parent.mkdir(exist_ok=True)
        nested.write_text('{"value":"frozen"}\n')
        second = storage.bundle_directory(root, apply=True, protected=set())
        index_after = json.loads(storage.BUNDLE_INDEX.read_text())
        self.assertEqual(first['archive'], second['archive'])
        self.assertEqual(index_before, index_after)
        self.assertTrue(archive_before.is_file())
        self.assertFalse(nested.exists())
        self.assertFalse((root / 'resources/data.bin').exists())

    def test_new_member_gets_new_immutable_archive_and_old_archive_remains(self):
        root = self.make_snapshot()
        first = storage.bundle_directory(root, apply=True, protected=set())
        new_file = root / 'notes.txt'
        new_file.write_text('new historical note\n')
        second = storage.bundle_directory(root, apply=True, protected=set())
        self.assertNotEqual(first['archive'], second['archive'])
        self.assertTrue((storage.WORK / first['archive']).is_file())
        self.assertTrue((storage.WORK / second['archive']).is_file())
        self.assertEqual(storage.verify_bundle(root)['verified_files'], 3)
        storage.materialize(root)
        self.assertEqual(new_file.read_text(), 'new historical note\n')

    def test_restore_refuses_different_existing_file_without_overwriting(self):
        root = self.make_snapshot()
        storage.bundle_directory(root, apply=True, protected=set())
        conflict = root / 'inputs/source.json'
        conflict.parent.mkdir(exist_ok=True)
        conflict.write_text('user data\n')
        with self.assertRaises(FileExistsError):
            storage.restore_bundle(root)
        self.assertEqual(conflict.read_text(), 'user data\n')
        self.assertFalse((root / 'resources/data.bin').exists())

    def test_restore_failure_removes_registered_temporary_files(self):
        root = self.make_snapshot()
        storage.bundle_directory(root, apply=True, protected=set())
        original = storage._stream_digest
        calls = 0

        def fail_while_extracting(stream, output=None):
            nonlocal calls
            calls += 1
            if calls == 3:  # Two reads verify the archive; the third writes a restore temp.
                raise OSError('simulated extraction failure')
            return original(stream, output)

        with patch.object(storage, '_stream_digest', side_effect=fail_while_extracting):
            with self.assertRaisesRegex(OSError, 'simulated extraction failure'):
                storage.restore_bundle('staging/example')
        self.assertEqual(list(root.rglob('.*.restore-*')), [])
        self.assertFalse((root / 'inputs/source.json').exists())

    def test_bundle_paths_reject_traversal_and_symlinks(self):
        with self.assertRaises(ValueError):
            storage._validate_bundle_member('../outside')
        with self.assertRaises(ValueError):
            storage._safe_relative('staging/../outside')
        root = self.make_snapshot()
        outside = self.root / 'outside.txt'
        outside.write_text('outside')
        (root / 'escape').symlink_to(outside)
        with self.assertRaises(ValueError):
            storage.bundle_directory(root, apply=False, protected=set())

    def test_public_bundle_api_rejects_retained_release(self):
        root = self.root / 'staging/active'
        root.mkdir()
        with self.assertRaisesRegex(ValueError, 'retained'):
            storage.bundle_directory(root, apply=True, protected={root.resolve()})

    def test_object_prefix_migration_replays_before_flattening(self):
        payload = b'verified immutable object data'
        key = hashlib.sha256(payload).hexdigest()
        flat = storage.object_path(key)
        flat.parent.mkdir(parents=True, exist_ok=True)
        header = {'format': 1, 'sha256': key, 'size': len(payload), 'base': None}
        with flat.open('wb') as raw:
            with gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as archive:
                archive.write(json.dumps(header, separators=(',', ':')).encode() + b'\n')
                archive.write(storage.RECORD.pack(0, len(payload)))
                archive.write(payload)
        legacy = storage.STORE / key[:2] / flat.name
        legacy.parent.mkdir(parents=True)
        flat.replace(legacy)
        unknown = legacy.parent / 'keep-this.txt'
        unknown.write_text('unknown data')
        self.assertEqual(storage.object_path(key), legacy)

        result = storage.flatten_objects(apply=True)
        self.assertEqual(result['moved'], 1)
        self.assertEqual(result['unknown_files'], [unknown.relative_to(storage.WORK).as_posix()])
        self.assertTrue((storage.STORE / flat.name).is_file())
        self.assertTrue(unknown.is_file())
        with tempfile.TemporaryFile(dir=WORK / '.tmp') as output:
            storage.replay(key, output=output)
            output.seek(0)
            self.assertEqual(output.read(), payload)


if __name__ == '__main__':
    unittest.main()
