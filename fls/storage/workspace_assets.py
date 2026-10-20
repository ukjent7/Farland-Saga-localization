"""Store generated previews once by content; resolve historical paths via an index."""
from contextlib import contextmanager
import hashlib
import html
import io
import json
import os
import fnmatch
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from fls.core.path_compat import REPO, WORK

ASSETS = WORK / 'assets'
IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif'}


def _relative(path):
    path = Path(path).resolve()
    if not path.is_relative_to(WORK.resolve()):
        raise ValueError('Preview paths must stay inside work')
    return path.relative_to(WORK.resolve()).as_posix()


@contextmanager
def _index_lock():
    ASSETS.mkdir(exist_ok=True)
    with (ASSETS / '.index.lock').open('a+b') as handle:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0); handle.write(b'0'); handle.flush(); handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _read_index():
    path = ASSETS / 'index.json'
    return json.loads(path.read_text()) if path.exists() else {'schema': 1, 'files': {}, 'aliases': {}}


def _write_index(index):
    temp = ASSETS / f'.index-{os.getpid()}.tmp'
    temp.write_text(json.dumps(index, ensure_ascii=False, indent=2))
    temp.replace(ASSETS / 'index.json')


def register_preview(origin, data):
    """Register bytes without leaving a duplicate at the requested output path."""
    origin = Path(origin); alias = _relative(origin)
    digest = hashlib.sha256(data).hexdigest()
    with _index_lock():
        index = _read_index()
        if digest not in index['files']:
            stem = re.sub(r'[^\w.-]+', '_', origin.stem)[:60]
            target = ASSETS / 'previews' / f'{stem}-{digest[:16]}{origin.suffix.lower()}'
            target.parent.mkdir(exist_ok=True)
            if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError('Preview content-address collision')
            target.write_bytes(data)
            index['files'][digest] = {'path': _relative(target), 'bytes': len(data)}
        index['aliases'][alias] = digest
        _write_index(index)
        return WORK / index['files'][digest]['path']


def save_preview(path, image, **options):
    buffer = io.BytesIO()
    image.save(buffer, format='PNG', **options)
    return register_preview(path, buffer.getvalue())


def preview_path(path):
    """Accept either a real path or an old stage/analysis preview name."""
    path = Path(path)
    if path.is_file():
        return path
    index = _read_index(); key = _relative(path)
    digest = index['aliases'].get(key)
    if digest is None:
        raise FileNotFoundError(path)
    result = WORK / index['files'][digest]['path']
    if not result.is_file():
        raise FileNotFoundError(result)
    return result


def inherit_previews(source, target):
    """Give a new candidate the old preview aliases, without copying pixels."""
    source_key, target_key = _relative(source) + '/', _relative(target) + '/'
    with _index_lock():
        index = _read_index()
        for key, digest in list(index['aliases'].items()):
            if key.startswith(source_key):
                index['aliases'][target_key + key[len(source_key):]] = digest
        _write_index(index)
    for path in Path(source).glob('*'):
        if path.suffix.lower() in IMAGE_SUFFIXES and path.is_file():
            register_preview(Path(target) / path.name, path.read_bytes())


def forget_preview_aliases(directory):
    """Remove aliases owned by a discarded temporary candidate, not its sources."""
    prefix=_relative(directory)+'/'
    with _index_lock():
        index=_read_index()
        index['aliases']={name:digest for name,digest in index['aliases'].items() if not name.startswith(prefix)}
        _write_index(index)


MARKDOWN_LINK = re.compile(r'(?P<prefix>!?\[[^\]\n]*\]\()(?P<url><[^>]+>|[^)\n]+)(?P<suffix>\))')
HTML_LINK = re.compile(r'(?P<prefix>\b(?:src|href)=[\"\'])(?P<url>[^\"\']+)(?P<suffix>[\"\'])')
INLINE_PATH = re.compile(r'(?P<prefix>`)(?P<url>[^`\n]+\.(?:png|jpg|jpeg|webp|bmp|gif))(?P<suffix>`)')


def _resolve_link(document, value):
    value = html.unescape(value.strip().strip('<>'))
    split = urlsplit(value)
    if split.scheme or split.netloc:
        return None
    raw = unquote(split.path)
    if Path(raw).suffix.lower() not in IMAGE_SUFFIXES:
        return None
    if raw.startswith('work/'):
        return WORK.parent / raw
    return (document.parent / raw).resolve()


def documents():
    for path in WORK.rglob('*'):
        if path.suffix.lower() not in {'.md', '.html'}:
            continue
        rel = path.relative_to(WORK)
        if any(part in {'.venv', '.uv-cache', '.pycache', '.tmp', 'vendor', 'backups', 'inputs', 'baseline', 'alpha06_baseline'} for part in rel.parts):
            continue
        # The public repository is version-controlled; never rewrite its links.
        if path.is_relative_to(REPO.resolve()):
            continue
        yield path


def image_references():
    refs = set()
    for document in documents():
        text = document.read_text(encoding='utf-8-sig')
        for pattern in [MARKDOWN_LINK, HTML_LINK, INLINE_PATH]:
            for match in pattern.finditer(text):
                path = _resolve_link(document, match['url'])
                if path is not None:
                    refs.add(path)
    return refs


def rewrite_image_links():
    index = _read_index(); changed = []
    for document in documents():
        text = document.read_text(encoding='utf-8-sig')
        def rewrite(match):
            path = _resolve_link(document, match['url'])
            if path is None or not path.is_relative_to(WORK.resolve()):
                return match.group()
            digest = index['aliases'].get(_relative(path))
            if digest is None:
                return match.group()
            target = WORK / index['files'][digest]['path']
            relative = Path(os.path.relpath(target, document.parent)).as_posix()
            return match['prefix'] + relative + match['suffix']
        revised = text
        for pattern in [MARKDOWN_LINK, HTML_LINK, INLINE_PATH]:
            revised = pattern.sub(rewrite, revised)
        if revised != text:
            document.write_text(revised)
            changed.append(_relative(document))
    return changed


def export_gallery():
    index = _read_index(); aliases = {}
    for origin, digest in index['aliases'].items():
        aliases.setdefault(digest, []).append(origin)
    cards = []
    for digest, item in index['files'].items():
        origins = sorted(aliases.get(digest, []))
        url = Path(os.path.relpath(WORK / item['path'], ASSETS)).as_posix()
        title = html.escape(origins[0] if origins else item['path'])
        search = html.escape(' '.join(origins), quote=True)
        cards.append(f'<article data-search="{search}"><a href="{url}"><img loading="lazy" src="{url}"></a><p>{title}<br>{len(origins)} 个原路径 · {item["bytes"]:,} bytes</p></article>')
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>本地化预览素材</title><style>body{font:16px/1.5 system-ui;margin:24px;background:#edf0ea}input{font:inherit;padding:8px;width:70%}main{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px;margin-top:20px}article{background:white;padding:12px;overflow-wrap:anywhere}img{max-width:100%;max-height:320px;object-fit:contain;image-rendering:pixelated}</style><h1>本地化预览素材</h1><p>相同内容只存一份。输入语言、版本或文件名筛选；点击图片查看原尺寸。</p><input id="q" placeholder="ko-KR / terrain / menu_spacing"><main>''' + ''.join(cards) + '''</main><script>document.querySelector('#q').oninput=e=>{const q=e.target.value.toLowerCase();document.querySelectorAll('article').forEach(a=>a.hidden=!a.dataset.search.toLowerCase().includes(q))}</script></html>'''
    (ASSETS / 'index.html').write_text(page)


def cleanup_images(apply=False):
    """Use the checked-in policy; remove only generated images, never native assets."""
    policy=json.loads((WORK/'config/cleanup.json').read_text())
    assert policy['schema']==1
    refs=image_references();paths=[]
    for folder in policy['generated_roots']:
        root=(WORK/folder).resolve()
        if root not in [(WORK/'analysis').resolve(),(WORK/'staging').resolve()]:
            raise ValueError('Cleanup root is not generated output')
        paths.extend(p for p in root.rglob('*') if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
                     and not any(part in {'inputs','baseline'} for part in p.relative_to(root).parts))
    inventory=[]
    for path in sorted(paths):
        key=_relative(path);data=path.read_bytes()
        obsolete=path.resolve() not in refs and any(fnmatch.fnmatchcase(key,pattern) for pattern in policy['obsolete_unreferenced_images'])
        inventory.append({'path':key,'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),
                          'action':'remove_obsolete' if obsolete else 'consolidate'})
    all_images=lambda:[p for p in WORK.rglob('*') if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
                       and not any(part in {'.venv','.pip-cache','.uv-cache','.pycache','.tmp','vendor'} for part in p.parts)
                       and not p.is_relative_to(REPO.resolve())]
    before=all_images();source_images=[p for p in before if not p.resolve().is_relative_to(ASSETS.resolve())]
    report={'applied':apply,'images_before':len(before),'source_images_before':len(source_images),
                              'source_image_bytes_before':sum(p.stat().st_size for p in source_images),
                              'bytes_before':sum(p.stat().st_size for p in before),
                              'obsolete_images':sum(row['action']=='remove_obsolete' for row in inventory),'files':inventory}
    if not apply:
        report['unique_retained_generated_images']=len({row['sha256'] for row in inventory if row['action']=='consolidate'})
        return report
    for row in inventory:
        path=WORK/row['path'];data=path.read_bytes()
        if hashlib.sha256(data).hexdigest()!=row['sha256']:
            raise RuntimeError(f'Image changed during cleanup: {path}')
        if row['action']=='consolidate':
            target=register_preview(path,data);row['canonical_path']=_relative(target)
            if target.read_bytes()!=data:raise AssertionError('Preview transfer mismatch')
        path.unlink()
    with _index_lock():
        index=_read_index()
        for row in inventory:
            if row['action']=='remove_obsolete':index['aliases'].pop(row['path'],None)
        # Verification outputs are temporary; they share production images but
        # should not appear as separate revisions in the user-facing gallery.
        for alias in list(index['aliases']):
            if alias.startswith('analysis/maintenance-tests/'):
                del index['aliases'][alias]
        referenced=set(index['aliases'].values())
        for digest,item in list(index['files'].items()):
            if digest not in referenced:
                path=WORK/item['path']
                if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise AssertionError('Asset index mismatch')
                path.unlink();del index['files'][digest]
        _write_index(index)
    report['updated_documents']=rewrite_image_links();export_gallery()
    after=all_images();report.update({'images_after':len(after),'bytes_after':sum(p.stat().st_size for p in after),
                                    'image_files_eliminated':len(before)-len(after),
                                    'bytes_reclaimed':report['bytes_before']-sum(p.stat().st_size for p in after)})
    return report


def verify_previews():
    index=_read_index()
    for digest,item in index['files'].items():
        path=WORK/item['path']
        if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise AssertionError(f'Preview checksum mismatch: {path}')
    missing=sorted(_relative(path) for path in image_references()
                   if path.is_relative_to(WORK.resolve()) and not path.exists())
    if missing:raise AssertionError(('Broken image links',missing))
    return {'unique_previews':len(index['files']),'aliases':len(index['aliases']),'broken_image_links':0}
