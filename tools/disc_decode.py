"""Undo the Steam Saturn image sector permutation and sector cipher."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
import pefile
from steam_container import MASK64
from path_compat import WORK


def unpack_lz(data):
    size, tokens, literal_offset = struct.unpack_from(">III", data)
    control = 12
    literal = literal_offset
    output = bytearray()
    mask = 0
    for _ in range(tokens):
        if not mask:
            flags = data[control]
            control += 1
            mask = 128
        if flags & mask:
            distance, count = data[literal], data[literal + 1] + 3
            literal += 2
            if not distance or distance > len(output):
                raise ValueError("Invalid LZ back reference")
            for _ in range(count):
                output.append(output[-distance])
        else:
            output.append(data[literal])
            literal += 1
        mask >>= 1
    if len(output) != size:
        raise ValueError("LZ size mismatch")
    return bytes(output)

def tables(exe):
    pe = pefile.PE(str(exe), fast_load=True)
    base = pe.OPTIONAL_HEADER.ImageBase
    def read(va, count):
        return pe.get_data(va - base, count)
    left = struct.unpack("<1024I", read(0x14118DDB0, 4096))
    right = struct.unpack("<1024I", read(0x14118EDB0, 4096))
    initial = b"".join(struct.pack("<I", (a-b) & 0xFFFFFFFF) for a, b in zip(left, right))
    key = 0
    for c in b"jS6ftnRc":
        key = (key << 8) | (c ^ (126 if c.bit_count() & 1 else 247))
    # PSHUFD 0xD8 on [lo, hi, lo, hi] produces [lo, lo, hi, hi].
    swapped = (key & 0xFFFFFFFF) * 0x100000001
    cipher_table = b"".join(struct.pack("<Q", v ^ swapped) for v in struct.unpack("<512Q", initial))
    compressed = read(0x140D2D2D0, 0x300000)
    delta = unpack_lz(compressed)
    perm = bytes((v + (i & 63)) & 255 for i, v in enumerate(delta))
    return struct.unpack("<1024I", cipher_table), struct.unpack("<512Q", cipher_table), perm

def sector_transform(data, lba, dwords, qwords, encode=False):
    idx = lba & 1023
    nxt = (idx + 1) & 1023
    chain = dwords[idx] | (dwords[nxt] << 32)
    idx = nxt >> 1
    output = bytearray(2352)
    for i, value in enumerate(struct.unpack("<294Q", data)):
        idx = (idx + 1) & 511
        if encode:
            value = (value + chain) & MASK64
            struct.pack_into("<Q", output, i * 8, value)
            chain = (value + qwords[idx]) & MASK64
        else:
            struct.pack_into("<Q", output, i * 8, (value - chain) & MASK64)
            chain = (value + qwords[idx]) & MASK64
    return bytes(output)

def decode(source, output, exe, sectors, file_index=None):
    output = Path(output).resolve()
    if not output.is_relative_to(WORK):
        raise ValueError("Output must stay under work")
    dwords, qwords, perm = tables(exe)
    with Path(source).open("rb") as stream:
        candidates = []
        for candidate in range(8) if file_index is None else [file_index]:
            offset = perm[candidate * 283840]
            stream.seek(offset * 2352)
            first = sector_transform(stream.read(2352), 0, dwords, qwords)
            print("Candidate", candidate, "physical sector", offset, "head", first[:64].hex(" "), flush=True)
            if first[:12] == b"\0" + b"\xff" * 10 + b"\0" and first[16:32] == b"SEGA SEGASATURN ":
                stream.seek((64 + perm[candidate * 283840 + 64]) * 2352)
                probe = sector_transform(stream.read(2352), 64, dwords, qwords)
                if probe[:16] == b"\0" + b"\xff" * 10 + b"\0\0\x02\x64\x01":
                    candidates.append(candidate)
        if len(candidates) != 1:
            raise ValueError(f"Cannot identify image permutation: {candidates}")
        selected = candidates[0]
        output.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with output.open("wb") as dst:
            for lba in range(sectors):
                physical = (lba & ~63) + perm[selected * 283840 + lba]
                stream.seek(physical * 2352)
                block = stream.read(2352)
                if len(block) != 2352:
                    raise ValueError(f"Truncated physical sector {physical}")
                plain = sector_transform(block, lba, dwords, qwords)
                dst.write(plain)
                digest.update(plain)
        report = {"source": str(Path(source).resolve()), "output": str(output), "file_index": selected,
                  "sectors": sectors, "size": output.stat().st_size, "sha256": digest.hexdigest()}
        output.with_suffix(output.suffix + ".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("output")
    parser.add_argument("--exe", default=str(WORK.parent / "FarlandSaga.exe"))
    parser.add_argument("--sectors", type=int, default=128075)
    parser.add_argument("--file-index", type=int, help="SSF FILE number; auto-detected when omitted")
    args = parser.parse_args()
    decode(args.source, args.output, args.exe, args.sectors, args.file_index)
