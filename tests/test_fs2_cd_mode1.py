"""Test CD-ROM Mode 1 sector EDC and ECC recomputation for Farland Saga II."""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tools/fs2'))
sys.path.insert(0, str(REPO / 'tools'))

from cd_mode1 import finish_sector


class TestFS2CDMode1(unittest.TestCase):
    def test_mode1_finish_sector(self):
        sample = bytearray(2352)
        sample[1:11] = b'\xFF' * 10
        sample[12:15] = b'\x00\x02\x00'
        sample[15] = 1
        sample[16:2064] = (b'FARLAND SAGA II MODE1 TEST ' * 80)[:2048]
        self.assertEqual(len(sample), 2352)

        finished_ecc = finish_sector(sample, ecc=True)
        self.assertEqual(len(finished_ecc), 2352)
        self.assertEqual(finished_ecc[15], 1)
        self.assertNotEqual(finished_ecc[2064:2068], b'\x00\x00\x00\x00')
        self.assertTrue(any(finished_ecc[2076:]))

        finished_no_ecc = finish_sector(sample, ecc=False)
        self.assertEqual(len(finished_no_ecc), 2352)
        self.assertEqual(finished_no_ecc[2064:2068], finished_ecc[2064:2068])
        self.assertEqual(finished_no_ecc[2076:], bytes(276))


if __name__ == '__main__':
    unittest.main()
