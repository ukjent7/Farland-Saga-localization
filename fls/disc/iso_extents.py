"""ISO9660 数据轨空闲扩展区扫描与搬移规划。

只读解析一份原始 MODE1/2352 数据轨（例如私有工作区 decoded/FarlandSaga_track1.bin），
保守地把每一个可能被占用的扇区都标记为已占用，再找出用户数据全零、且不在任何目录
记录声称范围内的完整 MODE1 扇区，用来回答「镜像里还能不能搬移文件」。双端序的目录
记录字段被视为竞争性声明：只有相互一致、并且与冻结资源清单吻合时才允许搬移。

本模块不内含任何游戏数据，import 时无副作用。命令行薄壳只读输入轨道并打印 JSON
摘要，不写任何文件。
"""
import argparse
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import sys
import tempfile

from fls.core.path_compat import inside_workspace


SECTOR_SIZE = 2352
PAYLOAD_SIZE = 2048
SYNC = b"\x00" + b"\xff" * 10 + b"\x00"
ZERO_PAYLOAD = bytes(PAYLOAD_SIZE)


def _u32le(data, offset):
    return int.from_bytes(data[offset:offset + 4], "little")


def _u32be(data, offset):
    return int.from_bytes(data[offset:offset + 4], "big")


def _sectors(size):
    return (size + PAYLOAD_SIZE - 1) // PAYLOAD_SIZE


@dataclass(frozen=True)
class ExtentRange:
    start_lba: int
    end_lba: int  # exclusive
    source: str
    path: str | None = None

    @property
    def sector_count(self):
        return self.end_lba - self.start_lba


@dataclass(frozen=True)
class ZeroExtent:
    start_lba: int
    sector_count: int

    @property
    def end_lba(self):
        return self.start_lba + self.sector_count

    @property
    def size_bytes(self):
        return self.sector_count * PAYLOAD_SIZE


@dataclass(frozen=True)
class DirectoryRecord:
    path: str
    record_lba: int
    record_offset: int  # byte offset within the 2048-byte sector payload
    lba_le: int
    lba_be: int
    size_le: int
    size_be: int
    extended_attribute_sectors: int
    flags: int
    raw: bytes = field(repr=False)
    volume_descriptor_lba: int | None = None

    @property
    def is_directory(self):
        return bool(self.flags & 0x02)

    @property
    def is_multi_extent(self):
        return bool(self.flags & 0x80)

    @property
    def endian_consistent(self):
        return self.lba_le == self.lba_be and self.size_le == self.size_be

    @property
    def candidate_lbas(self):
        return tuple(dict.fromkeys((self.lba_le, self.lba_be)))

    @property
    def conservative_size(self):
        return max(self.size_le, self.size_be)


@dataclass(frozen=True)
class FilePlan:
    path: str
    source_lba: int
    source_size: int
    destination_lba: int
    payload_size: int
    allocation_sectors: int
    relocated: bool
    records: tuple[DirectoryRecord, ...]
    payload: bytes = field(repr=False)


@dataclass
class IsoLayout:
    raw: bytes = field(repr=False)
    source_sha256: str
    sector_count: int
    volume_space_size: int
    records: list[DirectoryRecord]
    occupied_ranges: list[ExtentRange]
    anomalies: list[dict]
    complete: bool
    files: dict[str, list[DirectoryRecord]]
    directories: dict[str, list[DirectoryRecord]]

    def zero_free_extents(self):
        """Return unoccupied, complete MODE1 sectors with zero user data.

        Each candidate sector must have the raw CD sync pattern, MODE1 byte,
        and a full 2048-byte zero payload.  The sector header and parity are
        allowed to contain their required nonzero framing/check values.
        """
        limit = min(self.sector_count, self.volume_space_size)
        occupied = bytearray(limit)
        for extent in self.occupied_ranges:
            start = max(0, extent.start_lba)
            end = min(limit, extent.end_lba)
            if start < end:
                occupied[start:end] = b"\x01" * (end - start)

        result = []
        run_start = None
        for lba in range(limit):
            if occupied[lba]:
                if run_start is not None:
                    result.append(ZeroExtent(run_start, lba - run_start))
                    run_start = None
                continue
            offset = lba * SECTOR_SIZE
            sector = self.raw[offset:offset + SECTOR_SIZE]
            is_zero_mode1 = (len(sector) == SECTOR_SIZE and sector[:12] == SYNC
                             and sector[15] == 1
                             and sector[16:16 + PAYLOAD_SIZE] == ZERO_PAYLOAD)
            if is_zero_mode1:
                if run_start is None:
                    run_start = lba
            elif run_start is not None:
                result.append(ZeroExtent(run_start, lba - run_start))
                run_start = None
        if run_start is not None:
            result.append(ZeroExtent(run_start, limit - run_start))
        return result

    def file_records(self, path):
        return tuple(self.files.get(path, ()))

    def plan_relocations(self, changes, manifest):
        """Plan changed file extents using only freshly scanned zero sectors.

        ``manifest`` is the frozen resource manifest's ``files`` list or a
        mapping of path to its ``lba``/``size`` row.  A changed file must map
        to one or more matching, consistent directory records.  Multiple
        PVD/SVD copies are accepted only when they all point to the same
        extent and size.
        """
        if not self.complete:
            raise ValueError("ISO occupancy scan is incomplete; relocation is unsafe")
        manifest_by_path = ({row["path"]: row for row in manifest}
                            if not isinstance(manifest, dict) else manifest)
        free = [[extent.start_lba, extent.end_lba] for extent in self.zero_free_extents()]
        plans = []

        for path, payload_value in sorted(changes.items()):
            payload = bytes(payload_value)
            if path not in manifest_by_path:
                raise ValueError(f"Changed resource is absent from frozen manifest: {path}")
            source = manifest_by_path[path]
            if source.get("status") == "outside_data_track":
                raise ValueError(f"Changed resource is outside the MODE1 data track: {path}")
            if "lba" not in source or "size" not in source:
                raise ValueError(f"Manifest lacks original LBA/size: {path}")

            records = self.file_records(path)
            if not records:
                raise ValueError(f"No ISO directory record found for changed resource: {path}")
            if any(record.is_directory or record.is_multi_extent for record in records):
                raise ValueError(f"Changed resource has unsupported ISO record form: {path}")
            if any(not record.endian_consistent for record in records):
                raise ValueError(f"Changed resource has inconsistent directory endian fields: {path}")
            locations = {(record.lba_le, record.size_le) for record in records}
            if len(locations) != 1:
                raise ValueError(f"Changed resource has conflicting ISO directory records: {path}")
            source_lba, source_size = next(iter(locations))
            if source_lba != int(source["lba"]) or source_size != int(source["size"]):
                raise ValueError(f"Manifest LBA/size disagrees with ISO directory: {path}")
            if source_lba < 0 or source_lba + _sectors(source_size) > self.sector_count:
                raise ValueError(f"Changed resource extent escapes the raw data track: {path}")

            needed = _sectors(len(payload))
            original_allocation = _sectors(source_size)
            relocated = needed > original_allocation
            destination_lba = source_lba
            allocation = max(needed, original_allocation)
            if relocated:
                allocation = needed
                destination_lba = None
                for run in free:
                    if run[1] - run[0] >= needed:
                        destination_lba = run[0]
                        run[0] += needed
                        if run[0] == run[1]:
                            free.remove(run)
                        break
                if destination_lba is None:
                    raise ValueError(f"No verified zero MODE1 extent can hold changed resource: {path}")

            plans.append(FilePlan(path, source_lba, source_size, destination_lba,
                                  len(payload), allocation, relocated, records, payload))
        return plans

    def apply_plans(self, plans):
        """Apply planned logical payloads and directory updates to raw sectors.

        Returns mutable raw 2352-byte sectors keyed by logical LBA.  EDC/ECC
        is intentionally finalized by the caller after all edits are joined.
        """
        changed = {}

        def mutable_sector(lba):
            if lba not in changed:
                start = lba * SECTOR_SIZE
                raw_sector = self.raw[start:start + SECTOR_SIZE]
                if len(raw_sector) != SECTOR_SIZE:
                    raise ValueError(f"Raw ISO sector is truncated: {lba}")
                changed[lba] = bytearray(raw_sector)
            return changed[lba]

        for plan in plans:
            padded = plan.payload + bytes(plan.allocation_sectors * PAYLOAD_SIZE - len(plan.payload))
            for index in range(plan.allocation_sectors):
                lba = plan.destination_lba + index
                block = padded[index * PAYLOAD_SIZE:(index + 1) * PAYLOAD_SIZE]
                mutable_sector(lba)[16:16 + PAYLOAD_SIZE] = block

            if plan.relocated or plan.payload_size != plan.source_size:
                for record in plan.records:
                    sector = mutable_sector(record.record_lba)
                    offset = 16 + record.record_offset
                    end = offset + len(record.raw)
                    if end > 16 + PAYLOAD_SIZE:
                        raise ValueError(f"Directory record crosses sector boundary: {plan.path}")
                    encoded = bytearray(record.raw)
                    encoded[2:6] = plan.destination_lba.to_bytes(4, "little")
                    encoded[6:10] = plan.destination_lba.to_bytes(4, "big")
                    encoded[10:14] = plan.payload_size.to_bytes(4, "little")
                    encoded[14:18] = plan.payload_size.to_bytes(4, "big")
                    sector[offset:end] = encoded
        return changed


def _decode_identifier(raw_name):
    try:
        name = raw_name.decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError("ISO directory contains a non-ASCII identifier") from error
    if name in (".", ".."):
        return name
    return name.split(";", 1)[0]


def _add_occupied(ranges, start_lba, size, source, path=None, ext_attr=0):
    count = _sectors(size)
    start = start_lba - ext_attr
    end = start_lba + count
    if end > start:
        ranges.append(ExtentRange(start, end, source, path))


def _payload(raw, lba):
    if lba < 0:
        raise ValueError(f"Negative ISO LBA: {lba}")
    start = lba * SECTOR_SIZE
    sector = raw[start:start + SECTOR_SIZE]
    if len(sector) != SECTOR_SIZE:
        raise ValueError(f"ISO sector is outside the raw track: {lba}")
    if sector[:12] != SYNC or sector[15] != 1:
        raise ValueError(f"ISO sector is not raw MODE1/2352: {lba}")
    return sector[16:16 + PAYLOAD_SIZE]


def _range_for_record(ranges, record, source):
    for lba in record.candidate_lbas:
        _add_occupied(ranges, lba, record.conservative_size, source,
                      record.path, record.extended_attribute_sectors)


def _directory_record(data, pos, directory_lba, volume_lba, name, path):
    length = data[pos]
    if length < 34 or pos + length > len(data):
        raise ValueError(f"Invalid ISO directory record at LBA {directory_lba}, offset {pos}")
    if pos // PAYLOAD_SIZE != (pos + length - 1) // PAYLOAD_SIZE:
        raise ValueError(f"ISO directory record crosses a sector at LBA {directory_lba}")
    raw = bytes(data[pos:pos + length])
    identifier_length = raw[32]
    if 33 + identifier_length > length:
        raise ValueError(f"Invalid ISO identifier length at LBA {directory_lba}, offset {pos}")
    flags = raw[25]
    record_name = raw[33:33 + identifier_length]
    if record_name == b"\x00":
        normalized = "."
    elif record_name == b"\x01":
        normalized = ".."
    else:
        normalized = _decode_identifier(record_name)
    if normalized in (".", ".."):
        entry_path = path
    else:
        entry_path = f"{path}/{normalized}" if path else normalized
    return DirectoryRecord(
        path=entry_path,
        record_lba=directory_lba + pos // PAYLOAD_SIZE,
        record_offset=pos % PAYLOAD_SIZE,
        lba_le=_u32le(raw, 2),
        lba_be=_u32be(raw, 6),
        size_le=_u32le(raw, 10),
        size_be=_u32be(raw, 14),
        extended_attribute_sectors=raw[1],
        flags=flags,
        raw=raw,
        volume_descriptor_lba=volume_lba,
    )


def _parse_path_table(raw, lba, size, endian, ranges, anomalies, descriptor_lba, label):
    if not lba or not size:
        anomalies.append({"kind": "missing_path_table", "descriptor_lba": descriptor_lba,
                          "table": label, "lba": lba, "size": size})
        return
    _add_occupied(ranges, lba, size, f"{label}_path_table", f"descriptor:{descriptor_lba}")
    start = lba * SECTOR_SIZE
    table = bytearray()
    for sector_lba in range(lba, lba + _sectors(size)):
        table.extend(_payload(raw, sector_lba))
    table = table[:size]
    pos = 0
    names = [""]
    while pos < size:
        if not table[pos]:
            raise ValueError(f"Malformed {label} path table at LBA {lba}, offset {pos}")
        if pos + 8 > size:
            raise ValueError(f"Truncated {label} path table at LBA {lba}, offset {pos}")
        name_size = table[pos]
        ext_attr = table[pos + 1]
        record_size = 8 + name_size + (name_size & 1)
        if name_size == 0 or pos + record_size > size:
            raise ValueError(f"Invalid {label} path table record at LBA {lba}, offset {pos}")
        extent_bytes = table[pos + 2:pos + 6]
        extent_lba = int.from_bytes(extent_bytes, endian)
        parent_number = int.from_bytes(table[pos + 6:pos + 8], endian)
        name_raw = bytes(table[pos + 8:pos + 8 + name_size])
        name = "" if name_raw == b"\x00" else _decode_identifier(name_raw)
        if parent_number < 1 or parent_number > len(names):
            raise ValueError(f"Invalid parent index in {label} path table at LBA {lba}")
        if not name:
            path = ""
            if len(names) != 1:
                anomalies.append({"kind": "duplicate_path_table_root", "descriptor_lba": descriptor_lba,
                                  "table": label})
        else:
            parent = names[parent_number - 1]
            path = f"{parent}/{name}" if parent else name
        names.append(path)
        _add_occupied(ranges, extent_lba, 0, f"{label}_directory_extent", path, ext_attr)
        pos += record_size


def scan_iso(raw, expected_sha256=None):
    """Parse one raw MODE1/2352 data track and conservatively map occupancy."""
    raw = bytes(raw)
    if len(raw) % SECTOR_SIZE:
        raise ValueError("Raw data track size is not a whole number of sectors")
    sector_count = len(raw) // SECTOR_SIZE
    if sector_count <= 16:
        raise ValueError("Raw data track is too short to contain an ISO PVD")
    source_sha256 = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and source_sha256.lower() != expected_sha256.lower():
        raise ValueError("Raw ISO source SHA-256 does not match the approved source")

    ranges = [ExtentRange(0, min(16, sector_count), "system_area")]
    anomalies = []
    descriptors = []
    pvd_values = []
    terminator_found = False
    for lba in range(16, min(sector_count, 16 + 128)):
        block = _payload(raw, lba)
        if block[1:6] != b"CD001" or block[6] != 1:
            raise ValueError(f"Invalid ISO volume descriptor at LBA {lba}")
        descriptor_type = block[0]
        ranges.append(ExtentRange(lba, lba + 1, "volume_descriptor", str(lba)))
        descriptors.append((lba, descriptor_type, block))
        if descriptor_type in (1, 2):
            vol_le, vol_be = _u32le(block, 80), _u32be(block, 84)
            if vol_le != vol_be:
                anomalies.append({"kind": "volume_space_size_endian_mismatch", "descriptor_lba": lba,
                                  "little": vol_le, "big": vol_be})
            volume_size = max(vol_le, vol_be)
            block_le = int.from_bytes(block[128:130], "little")
            block_be = int.from_bytes(block[130:132], "big")
            if block_le != PAYLOAD_SIZE or block_be != PAYLOAD_SIZE:
                raise ValueError(f"Unsupported ISO logical block size at descriptor LBA {lba}")
            pvd_values.append((lba, block, volume_size))
        if descriptor_type == 255:
            terminator_found = True
            break
    if not terminator_found:
        raise ValueError("ISO volume descriptor terminator was not found")
    if not pvd_values:
        raise ValueError("ISO primary volume descriptor was not found")
    volume_space_size = max(value[2] for value in pvd_values)
    if any(value[2] != volume_space_size for value in pvd_values):
        anomalies.append({"kind": "volume_descriptors_disagree_on_size",
                          "values": [{"lba": lba, "size": size} for lba, _, size in pvd_values]})
    if volume_space_size <= 16:
        raise ValueError("ISO volume space size does not include its descriptors")

    # Parse every PVD/SVD path table.  Both mandatory endian forms and any
    # optional copies contribute occupied sectors and directory extents.
    for descriptor_lba, block, _ in pvd_values:
        size_le, size_be = _u32le(block, 132), _u32be(block, 136)
        if size_le != size_be:
            anomalies.append({"kind": "path_table_size_endian_mismatch", "descriptor_lba": descriptor_lba,
                              "little": size_le, "big": size_be})
        table_size = max(size_le, size_be)
        tables = [
            ("little", _u32le(block, 140), "type_l"),
            ("little", _u32le(block, 144), "optional_type_l"),
            ("big", _u32be(block, 148), "type_m"),
            ("big", _u32be(block, 152), "optional_type_m"),
        ]
        for endian, table_lba, label in tables:
            if table_lba:
                _parse_path_table(raw, table_lba, table_size, endian, ranges, anomalies,
                                   descriptor_lba, label)

    files = {}
    directories = {}
    records = []
    queue = []
    visited_dirs = set()

    # Root directory records are stored in each descriptor, not in a
    # directory table entry.  Use both-endian candidates and preserve the
    # larger size when reading an inconsistent record.
    for descriptor_lba, block, _ in pvd_values:
        root = block[156:156 + block[156]]
        if len(root) < 34 or root[0] != len(root):
            raise ValueError(f"Invalid root directory record at descriptor LBA {descriptor_lba}")
        root_lba_le, root_lba_be = _u32le(root, 2), _u32be(root, 6)
        root_size_le, root_size_be = _u32le(root, 10), _u32be(root, 14)
        root_record = DirectoryRecord("", -1, -1, root_lba_le, root_lba_be,
                                      root_size_le, root_size_be, root[1], root[25],
                                      bytes(root), descriptor_lba)
        if not root_record.endian_consistent:
            anomalies.append({"kind": "root_directory_endian_mismatch", "descriptor_lba": descriptor_lba,
                              "lba_little": root_lba_le, "lba_big": root_lba_be,
                              "size_little": root_size_le, "size_big": root_size_be})
        _range_for_record(ranges, root_record, "root_directory")
        directories.setdefault("", []).append(root_record)
        for root_lba in root_record.candidate_lbas:
            queue.append(("", root_lba, root_record.conservative_size, root[1], descriptor_lba))

    while queue:
        parent_path, directory_lba, directory_size, ext_attr, descriptor_lba = queue.pop(0)
        key = (parent_path, directory_lba, directory_size, ext_attr, descriptor_lba)
        if key in visited_dirs:
            continue
        visited_dirs.add(key)
        count = _sectors(directory_size)
        if directory_lba < 0 or directory_lba + count > sector_count:
            anomalies.append({"kind": "directory_outside_track", "path": parent_path,
                              "lba": directory_lba, "size": directory_size})
            continue
        data = bytearray()
        for index in range(count):
            data.extend(_payload(raw, directory_lba + index))
        data = data[:directory_size]
        pos = 0
        while pos < directory_size:
            if data[pos] == 0:
                pos = ((pos // PAYLOAD_SIZE) + 1) * PAYLOAD_SIZE
                continue
            record = _directory_record(data, pos, directory_lba, descriptor_lba,
                                       "", parent_path)
            records.append(record)
            if record.path not in (parent_path, f"{parent_path}/.", f"{parent_path}/.."):
                if record.is_directory:
                    directories.setdefault(record.path, []).append(record)
                    _range_for_record(ranges, record, "directory_record")
                    for candidate_lba in record.candidate_lbas:
                        queue.append((record.path, candidate_lba, record.conservative_size,
                                      record.extended_attribute_sectors, descriptor_lba))
                else:
                    files.setdefault(record.path, []).append(record)
                    _range_for_record(ranges, record, "file_record")
            pos += len(record.raw)

    # Any inconsistent non-target record is still fully occupied at every
    # LBA and through the larger declared size.  Report it for human review.
    for record in records:
        if not record.endian_consistent:
            anomalies.append({"kind": "directory_record_endian_mismatch", "path": record.path,
                              "record_lba": record.record_lba, "record_offset": record.record_offset,
                              "lba_little": record.lba_le, "lba_big": record.lba_be,
                              "size_little": record.size_le, "size_big": record.size_be,
                              "flags": record.flags})

    complete = not any(item["kind"] == "directory_outside_track" for item in anomalies)
    if volume_space_size > sector_count:
        anomalies.append({"kind": "volume_exceeds_raw_track", "volume_space_size": volume_space_size,
                          "raw_sector_count": sector_count})
        complete = False

    return IsoLayout(raw, source_sha256, sector_count, volume_space_size,
                     records, ranges, anomalies, complete, files, directories)


def checked_path(value, label):
    """解析并校验路径：只允许私有工作区、公开仓库与系统临时目录。"""
    path = Path(value).expanduser().resolve()
    scratch = Path(tempfile.gettempdir()).resolve()
    if not inside_workspace(path) and not path.is_relative_to(scratch):
        raise ValueError(f'{label} 必须位于私有工作区、公开仓库或临时目录内：{path}')
    return path


def report(layout, source, expected_sha256=None, min_run=5):
    """把扫描结果整理成 JSON 摘要；不写任何文件。"""
    runs = [extent for extent in layout.zero_free_extents() if extent.sector_count >= min_run]
    kinds = {}
    for item in layout.anomalies:
        kinds[item['kind']] = kinds.get(item['kind'], 0) + 1
    # 只统计与物理轨道相交的范围：声称超出轨道末端的记录已经作为
    # volume_exceeds_raw_track 异常上报，不能再计入占用扇区。
    limit = layout.sector_count
    inside = [extent for extent in layout.occupied_ranges if extent.start_lba < limit]
    merged = []
    for extent in sorted(inside, key=lambda item: (item.start_lba, item.end_lba)):
        end = min(extent.end_lba, limit)
        if merged and extent.start_lba <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([extent.start_lba, end])
    claimed = sum(end - start for start, end in merged)
    return {
        'source': {
            'path': str(source),
            'size_bytes': len(layout.raw),
            'physical_sector_count': layout.sector_count,
            'sector_size_bytes': SECTOR_SIZE,
            'payload_size_bytes': PAYLOAD_SIZE,
            'sha256': layout.source_sha256,
            'expected_sha256': expected_sha256,
        },
        'volume': {
            'volume_space_size': layout.volume_space_size,
            'physical_sectors_within_volume': min(layout.volume_space_size, layout.sector_count),
            'declared_sectors_beyond_raw_track': max(0, layout.volume_space_size - layout.sector_count),
            'scanner_complete_for_entire_declared_volume': layout.complete,
        },
        'occupancy': {
            'directory_record_count': len(layout.records),
            'file_path_count': len(layout.files),
            'directory_path_count': len(layout.directories),
            'raw_occupied_extent_count': len(inside),
            'merged_physical_occupied_ranges': [
                {'start_lba': start, 'end_lba_exclusive': end, 'sector_count': end - start}
                for start, end in merged],
            'merged_physical_occupied_sector_count': claimed,
            'physical_sector_count_not_claimed_by_occupancy_records': layout.sector_count - claimed,
        },
        'anomalies': {'count': len(layout.anomalies), 'kinds': kinds, 'items': layout.anomalies},
        'free_zero_mode1': {
            'min_run_sectors': min_run,
            'run_count': len(runs),
            'total_sectors': sum(extent.sector_count for extent in runs),
            'runs': [{'start_lba': extent.start_lba, 'end_lba_exclusive': extent.end_lba,
                      'sector_count': extent.sector_count, 'size_bytes': extent.size_bytes}
                     for extent in runs],
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='只读扫描 ISO9660 数据轨的空闲零扇区扩展区')
    parser.add_argument('track', help='原始 MODE1/2352 数据轨，例如 decoded/FarlandSaga_track1.bin')
    parser.add_argument('--expected-sha256', help='期望的来源 SHA-256，不一致时拒绝继续')
    parser.add_argument('--min-run', type=int, default=5,
                        help='只报告不少于这么多个连续空闲扇区的运行段，默认 5')
    args = parser.parse_args(argv)
    source = checked_path(args.track, '数据轨')
    layout = scan_iso(source.read_bytes(), args.expected_sha256)
    print(json.dumps(report(layout, source, args.expected_sha256, args.min_run),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
