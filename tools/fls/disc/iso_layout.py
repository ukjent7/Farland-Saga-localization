"""ISO9660 layout of the Saturn data track and its unclaimed sector runs.

The game resolves every event path through the ISO directory records at
runtime (1ST.BIN builds ``\\EVE\\EVE%02d_%1d.EVT`` and walks the records), so a
file whose translated payload outgrows its frozen extent can be moved into a
sector run that no record claims. Only the record's extent has to change.
"""
from functools import lru_cache
from pathlib import Path

from path_compat import WORK

SECTOR = 2352
PAYLOAD = 2048
SYSTEM_SECTORS = 16


@lru_cache(maxsize=1)
def read_track():
    return (WORK / 'decoded/FarlandSaga_track1.bin').read_bytes()


def sector_count():
    return len(read_track()) // SECTOR


def _extent(raw, lba, size):
    return b''.join(raw[i*SECTOR+16:i*SECTOR+16+PAYLOAD]
                     for i in range(lba, lba+(size+PAYLOAD-1)//PAYLOAD))[:size]


def scan_directories():
    """Walk the volume descriptors, path tables and directory records.

    Returns ``(records, claimed)``: ``records`` maps a POSIX path to the
    ``(sector, offset)`` of its directory record, and ``claimed`` lists every
    ``(start_lba, sector_count)`` run the filesystem occupies. Both endian
    size fields are read and the larger one is claimed, so a record whose
    big-endian size disagrees still reserves the space it may describe.
    """
    raw = read_track()
    records, claimed = {}, []

    def claim(start, sectors):
        if start < 0 or sectors < 0:
            raise ValueError(f'Invalid extent: {start}+{sectors}')
        claimed.append((start, sectors))

    claim(0, SYSTEM_SECTORS)
    lba = SYSTEM_SECTORS
    while True:
        block = _extent(raw, lba, PAYLOAD)
        if block[1:6] != b'CD001':
            raise ValueError(f'Volume descriptor without CD001 at {lba}')
        claim(lba, 1)
        if block[0] == 255:
            break
        if block[0] == 1:
            table_size = int.from_bytes(block[132:136], 'little')
            for field, endian in ((140, 'little'), (148, 'big')):
                start = int.from_bytes(block[field:field+4], endian)
                if start:
                    claim(start, (table_size+PAYLOAD-1)//PAYLOAD)
        lba += 1

    def walk(rec, prefix=''):
        start = int.from_bytes(rec[2:6], 'little')
        size = int.from_bytes(rec[10:14], 'little')
        claim(start, (size+PAYLOAD-1)//PAYLOAD)
        data = _extent(raw, start, size)
        pos = 0
        while pos < size:
            length = data[pos]
            if not length:
                pos = (pos//PAYLOAD+1)*PAYLOAD
                continue
            entry = data[pos:pos+length]
            name = entry[33:33+entry[32]]
            if name not in (b'\0', b'\1'):
                name = prefix + name.decode('ascii').split(';')[0]
                if entry[25] & 2:
                    walk(entry, name+'/')
                else:
                    extent = int.from_bytes(entry[2:6], 'little')
                    declared = max(int.from_bytes(entry[10:14], 'little'),
                                   int.from_bytes(entry[14:18], 'big'))
                    claim(extent, (declared+PAYLOAD-1)//PAYLOAD)
                    records[name] = (start+pos//PAYLOAD, 16+pos%PAYLOAD)
            pos += length

    pvd = _extent(raw, SYSTEM_SECTORS, PAYLOAD)
    walk(pvd[156:156+pvd[156]])
    return records, claimed


@lru_cache(maxsize=1)
def free_extents():
    """Unclaimed sector runs inside the physical data track with zero payloads.

    A run qualifies only when no directory record claims any of its sectors,
    it lies inside the raw track, and every 2048-byte payload is zero. Sectors
    past the raw track are never offered: the pressed image length is fixed.
    """
    raw = read_track()
    total = len(raw) // SECTOR
    _, claimed = scan_directories()
    taken = set()
    for start, count in claimed:
        taken.update(range(start, min(start+count, total)))
    runs, start = [], None
    for lba in range(total):
        payload = raw[lba*SECTOR+16:lba*SECTOR+16+PAYLOAD]
        if lba in taken or any(payload):
            if start is not None:
                runs.append((start, lba-start))
                start = None
        elif start is None:
            start = lba
    if start is not None:
        runs.append((start, total-start))
    return tuple(runs)


def relocation_plan(needs):
    """Assign free extents to files that outgrew their frozen allocation.

    ``needs`` maps a path to the byte count it requires. Returns
    ``(plan, unplaced)`` where ``plan`` maps a path to ``(lba, sectors)`` and
    ``unplaced`` lists the paths no free extent can hold.
    """
    extents = free_extents()
    plan, unplaced = {}, []
    index = offset = 0
    for name in sorted(needs):
        required = (needs[name]+PAYLOAD-1)//PAYLOAD
        while True:
            if index >= len(extents):
                unplaced.append(name)
                break
            start, count = extents[index]
            if count-offset >= required:
                plan[name] = (start+offset, required)
                offset += required
                break
            index += 1
            offset = 0
    return plan, unplaced
