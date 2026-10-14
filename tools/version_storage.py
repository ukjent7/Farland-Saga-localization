"""Lossless cold storage for historical candidates and installation backups.

Original backups stay as ordinary files. Historical large files share SHA-256
addressed gzip objects containing only changed 4 KiB blocks against that original.
No third-party modules or game-directory writes are required.
"""
import argparse
from contextlib import ExitStack
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys
import tarfile
import tempfile
from file_hash import sha256
from path_compat import WORK, local_path

STORE = WORK / 'storage/objects'
BUNDLE_STORE = WORK / 'storage/bundles'
BUNDLE_INDEX = WORK / 'storage/bundles.json'
POLICY = WORK / 'config/storage_policy.json'
MANIFEST = 'storage_manifest.json'
BUNDLE_MANIFEST = '_bundle_manifest.json'
BLOCK = 4096
READ_SIZE = 1024 * 1024
RECORD = struct.Struct('<QI')
ANALYSIS_BUNDLE_ROOTS = {
    'analysis/font_memory_alpha02_menu',
    'analysis/font_memory_item_menu',
    'analysis/font_memory_startup',
    'analysis/frontend-localization',
    'analysis/full_korean_frontend_sanity',
    'analysis/full_pipeline_capacity_preflight',
    'analysis/full_pipeline_smoke',
    'analysis/ui_labels',
    'analysis/unity_parameter_regression',
}
PROTECTED_BASELINE_ROOTS = {'maintenance/baseline/project-refactor-20261009'}
EXTRACTED_BUNDLE_ROOTS = {'extracted/original', 'extracted/korean'}


def under_work(path):
    path = Path(path).resolve()
    if not path.is_relative_to(WORK.resolve()) or path == WORK.resolve():
        raise ValueError(f'Path must be inside work: {path}')
    return path


def snapshot_path(value):
    value = Path(value)
    if not value.is_absolute():
        value = WORK / value
    path = under_work(value)
    if path.parent not in [(WORK / 'staging').resolve(), (WORK / 'backups').resolve()]:
        raise ValueError('Choose a direct child of staging or backups')
    return path


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def atomic_json(path, value):
    path = under_work(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as dst:
            json.dump(value, dst, ensure_ascii=False, indent=2)
            dst.write('\n')
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def object_path(key):
    if not re.fullmatch('[0-9a-f]{64}', key):
        raise ValueError('Invalid storage object hash')
    flat = STORE / (key + '.gz')
    legacy = STORE / key[:2] / (key + '.gz')
    return flat if flat.exists() or not legacy.exists() else legacy


def checked_base(header, cache):
    info = header.get('base')
    if info is None:
        return None
    path = under_work(WORK / info['path'])
    stat = path.stat()
    signature = (str(path), stat.st_size, stat.st_mtime_ns, info['sha256'])
    if signature not in cache:
        if stat.st_size != info['size'] or sha256(path) != info['sha256']:
            raise ValueError(f'Original storage base changed: {path}')
        cache.add(signature)
    return path


def replay(key, output=None, cache=None, source_path=None):
    """Stream-reconstruct and hash an object; write only if output is supplied."""
    cache = cache if cache is not None else set()
    with ExitStack() as stack:
        src = stack.enter_context(gzip.open(source_path or object_path(key), 'rb'))
        line = src.readline(65536)
        if not line.endswith(b'\n'):
            raise ValueError('Invalid storage header')
        header = json.loads(line)
        if header.get('format') != 1 or header['sha256'] != key or header['size'] < 0:
            raise ValueError('Invalid storage identity')
        base_path = checked_base(header, cache)
        base = stack.enter_context(base_path.open('rb')) if base_path else None
        h = hashlib.sha256()
        position = 0

        def emit(data):
            h.update(data)
            if output is not None:
                output.write(data)

        def gap(end):
            nonlocal position
            if end > position and base is None:
                raise ValueError('Full storage object has a missing block')
            if base is not None:
                base.seek(position)
            while position < end:
                data = base.read(min(READ_SIZE, end - position))
                if not data:
                    raise ValueError('Storage base is too short')
                emit(data)
                position += len(data)

        while raw := src.read(RECORD.size):
            if len(raw) != RECORD.size:
                raise ValueError('Truncated storage record')
            offset, size = RECORD.unpack(raw)
            if offset < position or not 0 < size <= BLOCK or offset + size > header['size']:
                raise ValueError('Invalid storage block range')
            gap(offset)
            data = src.read(size)
            if len(data) != size:
                raise ValueError('Truncated storage block')
            emit(data)
            position = offset + size
        gap(header['size'])
        if position != header['size'] or h.hexdigest() != key:
            raise ValueError(f'Reconstructed file hash mismatch: {key}')
        return header


def encode(path, key, base, cache):
    target = object_path(key)
    if target.exists():
        return replay(key, cache=cache)
    header = {'format': 1, 'sha256': key, 'size': path.stat().st_size, 'base': base}
    base_path = checked_base(header, cache)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=key + '.', suffix='.tmp', dir=target.parent)
    try:
        with ExitStack() as stack:
            raw = stack.enter_context(os.fdopen(fd, 'wb'))
            dst = stack.enter_context(gzip.GzipFile(filename='', mode='wb', fileobj=raw, compresslevel=6, mtime=0))
            src = stack.enter_context(path.open('rb'))
            original = stack.enter_context(base_path.open('rb')) if base_path else None
            dst.write(json.dumps(header, separators=(',', ':')).encode() + b'\n')
            h = hashlib.sha256()
            offset = 0
            while data := src.read(READ_SIZE):
                h.update(data)
                before = original.read(len(data)) if original else b''
                if data != before:
                    for start in range(0, len(data), BLOCK):
                        chunk = data[start:start + BLOCK]
                        if chunk != before[start:start + BLOCK]:
                            dst.write(RECORD.pack(offset + start, len(chunk)))
                            dst.write(chunk)
                offset += len(data)
            if h.hexdigest() != key or offset != header['size']:
                raise ValueError(f'File changed while archiving: {path}')
        with Path(temporary).open('rb') as completed:
            os.fsync(completed.fileno())
        os.replace(temporary, target)
        # Validate the complete reconstruction before any source file is removed.
        return replay(key, cache=cache)
    finally:
        Path(temporary).unlink(missing_ok=True)


def flatten_objects(apply=False):
    """Verify legacy prefix objects and move known objects into one flat directory."""
    result = {'applied': apply, 'verified': 0, 'moved': 0, 'duplicates_removed': 0,
              'unknown_files': [], 'directories_removed': 0}
    if not STORE.exists():
        return result
    for directory in sorted(STORE.iterdir()):
        if not directory.is_dir() or directory.is_symlink() or not re.fullmatch('[0-9a-f]{2}', directory.name):
            continue
        for source in sorted(directory.iterdir()):
            match = re.fullmatch('([0-9a-f]{64})\\.gz', source.name)
            if source.is_symlink() or not source.is_file() or not match or match.group(1)[:2] != directory.name:
                result['unknown_files'].append(source.relative_to(WORK).as_posix())
                continue
            key = match.group(1)
            replay(key, source_path=source)
            result['verified'] += 1
            target = STORE / source.name
            if not apply:
                continue
            if target.exists():
                replay(key, source_path=target)
                source.unlink()
                result['duplicates_removed'] += 1
            else:
                STORE.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)
                result['moved'] += 1
        if apply:
            try:
                directory.rmdir()
                result['directories_removed'] += 1
            except OSError:
                pass
    return result


def _safe_relative(value):
    raw = os.fspath(value)
    path = Path(raw)
    if path.is_absolute():
        try:
            path = path.relative_to(WORK)
        except ValueError as exc:
            raise ValueError(f'Path must be inside work: {raw}') from exc
    if '\\' in raw or ':' in raw:
        raise ValueError(f'Invalid POSIX work path: {raw}')
    parts = path.parts
    if not parts or any(part in {'', '.', '..'} for part in parts):
        raise ValueError(f'Invalid work-relative path: {raw}')
    return '/'.join(parts)


def _work_path(relative):
    relative = _safe_relative(relative)
    path = WORK.joinpath(*relative.split('/'))
    current = WORK
    for part in relative.split('/'):
        current = current / part
        if current.is_symlink():
            raise ValueError(f'Symlink paths are not allowed: {current}')
    if not path.resolve(strict=False).is_relative_to(WORK.resolve()):
        raise ValueError(f'Path escapes work: {relative}')
    return path


def _is_snapshot_root(relative):
    parts = relative.split('/')
    return len(parts) == 2 and parts[0] in {'staging', 'backups'}


def _allowed_bundle_root(relative):
    parts = relative.split('/')
    if _is_snapshot_root(relative):
        return True
    if len(parts) == 3 and parts[:2] == ['maintenance', 'baseline']:
        return relative not in PROTECTED_BASELINE_ROOTS
    if relative in ANALYSIS_BUNDLE_ROOTS or relative in EXTRACTED_BUNDLE_ROOTS:
        return True
    return False


def _bundle_root(value):
    relative = _safe_relative(value)
    if not _allowed_bundle_root(relative):
        raise ValueError(f'Path is not an approved bundle root: {relative}')
    return relative, _work_path(relative)


def _bundle_index():
    if not BUNDLE_INDEX.exists():
        return {'format': 1, 'bundles': {}}
    index = load(BUNDLE_INDEX)
    if index.get('format') != 1 or not isinstance(index.get('bundles'), dict):
        raise ValueError(f'Invalid bundle index: {BUNDLE_INDEX}')
    return index


def _is_bundle_id(relative):
    name = Path(relative).name
    return bool(re.fullmatch('[0-9a-f]{64}', relative)
                or re.fullmatch(r'[0-9a-f]{64}\.tar\.gz', name)
                and (relative == name or relative.startswith('storage/bundles/')))


def _resolve_bundle_root(value):
    """Accept either a configured root path or its immutable archive ID."""
    relative = _safe_relative(value)
    if _allowed_bundle_root(relative):
        return relative
    archive_name = Path(relative).name
    bundle_id = archive_name[:-7] if archive_name.endswith('.tar.gz') else archive_name
    if not re.fullmatch('[0-9a-f]{64}', bundle_id):
        raise ValueError(f'Not an approved bundle root or ID: {relative}')
    matches = [root for root, entry in _bundle_index()['bundles'].items()
               if Path(entry.get('archive', '')).name == bundle_id + '.tar.gz']
    if len(matches) != 1:
        raise ValueError(f'Bundle ID is not uniquely registered: {bundle_id}')
    return matches[0]


def _bundle_archive(entry):
    relative = _safe_relative(entry.get('archive', ''))
    if not relative.startswith('storage/bundles/'):
        raise ValueError('Bundle archive path is outside storage/bundles')
    path = _work_path(relative)
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _bundle_excluded_root_files(relative):
    if relative.startswith('staging/'):
        return {'build_report.json', 'resource_report.json', MANIFEST}
    if relative.startswith('backups/'):
        return {'manifest.json', 'previous_install_report.json', MANIFEST}
    return set()


def _collect_bundle_files(root, relative):
    excluded = _bundle_excluded_root_files(relative)
    found = []
    for current, dirs, names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        if any((current_path / name).is_symlink() for name in dirs):
            raise ValueError(f'Bundle source contains a symlink directory: {current_path}')
        dirs[:] = sorted(dirs)
        for name in names:
            path = current_path / name
            if current_path == root and name in excluded:
                continue
            if path.is_symlink():
                raise ValueError(f'Bundle source contains a symlink: {path}')
            mode = path.stat(follow_symlinks=False).st_mode
            if not stat.S_ISREG(mode):
                raise ValueError(f'Bundle source contains a non-regular file: {path}')
            rel_name = path.relative_to(root).as_posix()
            if rel_name == BUNDLE_MANIFEST:
                raise ValueError(f'Reserved bundle path exists in source: {path}')
            found.append({'path': rel_name, 'size': path.stat().st_size, 'sha256': sha256(path),
                          '_source': path})
    found.sort(key=lambda item: item['path'])
    return found


def _bundle_manifest(relative, files):
    return {'format': 1, 'root': relative,
            'files': [{'path': item['path'], 'size': item['size'], 'sha256': item['sha256']}
                      for item in files]}


def _write_bundle(root, relative, files):
    BUNDLE_STORE.mkdir(parents=True, exist_ok=True)
    manifest_bytes = json.dumps(_bundle_manifest(relative, files), ensure_ascii=False,
                                sort_keys=True, separators=(',', ':')).encode('utf-8') + b'\n'
    archive_rel = 'storage/bundles/' + hashlib.sha256(manifest_bytes).hexdigest() + '.tar.gz'
    archive = _work_path(archive_rel)
    if archive.exists():
        manifest = _verify_bundle_archive(archive, relative)
        if manifest != _bundle_manifest(relative, files):
            raise ValueError(f'Existing immutable bundle has different contents: {archive}')
        return {'root': relative, 'archive': archive_rel, 'archive_sha256': sha256(archive),
                'files': len(files), 'bytes': sum(item['size'] for item in files)}
    fd, temporary = tempfile.mkstemp(prefix=archive.name + '.', suffix='.tmp', dir=archive.parent)
    try:
        with os.fdopen(fd, 'wb') as raw:
            with gzip.GzipFile(filename='', mode='wb', fileobj=raw, compresslevel=6, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as archive_file:
                    info = tarfile.TarInfo(BUNDLE_MANIFEST)
                    info.size = len(manifest_bytes); info.mode = 0o644; info.mtime = 0
                    info.uid = info.gid = 0; info.uname = info.gname = ''
                    archive_file.addfile(info, io.BytesIO(manifest_bytes))
                    for item in files:
                        source = item['_source']
                        stat_result = source.stat()
                        info = tarfile.TarInfo(item['path'])
                        info.size = item['size']; info.mode = stat_result.st_mode & 0o777; info.mtime = 0
                        info.uid = info.gid = 0; info.uname = info.gname = ''
                        with source.open('rb') as stream:
                            archive_file.addfile(info, stream)
            raw.flush(); os.fsync(raw.fileno())
        try:
            os.link(temporary, archive)
        except FileExistsError:
            manifest = _verify_bundle_archive(archive, relative)
            if manifest != _bundle_manifest(relative, files):
                raise ValueError(f'Existing immutable bundle has different contents: {archive}')
    finally:
        Path(temporary).unlink(missing_ok=True)
    entry = {'root': relative, 'archive': archive_rel, 'archive_sha256': sha256(archive),
             'files': len(files), 'bytes': sum(item['size'] for item in files)}
    return entry


def _validate_bundle_member(name):
    if not isinstance(name, str) or not name or name.startswith('/') or '\\' in name or ':' in name:
        raise ValueError(f'Invalid bundle member path: {name!r}')
    parts = name.split('/')
    if any(part in {'', '.', '..'} for part in parts):
        raise ValueError(f'Invalid bundle member path: {name!r}')
    return '/'.join(parts)


def _stream_digest(stream, output=None):
    digest = hashlib.sha256(); size = 0
    while data := stream.read(READ_SIZE):
        digest.update(data); size += len(data)
        if output is not None:
            output.write(data)
    return size, digest.hexdigest()


def _verify_bundle_archive(archive, relative, expected_archive_sha256=None):
    if expected_archive_sha256 and sha256(archive) != expected_archive_sha256:
        raise ValueError(f'Bundle archive checksum mismatch: {archive}')
    with tarfile.open(archive, mode='r:gz') as bundle:
        members = bundle.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)) or names.count(BUNDLE_MANIFEST) != 1:
            raise ValueError(f'Invalid bundle member list: {archive}')
        if any(not member.isfile() for member in members):
            raise ValueError(f'Bundle contains a non-regular member: {archive}')
        for member in members:
            if member.name != BUNDLE_MANIFEST:
                _validate_bundle_member(member.name)
        manifest_member = next(member for member in members if member.name == BUNDLE_MANIFEST)
        source = bundle.extractfile(manifest_member)
        if source is None or manifest_member.size > 64 * 1024 * 1024:
            raise ValueError(f'Invalid bundle manifest: {archive}')
        manifest = json.loads(source.read())
        if manifest.get('format') != 1 or manifest.get('root') != relative or not isinstance(manifest.get('files'), list):
            raise ValueError(f'Bundle identity mismatch: {archive}')
        canonical_manifest = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                        separators=(',', ':')).encode('utf-8') + b'\n'
        expected_name = hashlib.sha256(canonical_manifest).hexdigest() + '.tar.gz'
        if archive.name != expected_name:
            raise ValueError(f'Bundle filename does not match immutable manifest identity: {archive}')
        expected = {}
        for item in manifest['files']:
            name = _validate_bundle_member(item.get('path'))
            size, digest = item.get('size'), item.get('sha256')
            if (name == BUNDLE_MANIFEST or name in expected or not isinstance(size, int) or size < 0
                    or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)):
                raise ValueError(f'Invalid bundle manifest entry: {name}')
            expected[name] = {'size': size, 'sha256': digest}
        if set(names) != {BUNDLE_MANIFEST, *expected}:
            raise ValueError(f'Bundle members do not match manifest: {archive}')
        for member in members:
            if member.name == BUNDLE_MANIFEST:
                continue
            entry = expected[member.name]
            if member.size != entry['size']:
                raise ValueError(f'Bundle size mismatch: {member.name}')
            stream = bundle.extractfile(member)
            if stream is None:
                raise ValueError(f'Bundle member is unreadable: {member.name}')
            size, digest = _stream_digest(stream)
            if size != entry['size'] or digest != entry['sha256']:
                raise ValueError(f'Bundle member checksum mismatch: {member.name}')
    return manifest


def verify_bundle(snapshot):
    relative, _ = _bundle_root(_resolve_bundle_root(snapshot))
    index = _bundle_index()
    entry = index['bundles'].get(relative)
    if entry is None:
        return {'root': relative, 'verified_files': 0, 'verified_bytes': 0, 'bundled': False}
    if entry.get('root') != relative:
        raise ValueError(f'Bundle index identity mismatch: {relative}')
    if not re.fullmatch('[0-9a-f]{64}', entry.get('archive_sha256', '')):
        raise ValueError(f'Invalid bundle archive checksum in index: {relative}')
    archive = _bundle_archive(entry)
    manifest = _verify_bundle_archive(archive, relative, entry.get('archive_sha256'))
    return {'root': relative, 'verified_files': len(manifest['files']),
            'verified_bytes': sum(item['size'] for item in manifest['files']), 'bundled': True}


def _existing_bundle_conflicts(root, manifest):
    destinations = []
    for item in manifest['files']:
        path = _work_path(root + '/' + _validate_bundle_member(item['path']))
        current = WORK
        for part in path.relative_to(WORK).parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise ValueError(f'Restore destination has a symlink parent: {current}')
            if current.exists() and not current.is_dir():
                raise ValueError(f'Restore destination parent is not a directory: {current}')
        if path.is_symlink():
            raise ValueError(f'Restore destination is a symlink: {path}')
        if path.exists():
            if not path.is_file() or path.stat().st_size != item['size'] or sha256(path) != item['sha256']:
                raise FileExistsError(f'Restore would overwrite different data: {path}')
            destinations.append((item, path, True))
        else:
            destinations.append((item, path, False))
    return destinations


def restore_bundle(snapshot):
    """Validate every archive member, then restore missing files without overwriting."""
    relative, root = _bundle_root(_resolve_bundle_root(snapshot))
    index = _bundle_index(); entry = index['bundles'].get(relative)
    if entry is None:
        return {'root': relative, 'restored_files': 0, 'restored_bytes': 0, 'bundled': False}
    if entry.get('root') != relative or not re.fullmatch('[0-9a-f]{64}', entry.get('archive_sha256', '')):
        raise ValueError(f'Invalid bundle index entry: {relative}')
    archive = _bundle_archive(entry)
    manifest = _verify_bundle_archive(archive, relative, entry.get('archive_sha256'))
    destinations = _existing_bundle_conflicts(relative, manifest)
    root.mkdir(parents=True, exist_ok=True)
    temporary_files = []
    created_dirs = []
    try:
        with tarfile.open(archive, mode='r:gz') as bundle:
            members = {member.name: member for member in bundle.getmembers()}
            for item, path, already_present in destinations:
                if already_present:
                    continue
                missing=[]; parent=path.parent
                while parent != WORK and not parent.exists():
                    missing.append(parent); parent=parent.parent
                path.parent.mkdir(parents=True, exist_ok=True)
                created_dirs.extend(reversed(missing))
                fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'.restore-', dir=path.parent)
                temp_path=Path(temporary)
                temporary_files.append((temp_path,path,item))
                with os.fdopen(fd,'wb') as output:
                    stream=bundle.extractfile(members[item['path']])
                    if stream is None:
                        raise ValueError(f'Bundle member is unreadable: {item["path"]}')
                    size,digest=_stream_digest(stream,output)
                    output.flush();os.fsync(output.fileno())
                if size != item['size'] or digest != item['sha256']:
                    raise ValueError(f'Bundle member changed during restore: {item["path"]}')
        # Recheck all destinations after extraction, then commit with no-overwrite hard links.
        for temporary,path,item in temporary_files:
            if path.is_symlink():
                raise ValueError(f'Restore destination became a symlink: {path}')
            if path.exists():
                if not path.is_file() or path.stat().st_size != item['size'] or sha256(path) != item['sha256']:
                    raise FileExistsError(f'Restore would overwrite different data: {path}')
        restored_files=restored_bytes=0
        for temporary,path,item in temporary_files:
            try:
                os.link(temporary,path)
            except FileExistsError:
                if not path.is_file() or path.stat().st_size != item['size'] or sha256(path) != item['sha256']:
                    raise FileExistsError(f'Restore would overwrite different data: {path}')
            else:
                restored_files+=1;restored_bytes+=item['size']
            temporary.unlink(missing_ok=True)
        return {'root':relative,'restored_files':restored_files,'restored_bytes':restored_bytes,'bundled':True}
    finally:
        for temporary,_,_ in temporary_files:
            temporary.unlink(missing_ok=True)
        for directory in reversed(created_dirs):
            try:directory.rmdir()
            except OSError:pass


def _remove_bundle_sources(files, root=None):
    removed=0
    for item in files:
        source=item['_source']
        current=source.stat(follow_symlinks=False)
        if current.st_size != item['size'] or sha256(source) != item['sha256']:
            raise ValueError(f'Bundle source changed during archive: {source}')
        source.unlink();removed+=1
    if root is None:
        root=Path(os.path.commonpath([item['_source'] for item in files]))
        if root.is_file():root=root.parent
    if root.exists():
        for current, dirs, _ in os.walk(root, topdown=False, followlinks=False):
            for name in dirs:
                directory=Path(current)/name
                if directory.is_symlink():
                    raise ValueError(f'Bundle source gained a symlink directory: {directory}')
                try:directory.rmdir()
                except OSError:pass
        try:root.rmdir()
        except OSError:pass
    return removed


def bundle_directory(snapshot, apply=False, protected=None):
    """Audit or create one lossless bundle, leaving root reports outside the archive."""
    relative, root = _bundle_root(snapshot)
    if _is_snapshot_root(relative):
        if protected is None:
            _, keep, _ = retention()
            protected = keep
        if root.resolve() in protected:
            raise ValueError(f'Refusing to bundle a retained candidate or backup: {relative}')
    if not root.exists():
        existing = verify_bundle(relative)
        if existing['bundled']:
            return {**existing, 'status':'already_bundled','applied':apply}
        raise FileNotFoundError(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f'Bundle root is not a regular directory: {root}')
    index = _bundle_index(); existing_entry=index['bundles'].get(relative)
    if existing_entry:
        verified=verify_bundle(relative)
        if not apply:
            return {**verified,'status':'already_bundled','applied':False}
        archived=_verify_bundle_archive(_bundle_archive(existing_entry),relative,existing_entry.get('archive_sha256'))
        archived_by_path={item['path']:item for item in archived['files']}
        current=_collect_bundle_files(root,relative)
        additions=[]
        for item in current:
            prior=archived_by_path.get(item['path'])
            if prior is None:
                additions.append(item)
            elif prior['size']!=item['size'] or prior['sha256']!=item['sha256']:
                raise FileExistsError(f'Existing bundled path has different data: {item["path"]}')
        if not additions:
            removed=_remove_bundle_sources(current,root) if current else 0
            return {'root':relative,'files':verified['verified_files'],'bytes':verified['verified_bytes'],
                    'bundled':True,'status':'already_bundled','applied':True,'removed_files':removed,
                    'archive':existing_entry['archive'],'archive_bytes':_bundle_archive(existing_entry).stat().st_size}
        restore_bundle(relative)
    files=_collect_bundle_files(root,relative)
    if not files:
        return {'root':relative,'files':0,'bytes':0,'bundled':False,'status':'empty','applied':apply}
    result={'root':relative,'files':len(files),'bytes':sum(item['size'] for item in files),
            'bundled':False,'status':'ready','applied':apply}
    if not apply:
        return result
    entry=_write_bundle(root,relative,files)
    _verify_bundle_archive(_bundle_archive(entry),relative,entry['archive_sha256'])
    index['bundles'][relative]=entry
    atomic_json(BUNDLE_INDEX,index)
    removed=_remove_bundle_sources(files,root)
    result.update({'bundled':True,'status':'archived','archive':entry['archive'],
                   'archive_bytes':_bundle_archive(entry).stat().st_size,'removed_files':removed})
    return result


def bundle_targets(keep=None):
    """Return explicit old snapshots and history roots, excluding retained releases."""
    if keep is None:
        _,keep,_=retention()
    targets=[]
    for parent in ['staging','backups']:
        for path in sorted((WORK/parent).iterdir()):
            if path.is_dir() and not path.is_symlink() and path.resolve() not in keep:
                targets.append(path.relative_to(WORK).as_posix())
    baseline=WORK/'maintenance/baseline'
    if baseline.exists():
        for path in sorted(baseline.iterdir()):
            relative=path.relative_to(WORK).as_posix()
            if path.is_dir() and not path.is_symlink() and relative not in PROTECTED_BASELINE_ROOTS:
                targets.append(relative)
    for relative in sorted(ANALYSIS_BUNDLE_ROOTS | EXTRACTED_BUNDLE_ROOTS):
        if (WORK/relative).is_dir() and not (WORK/relative).is_symlink():
            targets.append(relative)
    for relative in _bundle_index()['bundles']:
        if _is_snapshot_root(relative) and _work_path(relative).resolve() in keep:
            continue
        targets.append(relative)
    return sorted(set(targets))


def bundle(apply=False):
    """Audit all approved cold bundles, or compact then archive them atomically."""
    compaction=compact(apply=True) if apply else compact(apply=False)
    protected={WORK/path for path in compaction.get('kept_directories',[])}
    rows=[bundle_directory(path,apply,protected) for path in bundle_targets(protected)]
    result={'applied':apply,'roots':len(rows),'bundled_roots':sum(row.get('bundled',False) for row in rows),
            'files':sum(row.get('removed_files',0) if apply else row.get('files',0) for row in rows),
            'bytes':sum(row.get('bytes',0) for row in rows),'archive_bytes':sum(row.get('archive_bytes',0) for row in rows),
            'compaction':{k:v for k,v in compaction.items() if k!='files'},'bundles':rows}
    if apply:
        atomic_json(WORK/'maintenance/storage_bundling.json',result)
    return result


def materialize(directory):
    """Restore any indexed bundle and then reconstruct large stored snapshot files."""
    relative = _safe_relative(directory)
    if _is_bundle_id(relative):
        relative = _resolve_bundle_root(relative)
    bundle_result = restore_bundle(relative) if _allowed_bundle_root(relative) else None
    if not _is_snapshot_root(relative):
        return bundle_result or {'restored_files': 0, 'restored_bytes': 0}
    directory = snapshot_path(relative)
    index_path = directory / MANIFEST
    if not index_path.exists():
        return bundle_result or {'restored_files': 0, 'restored_bytes': 0}
    index = load(index_path)
    result = {'restored_files': 0, 'restored_bytes': 0}
    cache = set()
    for name, entry in index['files'].items():
        path = under_work(directory / name)
        if not path.is_relative_to(directory):
            raise ValueError('Archived file escapes snapshot')
        if path.exists():
            if path.stat().st_size != entry['size'] or sha256(path) != entry['sha256']:
                raise ValueError(f'Restored file has changed: {path}')
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.restore', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as dst:
                header = replay(entry['sha256'], dst, cache)
                if header['size'] != entry['size']:
                    raise ValueError('Storage manifest size mismatch')
                dst.flush()
                os.fsync(dst.fileno())
            if path.exists():
                # A concurrent restore may have won the race; an identical
                # restored file is as good as the one this process built.
                if path.stat().st_size == entry['size'] and sha256(path) == entry['sha256']:
                    continue
                raise FileExistsError(f'Destination appeared during restore: {path}')
            os.replace(temporary, path)
            result['restored_files'] += 1
            result['restored_bytes'] += entry['size']
        finally:
            Path(temporary).unlink(missing_ok=True)
    if bundle_result:
        result['bundle_restored_files'] = bundle_result['restored_files']
        result['bundle_restored_bytes'] = bundle_result['restored_bytes']
    return result


def verify_archive(directory, cache=None):
    directory = snapshot_path(directory)
    index = load(directory / MANIFEST)
    count = 0
    cache = cache if cache is not None else set()
    for name, entry in index['files'].items():
        path = under_work(directory / name)
        if not path.is_relative_to(directory):
            raise ValueError('Archived file escapes snapshot')
        header = replay(entry['sha256'], cache=cache)
        if header['size'] != entry['size']:
            raise ValueError(f'Storage manifest mismatch: {name}')
        count += 1
    return count


def verify_all(snapshot=None):
    """Verify stored large files and every member of selected/all bundles."""
    if snapshot:
        relative = _safe_relative(snapshot)
        if _is_bundle_id(relative):
            relative = _resolve_bundle_root(relative)
        bundle_result = verify_bundle(relative) if _allowed_bundle_root(relative) else {
            'verified_files': 0, 'verified_bytes': 0, 'bundled': False}
        object_files = verify_archive(relative) if _is_snapshot_root(relative) and (WORK/relative/MANIFEST).exists() else 0
        return {'snapshots': 1, 'verified_object_files': object_files,
                'verified_bundle_files': bundle_result['verified_files'],
                'verified_bundle_bytes': bundle_result['verified_bytes']}
    snapshots = [p.parent for root in ['staging', 'backups']
                 for p in (WORK/root).glob('*/'+MANIFEST)]
    cache=set()
    object_files=sum(verify_archive(path,cache) for path in snapshots)
    index=_bundle_index();bundle_files=bundle_bytes=0
    for relative in sorted(index['bundles']):
        item=verify_bundle(relative);bundle_files+=item['verified_files'];bundle_bytes+=item['verified_bytes']
    return {'snapshots':len(snapshots),'verified_object_files':object_files,
            'bundles':len(index['bundles']),'verified_bundle_files':bundle_files,
            'verified_bundle_bytes':bundle_bytes}


def retention():
    policy = load(POLICY)
    record = load(WORK / 'install_report.json')
    original = under_work(local_path(record['backup_directory']))
    previous = under_work(local_path(record['previous_release_backup'])) if record.get('previous_release_backup') else original
    keep = {original, previous}
    keep.update(snapshot_path('staging/' + name) for name in policy['active_candidates'].values())
    # Also retain the installed candidate if it differs from the configured active versions.
    for path in (WORK / 'staging').glob('*/build_report.json'):
        report = load(path)
        if report.get('files') and all(report['files'].get(name, {}).get('patched_sha256') == item['patched_sha256']
                                        for name, item in record['files'].items()):
            keep.add(path.parent.resolve())
    bases = {}
    for name, item in record['files'].items():
        path = under_work(original / name)
        if sha256(path) != item['original_sha256']:
            raise ValueError(f'Original backup mismatch: {name}')
        bases[name] = {'path': path.relative_to(WORK).as_posix(), 'sha256': item['original_sha256'], 'size': path.stat().st_size}
    return policy, keep, bases


def activate_candidate(stage, language=None):
    stage = snapshot_path(stage)
    report_path = stage / 'build_report.json'
    if not report_path.exists():
        return
    report = load(report_path)
    if report.get('build_status') != 'candidate_ready':
        return
    language = language or report.get('language')
    if language not in {'zh-CN', 'ko-KR', 'en-US'}:
        return
    policy = load(POLICY)
    policy['active_candidates'][language] = stage.name
    atomic_json(POLICY, policy)


def compact(apply=False):
    policy, keep, bases = retention()
    result = {'applied': apply, 'kept_directories': sorted(p.relative_to(WORK).as_posix() for p in keep),
              'files': [], 'source_bytes_removed': 0, 'new_object_bytes': 0}
    cache = set()
    for root, report_name in [('staging', 'build_report.json'), ('backups', 'manifest.json')]:
        for report_path in sorted((WORK / root).glob('*/' + report_name)):
            directory = snapshot_path(report_path.parent)
            if directory in keep:
                continue
            report = load(report_path)
            index_path = directory / MANIFEST
            index = load(index_path) if index_path.exists() else {'format': 1, 'files': {}}
            for name, info in report.get('files', {}).items():
                path = under_work(directory / name)
                if not path.is_relative_to(directory):
                    raise ValueError('Snapshot file escapes its directory')
                if not path.exists() or path.stat().st_size < policy['minimum_file_bytes']:
                    continue
                stat = path.stat()
                key = sha256(path)
                if root == 'staging' and (key != info['patched_sha256'] or stat.st_size != info['size']):
                    raise ValueError(f'Candidate mismatch: {path}')
                if root == 'backups':
                    previous_report = directory / 'previous_install_report.json'
                    previous_files = load(previous_report)['files'] if previous_report.exists() else {}
                    expected = {info['original_sha256']}
                    if name in previous_files:
                        expected.add(previous_files[name]['patched_sha256'])
                    if key not in expected:
                        raise ValueError(f'Backup mismatch: {path}')
                item = {'path': path.relative_to(WORK).as_posix(), 'size': stat.st_size, 'sha256': key}
                result['files'].append(item)
                if not apply:
                    continue
                blob = object_path(key)
                exists = blob.exists()
                header = encode(path, key, bases.get(name), cache)
                if header['size'] != stat.st_size:
                    raise ValueError('Storage object size mismatch')
                after = path.stat()
                if (after.st_size, after.st_mtime_ns, after.st_ino) != (stat.st_size, stat.st_mtime_ns, stat.st_ino):
                    raise ValueError(f'File changed during archiving: {path}')
                index['files'][name] = {'sha256': key, 'size': stat.st_size}
                atomic_json(index_path, index)
                path.unlink()
                result['source_bytes_removed'] += stat.st_size
                if not exists:
                    result['new_object_bytes'] += blob.stat().st_size
                print(f"Archived {item['path']} -> {blob.stat().st_size / 1048576:.2f} MiB", file=sys.stderr, flush=True)
    result['net_bytes_reclaimed'] = result['source_bytes_removed'] - result['new_object_bytes']
    result['object_flattening'] = flatten_objects(apply)
    if apply:
        atomic_json(WORK / 'maintenance/storage_compaction.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    compact_parser = commands.add_parser('compact', help='Inventory or archive historical large files')
    compact_parser.add_argument('--apply', action='store_true')
    bundle_parser = commands.add_parser('bundle', help='Bundle approved historical small files and directories')
    bundle_parser.add_argument('--apply', action='store_true', help='Compact large files, verify bundles, then remove loose members')
    restore_parser = commands.add_parser('restore', help='Restore a snapshot inside work; does not install')
    restore_parser.add_argument('--snapshot', required=True, help='A bundle ID or original work-relative snapshot path')
    verify_parser = commands.add_parser('verify', help='Hash-check reconstruction without extracting files')
    verify_parser.add_argument('--snapshot', help='A bundle ID or original work-relative snapshot path; omitted verifies all archives')
    args = parser.parse_args()
    if args.operation == 'compact':
        result = compact(args.apply)
        print(json.dumps({k: v for k, v in result.items() if k != 'files'}, ensure_ascii=False, indent=2))
        if not args.apply:
            print(json.dumps({'eligible_files': len(result['files']), 'eligible_bytes': sum(f['size'] for f in result['files'])}))
    elif args.operation == 'bundle':
        print(json.dumps(bundle(args.apply), ensure_ascii=False, indent=2))
    elif args.operation == 'restore':
        print(json.dumps(materialize(args.snapshot)))
    else:
        print(json.dumps(verify_all(args.snapshot), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
