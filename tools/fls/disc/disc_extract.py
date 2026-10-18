"""Read ISO9660 files from a MODE1/2352 track, without modifying the source."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from path_compat import WORK

SECTOR = 2352
PAYLOAD = 2048

def read_extent(stream, lba, size):
    out = bytearray()
    while len(out) < size:
        stream.seek(lba * SECTOR + 16)
        block = stream.read(PAYLOAD)
        if len(block) != PAYLOAD:
            raise ValueError(f"Truncated track at LBA {lba}")
        out.extend(block)
        lba += 1
    return bytes(out[:size])

def extract(source, output):
    source = Path(source).resolve()
    output = Path(output).resolve()
    if not output.is_relative_to(WORK):
        raise ValueError("Output must stay under work")
    output.mkdir(parents=True, exist_ok=True)
    entries = []
    with source.open("rb") as stream:
        stream.seek(0)
        if stream.read(12) != b"\x00" + b"\xff" * 10 + b"\x00":
            raise ValueError("Not a raw CD track with a sector sync header")
        pvd = read_extent(stream, 16, PAYLOAD)
        if pvd[:7] != b"\x01CD001\x01":
            raise ValueError("ISO9660 primary volume descriptor not found")
        root = pvd[156:156 + pvd[156]]
        visited = set()

        def walk(record, prefix):
            lba = int.from_bytes(record[2:6], "little")
            size = int.from_bytes(record[10:14], "little")
            if (lba, size) in visited:
                return
            visited.add((lba, size))
            data = read_extent(stream, lba, size)
            pos = 0
            while pos < size:
                length = data[pos]
                if not length:
                    pos = (pos // PAYLOAD + 1) * PAYLOAD
                    continue
                rec = data[pos:pos + length]
                if len(rec) < 34:
                    raise ValueError("Invalid directory record")
                name = rec[33:33 + rec[32]]
                pos += length
                if name in (b"\x00", b"\x01"):
                    continue
                name = name.decode("ascii").split(";")[0]
                path = prefix / name
                if rec[25] & 2:
                    walk(rec, path)
                    continue
                file_lba = int.from_bytes(rec[2:6], "little")
                file_size = int.from_bytes(rec[10:14], "little")
                # Audio entries may refer to a separate track beyond this file.
                if (file_lba + (file_size + PAYLOAD - 1) // PAYLOAD) * SECTOR > source.stat().st_size:
                    entries.append({"path": path.as_posix(), "lba": file_lba, "size": file_size,
                                    "status": "outside_data_track", "sha256": None})
                    continue
                payload = read_extent(stream, file_lba, file_size)
                target = (output / path).resolve()
                if not target.is_relative_to(output):
                    raise ValueError("Unsafe ISO file path")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                entries.append({"path": path.as_posix(), "lba": file_lba, "size": file_size,
                                "sha256": hashlib.sha256(payload).hexdigest()})
        walk(root, Path())
    report = {"source": str(source), "volume": pvd[40:72].decode("ascii").strip(), "files": entries}
    (output / "_manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"volume": report["volume"], "file_count": len(entries), "output": str(output)}, ensure_ascii=False))
    return report

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("output")
    args = parser.parse_args(argv)
    extract(args.source, args.output)

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
