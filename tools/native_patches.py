"""Apply declarative fixed-field text and bitmap-glyph recipes to any candidate."""
import json
from pathlib import Path
import re
import shutil
from PIL import Image, ImageDraw
import build_alpha as base
from language_encoding import protected_pairs
from candidate import Candidate
from path_compat import inside_workspace
from unifont_bitmap import Unifont
from workspace_assets import save_preview


def number(value):
    return int(value, 0) if isinstance(value, str) else int(value)


def load_recipe(value):
    path = Path(value)
    if not path.is_absolute():
        path = base.REPO / 'config/patches' / (str(value) + ('' if str(value).endswith('.json') else '.json'))
    path = path.resolve()
    if not inside_workspace(path):
        raise ValueError('Recipe must be inside the repository or workspace')
    recipe = json.loads(path.read_text())
    if recipe.get('schema') != 1:
        raise ValueError('Unsupported patch recipe schema')
    return path, recipe


def _translation_path(value, label):
    path = (base.REPO / value).resolve()
    if not inside_workspace(path):
        raise ValueError(f'{label} must be inside the repository or workspace')
    return path


def _normalized_layout_text(text):
    return ''.join(text.split())


def _bind_frontend_translations(recipe, candidate):
    snapshots = []
    for atlas in recipe.get('container_atlases', []):
        source = atlas.get('translation_source')
        if not source:
            continue
        path = _translation_path(source, 'Translation source')
        document = json.loads(path.read_text(encoding='utf-8'))
        groups = {row['id']: row for row in document['groups']}
        used = {}
        for row in atlas.get('labels', []):
            identifier = row.get('translation_id')
            if not identifier:
                raise ValueError(f'Atlas label lacks translation_id: {row.get("part")}')
            group = groups.get(identifier)
            if group is None:
                raise ValueError(f'Unknown atlas translation id: {identifier}')
            translations = group['text']
            prior_layout = row.get('text', {})
            row['text'] = {
                language: (prior_layout[language]
                           if isinstance(prior_layout.get(language), str)
                           and _normalized_layout_text(prior_layout[language]) == _normalized_layout_text(text)
                           else text)
                for language, text in translations.items()
            }
            used[identifier] = group

        panel_ids = atlas.get('panel_translation_ids', {})
        if panel_ids:
            panel_text = {language: {} for language in recipe['languages']}
            prior_panel_text = atlas.get('panel_text', {})
            for key, identifier in panel_ids.items():
                group = groups.get(identifier)
                if group is None:
                    raise ValueError(f'Unknown atlas panel translation id: {identifier}')
                for language in panel_text:
                    text = group['text'][language]
                    prior_layout = prior_panel_text.get(language, {}).get(key)
                    panel_text[language][key] = (
                        prior_layout if isinstance(prior_layout, str)
                        and _normalized_layout_text(prior_layout) == _normalized_layout_text(text)
                        else text
                    )
                used[identifier] = group
            atlas['panel_text'] = panel_text

        snapshots.append({'source': source, 'groups': [used[key] for key in sorted(used)]})
    if snapshots:
        target = candidate.stage / 'inputs' / f'{recipe["id"]}.translations.json'
        target.write_text(json.dumps(snapshots, ensure_ascii=False, indent=2), encoding='utf-8')


def _bind_condition_corpus(recipe, candidate):
    source = recipe.get('translation_corpus')
    if not source:
        return
    path = _translation_path(source, 'Translation corpus')
    document = json.loads(path.read_text(encoding='utf-8'))
    corpus = {row['id']: row for row in document['rows']}
    fields = recipe.get('fields', [])
    snapshot = []
    for field in fields:
        identifier = field.get('id')
        row = corpus.get(identifier)
        if row is None or row.get('kind') != 'condition':
            raise ValueError(f'Unknown condition corpus id: {identifier}')
        for key in ('source', 'file', 'offset', 'capacity_bytes', 'mode'):
            if field.get(key) != row.get(key):
                raise ValueError(f'Condition recipe/corpus location mismatch: {identifier} ({key})')
        field['text'] = dict(row['translations'])
        snapshot.append(row)
    target = candidate.stage / 'inputs' / f'{recipe["id"]}.translations.json'
    target.write_text(json.dumps({'rows': snapshot}, ensure_ascii=False, indent=2), encoding='utf-8')


def _parts(text, language):
    if not isinstance(text, str) or not text.strip() or any(ord(ch) < 32 for ch in text):
        raise ValueError('Text must be nonempty and contain no controls')
    if language == 'en-US':
        if not all(32 <= ord(ch) <= 126 for ch in text):
            raise ValueError('English must be printable ASCII')
        return [text[i:i+2] for i in range(0, len(text), 2)]
    if language == 'ko-KR' and re.search(r'[\u3040-\u30ff\u3400-\u9fff]', text):
        raise ValueError('Unexpected Japanese/CJK in Korean text')
    return list(text)


def apply_recipe(source, output, language, recipe_value, version=None, resources_only=False, create_preview=True):
    recipe_path, recipe = load_recipe(recipe_value)
    if language not in recipe['languages']:
        raise ValueError(f'{recipe["id"]} does not support {language}')
    candidate = Candidate(source, output, version)
    built_in = recipe_path.parent == (base.REPO / 'config/patches').resolve()
    if built_in:
        _bind_frontend_translations(recipe, candidate)
        _bind_condition_corpus(recipe, candidate)
    atlas_reports = []
    if recipe.get('container_atlases'):
        from container_atlases import apply_atlases
        atlas_reports = apply_atlases(candidate, language, recipe['container_atlases'], create_preview)
    fields = [dict(row) for row in recipe.get('fields', []) if 'text' not in row or language in row['text']]
    if recipe.get('translation_file'):
        path = (base.REPO / recipe['translation_file']).resolve()
        if not inside_workspace(path):
            raise ValueError('Translation file must be inside the repository or workspace')
        draft = json.loads(path.read_text())
        assert draft.get('translator', draft.get('reviewer')) == recipe['translation_model']
        translations = {row['source']: row for row in draft['translations']}
        for row in fields:
            row['text'] = {code: translations[row['source']][key] for code, key in recipe['languages'].items()}
        shutil.copyfile(path, candidate.stage / 'inputs' / path.name)
    glyph_path = candidate.stage / 'glyph_map.json'; glyphs = json.loads(glyph_path.read_text())
    mapping = ({ch: bytes.fromhex(pair) for ch, pair in glyphs['all'].items()} if 'all' in glyphs else
               {row['token']: bytes.fromhex(row['cp932_hex']) for row in glyphs['added']})
    mapping['　'] = b'\x81\x40'
    def parts(text): return _parts(text, language)
    runtime_config=recipe.get('runtime_word_writes')
    needed = {token for row in fields for token in parts(row['text'][language])}
    if runtime_config:
        needed.update(token for row in runtime_config['labels'] for token in parts(row['text'][language]))
    missing = sorted(needed - mapping.keys())
    free = [pair for pair in protected_pairs() if pair not in mapping.values()] if missing else []
    if len(missing) > len(free):
        raise ValueError('No unused glyph slots remain')
    mapping.update(zip(missing, free)); allowed = {}; rows = []; newly_identified = 0
    original_font = candidate.resources['SYSTEM/KANJI.FON']; mono = bytearray(original_font)
    font = Unifont() if missing else None
    for token in missing:
        glyph = font.ascii_pair(token) if language == 'en-US' else font.full_cell(token)
        off = base.font_index(mapping[token]) * 32; mono[off:off+32] = glyph
        allowed.setdefault('SYSTEM/KANJI.FON', set()).update(range(off, off+32))
        added = {'token': token, 'character': token, 'cp932_hex': mapping[token].hex(), 'slot': base.font_index(mapping[token])}
        glyphs['added'].append(added)
        if 'all' in glyphs: glyphs['all'][token] = mapping[token].hex()
    blank_off = base.font_index(mapping['　']) * 32
    assert mono[blank_off:blank_off+32] == bytes(32), 'Original blank cell is not blank'
    reverse = {pair: token for token, pair in mapping.items()}
    reverse[mapping['　']] = '　'
    def encode(text): return b''.join(mapping[token] for token in parts(text))
    for check in recipe.get('mirror_checks', []):
        original = (base.SOURCE / check['file']).read_bytes()
        off, size = number(check['offset']), number(check['size']); pattern = original[off:off+size]
        hits = []
        for path in sorted(base.SOURCE.rglob('*')):
            if not path.is_file(): continue
            data = path.read_bytes(); found = data.find(pattern)
            while found >= 0:
                hits.append({'file': path.relative_to(base.SOURCE).as_posix(), 'offset': hex(found)})
                found = data.find(pattern, found+1)
        assert hits == check['expected'], hits
    for row in fields:
        name, off, size = row['file'], number(row['offset']), number(row['capacity_bytes'])
        mode, text = row['mode'], row['text'][language]
        original_record = (base.SOURCE / name).read_bytes()[off:off+size]
        source_bytes = row['source'].encode('cp932')
        if mode == 'nul': source_bytes += bytes(size-len(source_bytes))
        assert original_record == source_bytes, (name, hex(off), 'Source mismatch')
        encoded = encode(text)
        if mode == 'fixed':
            if size % 2 or len(encoded) > size: raise ValueError(f'Fixed field overflow: {row}')
            encoded += mapping['　'] * ((size-len(encoded))//2)
        elif mode == 'nul':
            if len(encoded) >= size: raise ValueError(f'NUL field overflow: {row}')
            encoded += bytes(size-len(encoded))
        else: raise ValueError(f'Unknown field mode: {mode}')
        before = candidate.resources[name][off:off+size]
        expected = source_bytes
        if row.get('before'):
            expected = encode(row['before'][language]); expected += bytes(size-len(expected))
        assert before in [expected, encoded], (name, hex(off), 'Base text mismatch')
        if row.get('new_text') and before != encoded: newly_identified += 1
        decoded = ''.join(reverse[encoded[i:i+2]] for i in range(0,len(encode(text)),2))
        assert decoded == text
        patched = bytearray(candidate.resources[name]); patched[off:off+size] = encoded
        candidate.resources[name] = bytes(patched)
        allowed.setdefault(name, set()).update(range(off, off+size))
        rows.append({'source': row['source'], 'text': text, 'offset': hex(off), 'file': name,
                     'capacity_bytes': size, 'mode': mode, 'encoded_hex': encoded.hex(),
                     'glyph_cells': size//2 if mode=='fixed' else len(encode(text))//2,
                     'render_width_pixels': (size//2 if mode=='fixed' else len(encode(text))//2)*16,
                     'overflow': False})
    for row in recipe.get('glyphs', []):
        off = base.font_index(mapping[row['character']]) * 32
        expected, replacement = bytes.fromhex(row['source_hex']), bytes.fromhex(row['bitmap_hex'])
        assert len(expected) == len(replacement) == 32 and mono[off:off+32] in [expected, replacement]
        mono[off:off+32] = replacement; allowed.setdefault('SYSTEM/KANJI.FON', set()).update(range(off,off+32))
    restored = []
    for table in recipe.get('original_ranges', []):
        name = table['file']
        original = (base.SOURCE / name).read_bytes()
        if base.digest(base.SOURCE / name) != table['source_sha256']:
            raise ValueError('Frozen resource identity mismatch: ' + name)
        patched = bytearray(candidate.resources[name])
        start, stride, count = number(table['offset']), number(table['stride']), number(table['count'])
        field_offset, size = number(table['field_offset']), number(table['size'])
        expected = bytes.fromhex(table['before_hex'])
        if stride <= 0 or count <= 0 or size <= 0 or field_offset < 0 or field_offset+size > stride or len(expected) != size:
            raise ValueError('Invalid original range declaration')
        changed = 0
        for index in range(count):
            off = start + stride*index + field_offset
            if off < 0 or off+size > len(original) or off+size > len(patched):
                raise ValueError('Original range exceeds resource')
            source_bytes = original[off:off+size]
            if patched[off:off+size] not in [expected, source_bytes]:
                raise ValueError(f'Original range base mismatch: {name} {off:#x}')
            changed += patched[off:off+size] != source_bytes
            patched[off:off+size] = source_bytes
            allowed.setdefault(name, set()).update(range(off,off+size))
        candidate.resources[name] = bytes(patched)
        restored.append({**table, 'changed_records': changed, 'all_records_match_original': True})
    candidate.resources['SYSTEM/KANJI.FON'] = bytes(mono)
    for name, data in candidate.resources.items():
        before = candidate.original_resources[name]
        assert len(data) == len(before), name
        permitted = allowed.get(name, set())
        assert all(i in permitted for i,(a,b) in enumerate(zip(before,data)) if a!=b), name
    for check in recipe.get('width_checks', []):
        off, size = number(check['offset']), number(check['capacity_bytes']); name=check['file']
        before = candidate.original_resources[name][off:off+size]
        assert candidate.resources[name][off:off+size] == before
        assert before.find(b'\0') == check['glyph_cells']*2
        assert all(row['render_width_pixels'] == check['glyph_cells']*16 for row in rows)
    glyph_path.write_text(json.dumps(glyphs, ensure_ascii=False, indent=2))
    if missing:
        font.used.update(int(line.split(':')[0],16) for line in (candidate.source/'unifont_subset.hex').read_text().splitlines())
        font.export(candidate.stage)
        candidate.report['font_source'] = json.loads((candidate.stage/'font_source.json').read_text())
    patch_report = {'recipe':recipe['id'],'language':language,'rows':rows,'record_count':len(rows),
                    'new_glyph_tokens':missing,'all_source_copies_checked':bool(recipe.get('mirror_checks')),
                    'old_glyph_assignments_preserved':True,'overflow_count':0,'missing_glyph_count':0,
                    'instructions_pointers_and_table_size_unchanged':True,'game_launched':False}
    if restored:
        patch_report['original_ranges'] = restored
    if recipe.get('verify_item_attributes'):
        base.verify_item_attributes(candidate.resources['SYSTEM/ITEM.DAT'])
        original_item = (base.SOURCE / 'SYSTEM/ITEM.DAT').read_bytes()
        original_main = (base.SOURCE / 'SYSTEM/MAIN.DAT').read_bytes()
        item_offset = original_main.index(original_item)
        assert candidate.resources['SYSTEM/MAIN.DAT'][item_offset:item_offset+len(original_item)] == candidate.resources['SYSTEM/ITEM.DAT']
    if runtime_config:
        from runtime_text import apply_runtime_text,runtime_preview
        runtime_report=apply_runtime_text(candidate,language,runtime_config,mapping,parts)
        if create_preview:runtime_report['preview']=runtime_preview(candidate,runtime_report)
        patch_report['runtime_word_writes']=runtime_report
        patch_report['runtime_record_count']=len(runtime_report['labels'])
        patch_report['instructions_pointers_and_table_size_unchanged']=False
        patch_report['instruction_opcodes_addresses_calls_and_sizes_unchanged']=True
    if atlas_reports:
        patch_report['container_atlases'] = atlas_reports
        patch_report['atlas_label_count'] = sum(len(item['labels']) for item in atlas_reports)
        patch_report['atlas_panel_count'] = sum(len(item['panels']) for item in atlas_reports)
    if rows and len({row['render_width_pixels'] for row in rows}) == 1:
        patch_report['target_width_pixels'] = rows[0]['render_width_pixels']
    key=recipe.get('report_key', recipe['id']); candidate.report[key]=patch_report
    (candidate.stage/(key+'_report.json')).write_text(json.dumps(patch_report,ensure_ascii=False,indent=2))
    shutil.copyfile(recipe_path,candidate.stage/'inputs'/recipe_path.name)
    history=list(candidate.report.get('applied_recipes',[]))
    if recipe['id'] not in history: history.append(recipe['id'])
    candidate.report['applied_recipes']=history
    for counter in ['translated_texts','core_translated_texts','catalog_lines']:
        if counter in candidate.report: candidate.report[counter] += newly_identified
    candidate.report['new_glyphs']=len(glyphs['added']); candidate.report['additional_glyphs']=len(missing)
    native_path=candidate.stage/'native_report.json'
    if native_path.exists():
        native=json.loads(native_path.read_text())
        for counter in ['translated_texts','core_translated_texts','catalog_lines']:
            if counter in native: native[counter] += newly_identified
        native.update({'new_glyphs':len(glyphs['added']),'additional_glyphs':len(missing)})
        native_path.write_text(json.dumps(native,ensure_ascii=False,indent=2))
    if rows and create_preview: _preview(candidate, recipe, rows, bytes(mono))
    candidate.report['verification']='Declarative patch: fixed capacities, exact source/base bytes, text round trips, preserved old glyph mappings and all bytes outside declared fields verified. Runtime word writes change only declared text immediates; instruction opcodes, write addresses and call targets remain unchanged. Container atlases verify encrypted readback, all pixels outside declared text regions and unchanged sprite metadata. Final packed candidates additionally verify encrypted sector readback and untouched blocks. Game not launched or installed.'
    candidate.finalize(resources_only)
    return candidate


def _preview(candidate, recipe, rows, font):
    width=max(160,40+max(row['render_width_pixels'] for row in rows))
    image=Image.new('RGB',(width,24+24*len(rows)),'#9b9e86');draw=ImageDraw.Draw(image)
    draw.text((6,4),recipe['id'],fill='black')
    for i,row in enumerate(rows):
        draw.text((6,24+i*24),str(i+1),fill='black')
        data=bytes.fromhex(row['encoded_hex'])
        for cell in range(row['glyph_cells']):
            off=base.font_index(data[cell*2:cell*2+2])*32;raw=font[off:off+32]
            for y in range(16):
                bits=int.from_bytes(raw[y*2:y*2+2],'big')
                for x in range(16):
                    if bits&(1<<(15-x)): image.putpixel((32+cell*16+x,24+i*24+y),(0,0,0))
    save_preview(candidate.stage/recipe.get('preview_name','patch_preview.png'),image)
