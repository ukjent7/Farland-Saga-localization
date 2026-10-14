"""SSF Steam file container, reconstructed with E:/IDA IDALib.

The embedded tables are read from the local executable. The source files are
never changed. Repacking preserves the original 2048-byte container header.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
import pefile
from path_compat import WORK

MASK32 = (1 << 32) - 1
MASK64 = (1 << 64) - 1

def checksum(data, words=False):
    first = second = 1
    third = 0
    pos = 0
    if words:
        for pos in range(0, len(data) // 4 * 4, 4):
            first = (first + struct.unpack_from("<I", data, pos)[0]) & MASK32
            second = (second - first) & MASK32
        pos = len(data) // 4 * 4
    for i, value in enumerate(data[pos:]):
        first = (first + (value | ((value + i) << 8))) & MASK32
        second = (second - first) & MASK32
        third = (third + first * second) & MASK32
    return ((second | (first << 32)) ^ (third << 16)) & MASK64

def constants(exe):
    pe = pefile.PE(str(exe), fast_load=True)
    base = pe.OPTIONAL_HEADER.ImageBase
    def read(va, count):
        return pe.get_data(va - base, count)
    table = read(0x1410B9B90, 1024)
    raw = read(0x1410B9F90, 1024)
    permutation = [(v + (v >> 24)) & 255 for v in struct.unpack("<256I", raw)]
    if len(set(permutation)) != 256:
        raise ValueError("Executable does not contain the expected SSF tables")
    key = 0
    for c in b"V6pwsNFG":
        key = (key << 8) | (c ^ (127 + ((c.bit_count() & 1) << 7)))
    return table, raw, permutation, key

def parameters(source, exe, logical_name):
    table, raw, permutation, key = constants(exe)
    filename_hash = checksum(logical_name.encode("ascii"))
    selector = sum(bool(filename_hash & bit) for bit in [2, 0x2000, 0x800000])
    payload_selector = sum(bool(filename_hash & bit) for bit in [8, 0x8000, 0x80000])
    with Path(source).open("rb") as stream:
        header = stream.read(2048)
    if len(header) != 2048:
        raise ValueError("Container too short")
    reordered = bytearray(256)
    for i, dest in enumerate(permutation):
        reordered[dest] = header[i]
    header_hash = checksum(reordered, words=True)
    raw_qwords = struct.unpack("<128Q", raw)
    a = raw_qwords[filename_hash & 127] ^ filename_hash
    header_xor = struct.unpack_from("<Q", reordered, 248)[0] ^ ((a + raw_qwords[a & 127]) & MASK64)
    raw_dwords = struct.unpack("<256I", raw)
    decoded = b"".join(struct.pack("<Q", value ^ raw_dwords[i] ^ header_xor)
                       for i, value in enumerate(struct.unpack("<32Q", reordered)) if i < 31)
    if decoded != table[selector * 256:selector * 256 + 248]:
        raise ValueError("Container header authentication failed; executable/name mismatch")
    return {"header": header, "table": table, "qtable": struct.unpack_from("<32Q", table, payload_selector * 256),
            "a": (filename_hash + key) & MASK64, "b": (header_hash - filename_hash - key) & MASK64,
            "tail_key": sum(header_hash.to_bytes(8, "little")) & 255,
            "filename_hash": filename_hash, "header_hash": header_hash}

def transform(block, params, encode=False):
    full = len(block) // 2048 * 2048
    out = bytearray(len(block))
    a, b, qt = params["a"], params["b"], params["qtable"]
    for sector in range(0, full, 2048):
        chain = b
        for i in range(256):
            pos = sector + i * 8
            value = struct.unpack_from("<Q", block, pos)[0]
            if encode:
                cipher = (((value + a) & MASK64) ^ chain) + qt[i & 31]
                cipher &= MASK64
                struct.pack_into("<Q", out, pos, cipher)
                chain = (cipher + b) & MASK64
            else:
                plain = (chain ^ ((value - qt[i & 31]) & MASK64)) - a
                struct.pack_into("<Q", out, pos, plain & MASK64)
                chain = (value + b) & MASK64
    chain = params["tail_key"]
    for i, value in enumerate(block[full:]):
        if encode:
            cipher = ((value ^ (chain & 255)) + params["table"][i & 1023]) & 255
            out[full + i] = cipher
            chain = params["tail_key"] + cipher
        else:
            out[full + i] = (chain ^ (value - params["table"][i & 1023])) & 255
            chain = params["tail_key"] + value
    return bytes(out)

def convert(source, output, exe, logical_name, template=None):
    output = Path(output).resolve()
    if not output.is_relative_to(WORK):
        raise ValueError("Output must stay under work")
    output.parent.mkdir(parents=True, exist_ok=True)
    params = parameters(template or source, exe, logical_name)
    digest = hashlib.sha256()
    with Path(source).open("rb") as src, output.open("wb") as dst:
        if template:
            dst.write(params["header"])
        else:
            src.seek(2048)
        while block := src.read(32768):
            result = transform(block, params, encode=bool(template))
            dst.write(result)
            digest.update(result)
    report = {"source": str(Path(source).resolve()), "output": str(output), "logical_name": logical_name,
              "payload_sha256": digest.hexdigest(), "size": output.stat().st_size,
              "filename_hash": hex(params["filename_hash"]), "header_hash": hex(params["header_hash"]),
              "operation": "encode" if template else "decode"}
    output.with_suffix(output.suffix + ".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("output")
    parser.add_argument("--exe", default=str(WORK.parent / "FarlandSaga.exe"))
    parser.add_argument("--name", required=True, help="Original basename used by the game")
    parser.add_argument("--template", help="Original encrypted container for repacking")
    args = parser.parse_args()
    convert(args.source, args.output, args.exe, args.name, args.template)
