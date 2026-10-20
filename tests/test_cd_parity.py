"""Cross-check the CD MODE1 EDC/ECC code against an independent implementation.

The independent EDC/ECC below follows the CD-ROM MODE1 rules directly and never
imports build_alpha's EDC/FORWARD/BACKWARD tables, so a shared table bug cannot
hide. All fixtures are synthetic; the tests never touch the game installation.
"""
import contextlib
import hashlib
import importlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import fls.builders.build_alpha as build_alpha
import fls.disc.cd_parity as cd_parity
import fls.core.path_compat as path_compat

WORK = path_compat.WORK


def _gf_tables():
    """GF(256) 指数/对数表, 本原多项式 x^8+x^4+x^3+x^2+1 = 0x11D, 生成元 alpha = 2。"""
    exp = [0]*512
    log = [0]*256
    value = 1
    for i in range(255):
        exp[i] = value
        log[value] = i
        value <<= 1
        if value & 0x100:
            value ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i-255]
    return exp, log


GF_EXP, GF_LOG = _gf_tables()


def gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return GF_EXP[GF_LOG[a] + GF_LOG[b]]


def independent_edc(data):
    """CD-ROM EDC: 对 data 计算 CRC-32 并返回整数。

    规范生成多项式为 0x8001801B(ECMA-130 将其分解为 (x^16+x^15+x^2+1) 与
    (x^16+x^2+x+1) 之积), 反射实现用它的比特反转 0xD8018001, init 与 xorout
    均为 0, 结果以 4 字节小端写在扇区偏移 2064。binascii.crc32 的多项式是
    0x04C11DB7(反射形式 0xEDB88320), 与 CD-ROM 的不是同一个, 因此这里按位独立
    计算, 不借助该库函数, 也不使用 build_alpha 的 EDC 表。
    """
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xD8018001 if crc & 1 else 0)
    return crc


# Reed-Solomon 系统码的生成多项式 G(x) = (x+1)(x+alpha) = x^2+3x+2, alpha=2。
RS_G1, RS_G0 = 3, 2


def independent_parity(values):
    """把 values(高位在前)当作消息多项式系数, 返回 x^2*D(x) mod G(x) 的两个系数。

    即 P 校验 RS(26,24) 与 Q 校验 RS(45,43) 的系统码校验字节: 先返回 x 系数,
    后返回常数项, 与 build_alpha.finish_sector 的写入顺序一致。
    """
    r0 = r1 = 0
    for value in list(values) + [0, 0]:
        feedback = r0
        r0, r1 = r1 ^ gf_mul(feedback, RS_G1), value ^ gf_mul(feedback, RS_G0)
    return r0, r1


def independent_finish(sector):
    """按 CD-ROM MODE1 规范独立重算扇区的 EDC/ECC, 布局与 finish_sector 一致。

    EDC 写在 2064..2068(小端), 2068..2076 保留零; P 校验按 86 列各取 24 字节
    (列内步进 86, 模 2064), 写入 2076+major 与 2076+major+86; Q 校验按 52 组
    各取 43 字节(起点 (major>>1)*86+(major&1), 步进 88, 模 2236, 覆盖扇区头、
    用户数据和刚写好的 P 校验), 写入 2248+major 与 2248+major+52。
    """
    buf = bytearray(sector)
    assert len(buf) == 2352 and buf[15] == 1
    buf[2064:2068] = independent_edc(buf[:2064]).to_bytes(4, 'little')
    buf[2068:2076] = bytes(8)
    for major in range(86):
        index = major
        values = []
        for _ in range(24):
            values.append(buf[12+index])
            index = (index+86) % 2064
        buf[2076+major], buf[2076+major+86] = independent_parity(values)
    for major in range(52):
        index = (major >> 1)*86 + (major & 1)
        values = []
        for _ in range(43):
            values.append(buf[12+index])
            index = (index+88) % 2236
        buf[2248+major], buf[2248+major+52] = independent_parity(values)
    return bytes(buf)


def random_sector(seed):
    """确定性伪随机扇区, 与 cd_parity 的已知答案向量使用不同域分隔, 互相独立。"""
    sector = bytearray(2352)
    sector[0:12] = b'\x00' + b'\xff'*10 + b'\x00'
    frame = seed + 150
    sector[12:15] = bytes((frame//75//60, frame//75 % 60, frame % 75))
    sector[15] = 1
    for i in range(2048):
        sector[16+i] = hashlib.sha256(
            b'cd-parity-cross-check' + seed.to_bytes(4, 'big') + i.to_bytes(4, 'big')).digest()[0]
    return bytes(sector)


class IndependentEdcEcc(unittest.TestCase):
    """独立实现与 build_alpha.finish_sector 的交叉验证。"""

    def test_cross_check_random_sectors(self):
        for seed in range(200):
            sector = random_sector(seed)
            expected = build_alpha.finish_sector(bytearray(sector))
            actual = independent_finish(sector)
            self.assertEqual(actual[2064:], expected[2064:], f'第 {seed} 个扇区的 EDC/ECC 不一致')

    def test_known_answer_sector_round_trips(self):
        sector = bytes.fromhex(cd_parity.KNOWN_ANSWER_SECTOR)
        self.assertEqual(len(sector), 2352)
        self.assertEqual(sector[15], 1)
        self.assertEqual(build_alpha.finish_sector(bytearray(sector)), sector)

    def test_known_answer_matches_documented_generator(self):
        pre = cd_parity.synthetic_sector()
        self.assertEqual(pre[0:12], b'\x00' + b'\xff'*10 + b'\x00')
        self.assertEqual(pre[12:16], bytes((0x00, 0x02, 0x00, 0x01)))
        self.assertEqual(cd_parity.KNOWN_ANSWER_SECTOR,
                         build_alpha.finish_sector(bytearray(pre)).hex())

    def test_independent_implementation_reproduces_known_answer(self):
        sector = bytes.fromhex(cd_parity.KNOWN_ANSWER_SECTOR)
        self.assertEqual(independent_finish(sector)[2064:], sector[2064:])


class PressedReference(unittest.TestCase):
    """pressed_reference() 的来源选择; WORK 指向临时目录, 不碰真实工作区。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def reference(self):
        # cd_parity 在 import 时即绑定 WORK, 这里把 path_compat.WORK 与
        # cd_parity.WORK 一起指向临时目录。
        with patch.object(path_compat, 'WORK', self.root), patch.object(cd_parity, 'WORK', self.root):
            return cd_parity.pressed_reference()

    def test_falls_back_to_known_answer_without_pressed_disc(self):
        sector, label = self.reference()
        self.assertEqual(label, 'known-answer')
        self.assertEqual(sector, bytes.fromhex(cd_parity.KNOWN_ANSWER_SECTOR))

    def test_prefers_pressed_disc_when_present(self):
        pressed = bytes((i*13 + 7) % 256 for i in range(2352))
        (self.root/'reference_original.bin').write_bytes(pressed)
        sector, label = self.reference()
        self.assertEqual(label, 'pressed-disc')
        self.assertEqual(sector, pressed)


class VerifyContract(unittest.TestCase):

    def test_accepts_known_answer(self):
        sector = bytes.fromhex(cd_parity.KNOWN_ANSWER_SECTOR)
        self.assertEqual(cd_parity.verify(sector, 'known-answer'), sector)

    def test_rejects_tampered_edc_with_label(self):
        sector = bytearray.fromhex(cd_parity.KNOWN_ANSWER_SECTOR)
        sector[2064] ^= 0x5A
        with self.assertRaisesRegex(ValueError, 'tampered'):
            cd_parity.verify(bytes(sector), 'tampered')

    def test_rejects_wrong_length_with_label(self):
        with self.assertRaisesRegex(ValueError, 'short'):
            cd_parity.verify(b'\x00'*100, 'short')


class ModuleContract(unittest.TestCase):

    def test_import_has_no_side_effects(self):
        before = path_compat.WORK
        stdout = io.StringIO()
        with patch.object(sys, 'argv', ['cd_parity.py', 'unexpected-argument']), contextlib.redirect_stdout(stdout):
            importlib.reload(cd_parity)
        self.assertEqual(stdout.getvalue(), '')
        self.assertIs(path_compat.WORK, before)
        self.assertIs(cd_parity.WORK, before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
