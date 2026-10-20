"""Reusable candidate cloning, resource validation, packing, and font syncing."""
from copy import deepcopy
import json
import hashlib
from pathlib import Path
import shutil
from fls.core.path_compat import WORK

def _work():
    """Resolve the workspace, following build_alpha when a test repoints it."""
    import sys
    build_alpha = sys.modules.get('fls.builders.build_alpha')
    if build_alpha is not None and hasattr(build_alpha, 'WORK'):
        return build_alpha.WORK
    return WORK
from fls.core.env import SOURCE, EXE
from fls.core.file_hash import sha256 as digest
from fls.containers.overlay_font import font_patch
from fls.storage.workspace_assets import IMAGE_SUFFIXES, inherit_previews
from fls.storage.version_storage import materialize


def stage_path(value):
    path = Path(value)
    path = path.resolve() if path.is_absolute() else (_work() / 'staging' / path).resolve()
    allowed = [(_work() / name).resolve() for name in ['staging', 'analysis']]
    if not any(path.is_relative_to(root) and path != root for root in allowed):
        raise ValueError('Candidates must be inside work/staging or work/analysis')
    return path


def read_report(stage):
    stage = stage_path(stage)
    for name in ['build_report.json', 'resource_report.json']:
        if (stage / name).exists():
            return json.loads((stage / name).read_text())
    raise FileNotFoundError(f'No candidate report: {stage}')


def _copy_report(report):
    """Derive a report without sharing mutable nested metadata with its source."""
    return deepcopy(report)


def verify_candidate(stage, verify_resources=True):
    stage = stage_path(stage)
    if stage.parent == (_work() / 'staging').resolve():
        materialize(stage)
    report = read_report(stage)
    if report.get('build_status', 'candidate_ready' if report.get('verification') and report.get('files') else None) != 'candidate_ready':
        raise ValueError(f'Candidate is not ready: {stage}')
    if 'FarlandSaga.bin' not in report.get('files', {}):
        raise ValueError(f'Ready candidate does not declare FarlandSaga.bin: {stage}')
    for name, item in report['files'].items():
        path = (stage / name).resolve()
        if not path.is_relative_to(stage):
            raise ValueError('Candidate file escapes its directory')
        if digest(path) != item['patched_sha256'] or path.stat().st_size != item['size']:
            raise ValueError(f'Candidate file mismatch: {name}')
    if verify_resources:
        for item in report.get('changed_resources', []):
            path = stage / 'resources' / item['path']
            if digest(path) != item['sha256'] or path.stat().st_size != item['patched_size']:
                raise ValueError(f'Candidate resource mismatch: {item["path"]}')
    return report


def _safe_candidate_file(stage, name):
    path = (stage / name).resolve()
    if (Path(name).is_absolute() or '..' in Path(name).parts or ':' in name or '\\' in name
            or not path.is_relative_to(stage)):
        raise ValueError(f'Candidate file escapes its directory: {name}')
    return path


def _changed_resource_manifest(resources):
    manifest = json.loads((SOURCE / '_manifest.json').read_text(encoding='utf-8'))['files']
    original = {row['path']: row for row in manifest}
    unknown = set(resources) - set(original)
    if unknown:
        raise ValueError(f'Candidate contains resources outside the frozen manifest: {sorted(unknown)}')
    result = []
    for name, data in sorted(resources.items()):
        source = _safe_candidate_file(SOURCE, name)
        original_data = source.read_bytes()
        entry = original[name]
        if len(original_data) != entry['size']:
            raise ValueError(f'Frozen resource size changed: {name}')
        if data != original_data:
            result.append({'path': name, 'original_size': entry['size'],
                           'patched_size': len(data),
                           'sha256': hashlib.sha256(data).hexdigest()})
    return result


def _verify_resource_only_files(stage, report, source_report):
    files = report.get('files')
    if not isinstance(files, dict):
        raise ValueError(f'Resource-only candidate has no file manifest: {stage}')
    omitted = set(report.get('omitted_files', []))
    if omitted - {'FarlandSaga.bin'}:
        raise ValueError(f'Resource-only candidate omits unsupported files: {sorted(omitted)}')
    bin_path = _safe_candidate_file(stage, 'FarlandSaga.bin')
    if not bin_path.is_file():
        if omitted != {'FarlandSaga.bin'} or 'FarlandSaga.bin' in files:
            raise ValueError(f'Resource-only candidate has an undeclared missing disc image: {stage}')
    elif 'FarlandSaga.bin' in omitted:
        raise ValueError(f'Resource-only candidate declares an existing disc image omitted: {stage}')

    for name, previous in source_report.get('files', {}).items():
        path = _safe_candidate_file(stage, name)
        current = files.get(name)
        if name == 'FarlandSaga.bin' and not path.is_file() and 'FarlandSaga.bin' in omitted:
            continue
        if current is None:
            raise ValueError(f'Resource-only candidate lost inherited file: {name}')
        if current.get('original_sha256') != previous.get('original_sha256'):
            raise ValueError(f'Resource-only candidate changed file lineage: {name}')

    inherited = set(source_report.get('files', {}))
    if 'FarlandSaga.bin' in omitted:
        inherited.discard('FarlandSaga.bin')
    if not inherited.issubset(files):
        raise ValueError(f'Resource-only candidate lost inherited files: {sorted(inherited-set(files))}')

    for name, item in files.items():
        path = _safe_candidate_file(stage, name)
        if not path.is_file():
            if name == 'FarlandSaga.bin' and 'FarlandSaga.bin' in omitted:
                continue
            raise ValueError(f'Resource-only candidate file is missing: {name}')
        if not isinstance(item.get('original_sha256'), str) or len(item['original_sha256']) != 64:
            raise ValueError(f'Resource-only candidate has invalid source hash: {name}')
        if digest(path) != item.get('patched_sha256') or path.stat().st_size != item.get('size'):
            raise ValueError(f'Resource-only candidate file mismatch: {name}')


def _verify_resource_only_resources(stage, report, source_stage):
    root = stage / 'resources'
    manifest = json.loads((SOURCE / '_manifest.json').read_text(encoding='utf-8'))['files']
    source_rows = {row['path']: row for row in manifest}
    actual_paths = {path.relative_to(root).as_posix() for path in root.rglob('*') if path.is_file()}
    source_root = source_stage / 'resources'
    source_paths = {path.relative_to(source_root).as_posix()
                    for path in source_root.rglob('*') if path.is_file()}
    if not actual_paths.issubset(source_rows):
        raise ValueError(f'Resource-only resource tree contains unknown files: {stage}')
    if not source_paths.issubset(actual_paths):
        raise ValueError(f'Resource-only candidate lost inherited resources: {sorted(source_paths-actual_paths)}')
    expected = []
    for name in sorted(actual_paths):
        entry = source_rows[name]
        source = _safe_candidate_file(SOURCE, name)
        current = _safe_candidate_file(root, name)
        original_data = source.read_bytes()
        patched_data = current.read_bytes()
        if len(original_data) != entry['size']:
            raise ValueError(f'Frozen resource size changed: {name}')
        if original_data != patched_data:
            expected.append({'path': name, 'original_size': entry['size'],
                             'patched_size': len(patched_data),
                             'sha256': hashlib.sha256(patched_data).hexdigest()})
    supplied = report.get('changed_resources')
    if supplied != expected:
        raise ValueError(f'Resource-only resource hashes do not match their source lineage: {stage}')


def verify_builder_seed(stage, seen=None):
    """Verify either a ready candidate or a resource-only stage with a ready ancestor."""
    stage = stage_path(stage)
    if stage.parent == (_work() / 'staging').resolve():
        materialize(stage)
    report = read_report(stage)
    status = report.get('build_status', 'candidate_ready' if report.get('verification') and report.get('files') else None)
    if status == 'candidate_ready':
        ready = verify_candidate(stage)
        return ready, ready
    if status != 'resources_only':
        raise ValueError(f'Candidate cannot be used as a build base: {stage}')

    seen = set() if seen is None else set(seen)
    if stage in seen:
        raise ValueError(f'Resource-only candidate lineage cycle: {stage}')
    seen.add(stage)
    source_ref = report.get('source_candidate')
    if not isinstance(source_ref, str) or not source_ref:
        raise ValueError(f'Resource-only candidate has no source lineage: {stage}')
    source_ref_path = Path(source_ref)
    if (source_ref_path.is_absolute() or '..' in source_ref_path.parts
            or '\\' in source_ref or ':' in source_ref):
        raise ValueError(f'Resource-only candidate source path is invalid: {stage}')
    source = stage_path((_work() / source_ref).resolve())
    if source == stage or report.get('previous_candidate') != source.name:
        raise ValueError(f'Resource-only candidate source lineage is invalid: {stage}')
    source_report, ready_ancestor = verify_builder_seed(source, seen)
    _verify_resource_only_files(stage, report, source_report)
    _verify_resource_only_resources(stage, report, source)
    if 'FarlandSaga.bin' not in ready_ancestor.get('files', {}):
        raise ValueError(f'Resource-only candidate has no ready ancestor: {stage}')
    return report, ready_ancestor


def clone_support_files(source, target):
    """Copy reproducibility inputs/resources, sharing indexed preview images."""
    source, target = stage_path(source), stage_path(target)
    if source == target:
        raise ValueError('Base and output must be different directories')
    if source.parent == (_work() / 'staging').resolve():
        materialize(source)
    target.mkdir(parents=True, exist_ok=True)
    excluded = {'FarlandSaga.bin', 'inner.bin', 'install.log', 'build_report.json', 'resource_report.json', 'storage_manifest.json'}
    for path in source.iterdir():
        if path.name in excluded or path.suffix.lower() in IMAGE_SUFFIXES or path.suffix.lower() in {'.md', '.log'}:
            continue
        dest = target / path.name
        if path.is_dir():
            shutil.copytree(path, dest, dirs_exist_ok=True)
        else:
            shutil.copyfile(path, dest)
    inherit_previews(source, target)


def preserve_extra_frontend_files(source, target):
    """Carry declared host patches through a full native-text rebuild."""
    source, target = stage_path(source), stage_path(target)
    report = read_report(source)
    rebuilt = {'FarlandSaga.bin', 'FarlandSaga.exe', 'S-Tribute_Data/resources.assets',
               'S-Tribute_Data/sharedassets0.assets', 'S-Tribute_Data/level0'}
    files = {name:item for name,item in report.get('files',{}).items() if name not in rebuilt}
    if files and source.parent == (_work() / 'staging').resolve():
        materialize(source)
    for name,item in files.items():
        path = (source/name).resolve()
        dest = (target/name).resolve()
        if not path.is_relative_to(source) or not dest.is_relative_to(target):
            raise ValueError('Frontend file escapes its candidate')
        if digest(path) != item['patched_sha256'] or path.stat().st_size != item['size']:
            raise ValueError(f'Frontend source mismatch: {name}')
        dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(path,dest)
    metadata = {key:value for key,value in report.items()
                if isinstance(value,dict) and value.get('container_atlases')}
    return files, metadata


class Candidate:
    def __init__(self, source, output, version=None):
        self.source, self.stage = stage_path(source), stage_path(output)
        self.prior, self.ready_ancestor = verify_builder_seed(self.source)
        if self.stage.exists():
            raise FileExistsError(f'Use a new output directory: {self.stage}')
        clone_support_files(self.source, self.stage)
        self.report = _copy_report(self.prior)
        self.report.update({'version': version or self.stage.name, 'previous_candidate': self.source.name,
                            'source_candidate': self.source.relative_to(_work()).as_posix(),
                            'build_status': 'resources_only', 'installed': False})
        self.original_resources = {p.relative_to(self.source / 'resources').as_posix(): p.read_bytes()
                                   for p in (self.source / 'resources').rglob('*') if p.is_file()}
        self.resources = dict(self.original_resources)
        self.frontend_files = {}

    def write_frontend_file(self, name, data, original_sha256):
        """Register an explicitly changed host file, including newly covered files."""
        path = self.stage / name
        if (Path(name).is_absolute() or '..' in Path(name).parts or ':' in name or '\\' in name
                or not path.resolve().is_relative_to(self.stage)):
            raise ValueError(f'Invalid frontend file path: {name}')
        if name in {'FarlandSaga.bin', 'FarlandSaga.exe'}:
            raise ValueError('Disc and executable changes use their dedicated builders')
        prior = self.prior['files'].get(name)
        if prior and prior['original_sha256'] != original_sha256:
            raise ValueError(f'Original frontend identity changed: {name}')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.frontend_files[name] = {'original_sha256': original_sha256}
        self.report['files'] = {**self.report['files'], name: {
            'original_sha256': original_sha256, 'patched_sha256': digest(path), 'size': len(data)}}

    def write_resources(self):
        for name, data in self.resources.items():
            path = self.stage / 'resources' / name
            path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)

    def sync_description_font(self):
        name = 'SYSTEM/KANJI.FON'
        if self.resources[name] == self.original_resources[name]:
            return
        generated, overlay = font_patch(EXE.read_bytes(), self.resources[name])
        prior_exe = (self.stage / 'FarlandSaga.exe').read_bytes()
        start = overlay['file_offset']; end = start + overlay['reserved_bytes']
        patched = bytearray(prior_exe); patched[start:end] = generated[start:end]
        if len(patched) != len(prior_exe) or patched[:start] != prior_exe[:start] or patched[end:] != prior_exe[end:]:
            raise AssertionError('Executable changed outside the description font')
        (self.stage / 'FarlandSaga.exe').write_bytes(patched)
        self.report['exe_patch'] = {**self.prior['exe_patch'], 'item_description_font': overlay}

    def finalize(self, resources_only=False):
        self.write_resources()
        self.sync_description_font()
        self.report['input_snapshots'] = {p.name: digest(p) for p in (self.stage / 'inputs').iterdir() if p.is_file()}
        if resources_only:
            self.report['changed_resources'] = _changed_resource_manifest(self.resources)
            omitted = []
            if not (self.stage / 'FarlandSaga.bin').is_file():
                self.report.get('files', {}).pop('FarlandSaga.bin', None)
                omitted.append('FarlandSaga.bin')
            for name, item in self.report.get('files', {}).items():
                path = _safe_candidate_file(self.stage, name)
                if not path.is_file():
                    raise ValueError(f'Resource-only output file is missing: {name}')
                item['patched_sha256'] = digest(path)
                item['size'] = path.stat().st_size
            self.report['omitted_files'] = omitted
            self._save('resource_report.json')
            return self.report
        if self.resources == self.original_resources and (self.source / 'FarlandSaga.bin').is_file():
            shutil.copyfile(self.source / 'FarlandSaga.bin', self.stage / 'FarlandSaga.bin')
        else:
            from fls.builders.build_alpha import build_disc
            self.report.update(build_disc(self.resources, stage=self.stage))
        declared_files = {**self.ready_ancestor['files'], **self.prior.get('files', {}), **self.frontend_files}
        self.report['files'] = {name: {**item, 'patched_sha256': digest(self.stage / name),
                                       'size': (self.stage / name).stat().st_size}
                                for name, item in declared_files.items()}
        for name in self.report['files']:
            if name not in {'FarlandSaga.bin', 'FarlandSaga.exe'} and name not in self.frontend_files:
                if self.report['files'][name] != self.prior['files'][name]:
                    raise AssertionError(f'Unexpected frontend change: {name}')
        self.report['build_status'] = 'candidate_ready'
        self.report.pop('omitted_files', None)
        self._save('build_report.json')
        verify_candidate(self.stage)
        return self.report

    def _save(self, name):
        (self.stage / name).write_text(json.dumps(self.report, ensure_ascii=False, indent=2))


def prune_intermediates(apply=False):
    """Delete only rebuildable inner.bin beside independently verified releases."""
    expected_size=(WORK/'decoded/FarlandSaga.bin').stat().st_size
    result={'applied':apply,'files':[],'bytes_reclaimed':0}
    for path in sorted((WORK/'staging').glob('*/inner.bin')):
        if path.is_symlink():continue
        report_path=path.parent/'build_report.json'
        if not report_path.exists():continue
        report=json.loads(report_path.read_text())
        status=report.get('build_status','candidate_ready' if report.get('verification') and report.get('files') else None)
        if status!='candidate_ready' or path.name in report.get('files',{}):continue
        if path.stat().st_size!=expected_size:raise AssertionError(f'Unexpected intermediate size: {path}')
        verify_candidate(path.parent)
        result['files'].append({'path':path.relative_to(_work()).as_posix(),'bytes':expected_size,
                                'verified_candidate':report.get('version',path.parent.name)})
    if apply:
        for item in result['files']:
            path=WORK/item['path']
            if path.stat().st_size!=item['bytes']:raise AssertionError('Intermediate changed during cleanup')
            path.unlink();result['bytes_reclaimed']+=item['bytes']
    return result


# Backward compatibility alias
import sys
base = sys.modules.get('build_alpha')
