"""Checks for the ISO9660 free-extent scanner and its command-line shell.

The fixtures are synthetic MODE1/2352 tracks built in memory; the tests never
touch the game installation or the private workspace's decoded track.
"""
import contextlib
import importlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO/'tools'))
import iso_extents
import path_compat

WORK = path_compat.WORK
SECTOR = iso_extents.SECTOR_SIZE
PAYLOAD = iso_extents.PAYLOAD_SIZE


def _sector(lba, zero_payload):
    block = bytearray(SECTOR)
    block[:12] = iso_extents.SYNC
    block[15] = 1
    block[16:16 + PAYLOAD] = iso_extents.ZERO_PAYLOAD if zero_payload else bytes([lba + 1]) * PAYLOAD
    return bytes(block)


def _synthetic_layout():
    """20 个扇区的轨道：0–15 已占用，17–19 是空闲零扇区，另有一条越界记录。"""
    raw = b''.join(_sector(lba, zero_payload=lba >= 17) for lba in range(20))
    ranges = (iso_extents.ExtentRange(0, 16, 'system_area'),
              iso_extents.ExtentRange(20, 25, 'file_record', 'BEYOND.BIN'))
    return iso_extents.IsoLayout(
        raw=raw, source_sha256='0' * 64, sector_count=20, volume_space_size=25,
        records=(), occupied_ranges=ranges,
        anomalies=({'kind': 'volume_exceeds_raw_track'},), complete=False,
        files={}, directories={})


class OccupancyReport(unittest.TestCase):

    def test_out_of_track_extent_is_not_counted_as_occupancy(self):
        """声称超出物理轨道的范围只作为异常上报，不得计入占用扇区。"""
        occupancy = iso_extents.report(_synthetic_layout(), Path('track.bin'))['occupancy']
        self.assertEqual(occupancy['raw_occupied_extent_count'], 1)
        self.assertEqual(occupancy['merged_physical_occupied_ranges'],
                         [{'start_lba': 0, 'end_lba_exclusive': 16, 'sector_count': 16}])
        self.assertEqual(occupancy['merged_physical_occupied_sector_count'], 16)
        self.assertEqual(occupancy['physical_sector_count_not_claimed_by_occupancy_records'], 4)

    def test_free_zero_runs_respect_min_run(self):
        layout = _synthetic_layout()
        short = iso_extents.report(layout, Path('track.bin'), min_run=5)['free_zero_mode1']
        self.assertEqual((short['run_count'], short['total_sectors']), (0, 0))
        long = iso_extents.report(layout, Path('track.bin'), min_run=1)['free_zero_mode1']
        self.assertEqual(long['run_count'], 1)
        self.assertEqual(long['runs'],
                         [{'start_lba': 17, 'end_lba_exclusive': 20, 'sector_count': 3,
                           'size_bytes': 3 * PAYLOAD}])

    def test_report_carries_source_and_volume_facts(self):
        data = iso_extents.report(_synthetic_layout(), Path('track.bin'), 'abc')
        self.assertEqual(data['source']['physical_sector_count'], 20)
        self.assertEqual(data['source']['sha256'], '0' * 64)
        self.assertEqual(data['source']['expected_sha256'], 'abc')
        self.assertEqual(data['volume']['declared_sectors_beyond_raw_track'], 5)
        self.assertFalse(data['volume']['scanner_complete_for_entire_declared_volume'])
        self.assertEqual(data['anomalies']['kinds'], {'volume_exceeds_raw_track': 1})


class PathGuard(unittest.TestCase):

    def test_accepts_workspace_and_scratch_paths(self):
        # inside_workspace 要求严格位于根目录内部，根目录本身不算。
        inside = WORK / 'decoded'
        scratch = Path(tempfile.gettempdir())
        self.assertEqual(iso_extents.checked_path(str(inside), '数据轨'), inside.resolve())
        self.assertEqual(iso_extents.checked_path(str(scratch), '临时目录'), scratch.resolve())

    def test_refuses_paths_outside_the_workspace(self):
        outside = Path.home().resolve()
        if outside == WORK.resolve() or outside.is_relative_to(WORK.resolve()):
            self.skipTest('主目录位于私有工作区内')
        with self.assertRaises(ValueError):
            iso_extents.checked_path(str(outside), '数据轨')


class CommandLine(unittest.TestCase):

    def test_prints_json_summary(self):
        with tempfile.TemporaryDirectory() as scratch:
            track = Path(scratch) / 'track.bin'
            track.write_bytes(b'\x00' * SECTOR)
            with patch.object(iso_extents, 'scan_iso', return_value=_synthetic_layout()), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                iso_extents.main([str(track)])
        data = json.loads(out.getvalue())
        self.assertEqual(data['occupancy']['file_path_count'], 0)
        self.assertEqual(data['free_zero_mode1']['run_count'], 0)

    def test_refuses_a_mismatched_source_hash(self):
        with tempfile.TemporaryDirectory() as scratch:
            track = Path(scratch) / 'track.bin'
            track.write_bytes(b'\x00' * (20 * SECTOR))
            with self.assertRaises(ValueError):
                iso_extents.main([str(track), '--expected-sha256', 'deadbeef'])

    def test_refuses_a_track_that_is_not_whole_sectors(self):
        with tempfile.TemporaryDirectory() as scratch:
            track = Path(scratch) / 'track.bin'
            track.write_bytes(b'\x00' * (SECTOR + 1))
            with self.assertRaises(ValueError):
                iso_extents.main([str(track)])


class ModuleContract(unittest.TestCase):

    def test_import_has_no_side_effects(self):
        before = path_compat.WORK
        stdout = io.StringIO()
        with patch.object(sys, 'argv', ['iso_extents.py', 'unexpected-argument']), \
                contextlib.redirect_stdout(stdout):
            importlib.reload(iso_extents)
        self.assertEqual(stdout.getvalue(), '')
        self.assertIs(path_compat.WORK, before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
