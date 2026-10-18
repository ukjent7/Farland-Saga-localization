"""Build full Chinese, Korean, and English candidates from the shared corpus."""
import argparse
import collections
import json
import shutil
from pathlib import Path
from PIL import Image
import build_alpha as base
import language_encoding
from path_compat import REPO
from build_ui_labels import palette, bitmap, save, read_atlas
from merge_full_translations import validate
from unifont_bitmap import Unifont
from candidate import stage_path, preserve_extra_frontend_files, verify_builder_seed
from workspace_assets import save_preview, inherit_previews
from translation_data import LANGUAGES, read_corpus, read_corpus_rows

VERSIONS={'ko':('ko-KR-unifont-full07','ko-KR-unifont-full08'),
          'en':('en-US-unifont-full07','en-US-unifont-full08'),
          'zh-CN':('zh-CN-unifont07','zh-CN-unifont08')}
LANGUAGE_NAMES={'ko':'ko-KR','en':'en-US'}
TEXT_KINDS={'event','catalog','description','host','unity'}
ALL_BUILD_KINDS=TEXT_KINDS|{'condition'}


def resources(language,rows,translated,seed,stage,*,enforce_capacity=True):
    core=[r for r in rows if r['kind'] in ('event','catalog','description')]
    condition_rows=read_corpus_rows(LANGUAGE_NAMES[language],{'condition'})
    translated={key:dict(value) for key,value in translated.items()}
    for row in core:
        text=translated[row['id']]['translation']
        translated[row['id']]['translation']=base.catalog_text(row,text,language)
    vocabulary=sorted({t for r in core for t in language_encoding.tokens(translated[r['id']]['translation'],language)}
                      | {t for r in condition_rows for t in language_encoding.tokens(r['translation'],language)})
    old_map=json.loads((seed/'glyph_map.json').read_text())['added']
    mapping={r['token']:bytes.fromhex(r['cp932_hex']) for r in old_map}
    free=[pair for pair in language_encoding.protected_pairs() if pair not in mapping.values()]
    missing=[token for token in vocabulary if token not in mapping]
    assert len(missing)<=len(free),('Unused glyph slots',len(missing),len(free))
    mapping.update(zip(missing,free));assert len(set(mapping.values()))==len(mapping)
    placeholders={token:chr(0xE000+i) for i,token in enumerate(mapping)}
    native={placeholders[token]:pair for token,pair in mapping.items()};native['\u3000']=b'\x81\x40'
    font=Unifont();mono=bytearray((base.SOURCE/'SYSTEM/KANJI.FON').read_bytes())
    for token,pair in mapping.items():
        glyph=font.ascii_pair(token) if language=='en' else font.full_cell(token)
        off=base.font_index(pair)*32;mono[off:off+32]=glyph
    reverse={pair:token for token,pair in mapping.items()}
    def encode(text):
        result=b''.join(mapping[t] for t in language_encoding.tokens(text,language))
        assert ''.join(reverse[result[i:i+2]] for i in range(0,len(result),2))==text
        return result
    changes={'SYSTEM/KANJI.FON':bytes(mono)};catalog=[];event_rows=[]
    for row in core:
        text=translated[row['id']]['translation']
        if row['kind']=='event':
            event_rows.append({**row,'translation':''.join(placeholders[t] for t in language_encoding.tokens(text,language))})
            continue
        name=row['file'];original=(base.SOURCE/name).read_bytes();off=int(row['offset'],16)
        size=base.catalog_capacity(row);assert original[off:off+size].split(b'\0')[0].decode('cp932')==row['source'],row['id']
        encoded=encode(text);assert len(encoded)<size,row['id']
        patched=bytearray(changes.get(name,original));patched[off:off+size]=encoded+bytes(size-len(encoded))
        assert len(patched)==len(original);changes[name]=bytes(patched)
        if row['kind']=='catalog':catalog.append({**row,'translation':text})
    changes,mirrors=base.sync_resource_mirrors(changes,catalog)
    events,padding,capacity=base.build_events(
        event_rows,native,enforce_capacity=enforce_capacity)
    capacity_overflows=base.capacity_overflows(capacity)
    changes.update(events)
    for row in condition_rows:
        name=row['file'];off=int(row['offset'],16);size=int(row['capacity_bytes'])
        original=(base.SOURCE/name).read_bytes()
        assert original[off:off+size].decode('cp932')==row['source'],row['id']
        text=row['translation'];encoded=encode(text)
        assert row.get('mode')=='fixed' and size%2==0 and len(encoded)<=size,row['id']
        encoded+=b'\x81\x40'*((size-len(encoded))//2)
        patched=bytearray(changes.get(name,original))
        patched[off:off+size]=encoded;changes[name]=bytes(patched)
    # Merge the frozen title/terrain/equipment resources while retaining newly
    # translated tables in MAIN.DAT and the new language font.
    ui_main=(seed/'resources/SYSTEM/MAIN.DAT').read_bytes()
    main=bytearray(changes['SYSTEM/MAIN.DAT'])
    for off,size in [(0x2730,63744),(0x12030,98304)]:main[off:off+size]=ui_main[off:off+size]
    changes['SYSTEM/MAIN.DAT']=bytes(main)
    for p in (seed/'resources').rglob('*'):
        if not p.is_file():continue
        name=p.relative_to(seed/'resources').as_posix()
        if name.startswith(('TITLE/','SYSTEM/SUBMAP')) or name in {
            'SYSTEM/MENU.CEL','SYSTEM/MENU_S.SPR','SYSTEM/RESET.DAT','SYSTEM/N_MENU.DAT'}:
            assert name not in changes,name;changes[name]=p.read_bytes()
    # The shared attribute label belongs to the same trilingual UI-label table.
    ui_labels=json.loads((REPO/'translation/ui_labels.json').read_text(encoding='utf-8'))
    extra=[row for row in ui_labels['stats'] if row['source']=='属性']
    if extra:
        assert len(extra)==1
        text=extra[0][LANGUAGE_NAMES[language]];ink=bitmap(font,text);assert ink.width<=32 and ink.height==16
        region=Image.new('P',(32,16))
        for y in range(16):
            for x in range(32):
                region.putpixel((x,y),11 if x<ink.width and ink.getpixel((x,y))
                                else 43 if y%2 and not x%2 else 50)
        old_cel=changes['SYSTEM/MENU.CEL'];cel=bytearray(old_cel)
        for ty in range(2):
            off=(0x418+128*ty)*32
            for tx in range(4):
                cel[off+64*tx:off+64*(tx+1)]=region.crop((8*tx,8*ty,8*tx+8,8*ty+8)).tobytes()
        ui=json.loads((stage/'ui_labels_report.json').read_text())
        for mirror in ui['stats']['mirrors']:
            name=mirror['resource'];off=int(mirror['offset'],16);size=mirror['size']
            data=bytearray(changes[name]);assert data[off:off+size]==old_cel[:size]
            data[off:off+size]=cel[:size];changes[name]=bytes(data)
        ui['stats']['localized_labels']=8
        ui['stats']['rows']=[row for row in ui['stats']['rows'] if row['source']!='属性']
        ui['stats']['rows'].append({'source':'属性','text':text,'ink':list(ink.size),
                                    'atlas_x':96,'atlas_tile_rows':[16,18],'overflow':False})
        save(stage/'ui_labels_report.json',ui)
        atlas=read_atlas(cel);strip=Image.new('P',(256,16));strip.putpalette(atlas.getpalette())
        strip.paste(atlas.crop((0,128,256,136)),(0,0));strip.paste(atlas.crop((0,144,256,152)),(0,8))
        save_preview(stage/'stats_preview.png',strip)
    # Keep declared field patches from a later seed when their logical wording
    # still matches the corpus; fixed tables need no separate version builder.
    from candidate import read_report
    seed_report=read_report(seed);preserved={};additional=0
    known={(row['file'],int(row['offset'],16)):row for row in core if row['kind']!='event'}
    condition_positions={(row['file'],int(row['offset'],16)):row for row in condition_rows}
    known.update(condition_positions)
    all_known=dict(known)
    all_known.update(condition_positions)
    for key,value in seed_report.items():
        if not isinstance(value,dict) or not value.get('rows'):continue
        if not all('encoded_hex' in row and 'offset' in row and 'capacity_bytes' in row for row in value['rows']):continue
        keep=True
        for row in value['rows']:
            name=row.get('file','1ST.BIN');off=int(row['offset'],16);size=int(row['capacity_bytes'])
            encoded=bytes.fromhex(row['encoded_hex']);assert len(encoded)==size
            assert (seed/'resources'/name).read_bytes()[off:off+size]==encoded
            ordinary=all_known.get((name,off));text=row.get('text',row.get('after',''))
            if ordinary:
                current=translated[ordinary['id']]['translation'] if ordinary['id'] in translated else ordinary['translation']
                if text.replace('　','')!=current.replace('　',''):
                    keep=False;break
        if not keep:continue
        for row in value['rows']:
            name=row.get('file','1ST.BIN');off=int(row['offset'],16);size=int(row['capacity_bytes'])
            patched=bytearray(changes.get(name,(base.SOURCE/name).read_bytes()))
            patched[off:off+size]=bytes.fromhex(row['encoded_hex']);changes[name]=bytes(patched)
            if (name,off) not in known:additional+=1
        preserved[key]=value
    for name,data in changes.items():
        path=stage/'resources'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    for r in old_map:assert mapping[r['token']]==bytes.fromhex(r['cp932_hex']),r['token']
    save(stage/'glyph_map.json',{'language':language,'added':[
        {'token':t,'cp932_hex':p.hex(),'slot':base.font_index(p)} for t,p in mapping.items()],
        'packing':'two ASCII characters per glyph' if language=='en' else 'one character per glyph',
        'font':'GNU Unifont 18.0.01','previous_assignments_preserved':True})
    font.used.update(int(line.split(':')[0],16) for line in (seed/'unifont_subset.hex').read_text().splitlines())
    font.export(stage)
    save(stage/'resource_mirrors.json',mirrors);save(stage/'event_capacity.json',capacity)
    save(stage/'dialogue_padding.json',padding)
    counts=collections.Counter(r['kind'] for r in core)
    result={'core_translated_texts':len(core)+len(condition_rows)+additional,
            'translated_texts':len(core)+len(condition_rows)+additional,
            'condition_label_count':len(condition_rows),'event_lines':counts['event'],
            'event_files':len(capacity),'catalog_lines':counts['catalog'],'description_lines':counts['description'],
            'new_glyphs':len(mapping),'additional_glyphs':len(missing),'available_original_glyph_slots':len(free)+len(old_map),
            'previous_glyph_assignments_preserved':True,'compacted_event_tables':sum(r['compacted_table'] for r in capacity),
            'removed_blank_glyphs':sum(r['removed_blank_glyphs'] for r in padding),
            'packable':not capacity_overflows,'capacity_overflows':capacity_overflows,
            'packing_block_reason':('ISO extent capacity exceeded; '+ '; '.join(
                f"{r['file']} needs {r['required_bytes']} bytes, {r['available_bytes']} allocated "
                f"({r['overflow_bytes']} byte overflow)" for r in capacity_overflows))
                if capacity_overflows else None}
    result.update(preserved);result['preserved_additional_field_texts']=additional
    save(stage/'native_report.json',result)
    return changes,result


def _overlay_span(resources_by_name,name,offset,payload):
    """Replace one complete text interval, including every old tail byte."""
    if name not in resources_by_name:
        raise KeyError(f'Missing candidate resource: {name}')
    offset=int(offset);current=resources_by_name[name]
    if offset<0 or offset+len(payload)>len(current):
        raise ValueError(f'Text span escapes resource: {name} {offset:#x}+{len(payload):#x}')
    patched=bytearray(current);patched[offset:offset+len(payload)]=payload
    resources_by_name[name]=bytes(patched)


def _copy_fresh_span(target,fresh,name,offset,size):
    if name not in fresh:
        raise KeyError(f'Missing canonical resource: {name}')
    offset,size=int(offset),int(size)
    payload=fresh[name][offset:offset+size]
    if len(payload)!=size:
        raise ValueError(f'Canonical text span is truncated: {name} {offset:#x}+{size:#x}')
    _overlay_span(target,name,offset,payload)


def _encode_zh_condition(row,mapping):
    size=int(row['capacity_bytes'])
    if row.get('mode')!='fixed' or size%2:
        raise ValueError(f'Invalid Chinese condition field: {row["id"]}')
    tokens=language_encoding.tokens(row['translation'],'zh')
    encoded=b''.join(mapping[token] for token in tokens)
    blank=mapping.get('　',b'\x81\x40')
    if len(encoded)>size or len(blank)!=2:
        raise ValueError(f'Chinese condition field overflow: {row["id"]}')
    payload=encoded+blank*((size-len(encoded))//2)
    if len(payload)!=size:
        raise ValueError(f'Chinese condition field has odd padding: {row["id"]}')
    return payload


def _remove_legacy_input_dumps(inputs):
    obsolete={'events.json','catalog.json','catalog.tsv','events.tsv',
              'item_descriptions.zh-CN.json','unit_descriptions.zh-CN.json',
              'skill_descriptions.zh-CN.json','host_strings.zh-CN.json',
              'unity_output.json','input_all.json','output_all.json','terms.json',
              'extra_ui_labels.json','condition_labels_luna.json'}
    for path in list(inputs.rglob('*')):
        if path.is_file() and path.name in obsolete:
            path.unlink()
    for path in sorted((p for p in inputs.rglob('*') if p.is_dir()),reverse=True):
        try:path.rmdir()
        except OSError:pass


def patch_host(rows,translated,language,stage):
    report=base.patch_exe(include_host_strings=False);path=stage/'FarlandSaga.exe';data=bytearray(path.read_bytes())
    before=bytes(data);allowed=set();count=0
    for row in rows:
        if row['kind']!='host':continue
        off=int(row['offset'],16);size=int(row['capacity_bytes']);text=translated[row['id']]['translation']
        assert data[off:off+size]==row['source'].encode('utf-8')+b'\0',row['id']
        encoded=text.encode('utf-8');assert len(encoded)<size,row['id']
        data[off:off+size]=encoded+bytes(size-len(encoded));allowed.update(range(off,off+size));count+=1
        assert data[off:off+size].split(b'\0')[0].decode('utf-8')==text
    assert len(data)==len(before) and all(i in allowed for i,(a,b) in enumerate(zip(before,data)) if a!=b)
    path.write_bytes(data);report['host_strings']=count
    return report


def build_chinese(seed,stage,resources_only):
    """Rebuild Chinese text from the corpus while carrying current UI patches."""
    from candidate import Candidate, read_report, verify_builder_seed

    verify_builder_seed(seed)
    base.STAGE=stage
    candidate=Candidate(seed,stage,version=stage.name)
    input_dir=stage/'inputs'
    _remove_legacy_input_dumps(input_dir)
    unity_src = REPO / 'config/unity.json' if (REPO / 'config/unity.json').is_file() else base.WORK / 'assets/sources/unity.json'
    glyph_src = REPO / 'config/glyph-slots.zh-CN.json' if (REPO / 'config/glyph-slots.zh-CN.json').is_file() else base.WORK / 'assets/sources/glyph-slots.zh-CN.json'
    source_files=[REPO/'translation/corpus.json',REPO/'translation/glossary.json',
                  REPO/'translation/ui_labels.json',REPO/'translation/titles.json',
                  REPO/'translation/frontend_submenus.json',
                  unity_src, glyph_src]
    for source in source_files:
        if source.exists():shutil.copyfile(source,input_dir/source.name)

    try:
        changes,native=base.build_resources('unifont',enforce_capacity=not resources_only)
    except base.ResourceCapacityError:
        shutil.rmtree(stage)
        raise
    language_rows=read_corpus_rows('zh-CN')
    for row in language_rows:
        if row.get('kind') in ALL_BUILD_KINDS:
            validate(row,language='zh-CN')

    # Rebuild fonts and event resources from complete canonical outputs. Fixed
    # records below use whole declared intervals so stale suffixes are cleared.
    candidate.resources['SYSTEM/KANJI.FON']=changes['SYSTEM/KANJI.FON']
    event_rows=[row for row in language_rows if row.get('kind')=='event']
    event_files={row['file'] for row in event_rows}
    if not event_files.issubset(changes):
        raise AssertionError(f'Fresh event build omitted files: {sorted(event_files-set(changes))}')
    for name in event_files:
        candidate.resources[name]=changes[name]

    canonical_positions=set()
    catalog_rows=[row for row in language_rows if row.get('kind')=='catalog']
    description_rows=[row for row in language_rows if row.get('kind')=='description']
    for row in catalog_rows:
        offset=int(row['offset'],16);size=base.catalog_capacity(row)
        _copy_fresh_span(candidate.resources,changes,row['file'],offset,size)
        canonical_positions.add((row['file'],offset))
    for row in description_rows:
        offset=int(row['offset'],16);size=int(row['capacity_bytes'])
        _copy_fresh_span(candidate.resources,changes,row['file'],offset,size)
        canonical_positions.add((row['file'],offset))

    # MAIN.DAT contains runtime mirrors of item/menu-info blocks and selected
    # unit/magic names. Synchronize those exact text intervals only.
    mirrors=json.loads((stage/'resource_mirrors.json').read_text(encoding='utf-8'))
    mirror_bases={entry['resource']:int(entry['offset'],16) for entry in mirrors
                  if entry['resource'] in {'SYSTEM/ITEM.DAT','SYSTEM/MENUINFO.DAT'}}
    corpus_ids={row['id'] for row in catalog_rows}
    mirror_rows={entry['resource']:entry for entry in mirrors if entry['resource'] in corpus_ids}
    for row in catalog_rows+description_rows:
        if row['file'] in mirror_bases:
            target_offset=mirror_bases[row['file']]+int(row['offset'],16)
        else:
            mirror=mirror_rows.get(row['id'])
            if mirror is None:continue
            target_offset=int(mirror['offset'],16)
        size=base.catalog_capacity(row) if row['kind']=='catalog' else int(row['capacity_bytes'])
        _copy_fresh_span(candidate.resources,changes,'SYSTEM/MAIN.DAT',target_offset,size)

    # Use the same stable font map as other Chinese text, so edited condition
    # labels can contain newly added Chinese glyphs.
    condition_rows=[row for row in language_rows if row.get('kind')=='condition']
    glyph_map=json.loads((stage/'glyph_map.json').read_text(encoding='utf-8'))
    glyph_mapping={character:bytes.fromhex(code) for character,code in glyph_map['all'].items()}
    condition_source_preserved=0
    for row in condition_rows:
        name=row['file'];offset=int(row['offset'],16);size=int(row['capacity_bytes'])
        source=(base.SOURCE/name).read_bytes();source_bytes=row['source'].encode('cp932')
        if source[offset:offset+len(source_bytes)]!=source_bytes:
            raise AssertionError(f'Frozen condition source changed: {row["id"]}')
        payload=_encode_zh_condition(row,glyph_mapping)
        condition_source_preserved+=payload==source[offset:offset+size]
        _overlay_span(candidate.resources,name,offset,payload)
        canonical_positions.add((name,offset))

    seed_report=read_report(seed);preserved={};additional=0
    by_position={(row['file'],int(row['offset'],16)):row for row in language_rows
                 if row.get('file') and row.get('offset')}
    for key,value in seed_report.items():
        if not isinstance(value,dict) or not value.get('rows'):continue
        if not all('encoded_hex' in row and 'offset' in row and 'capacity_bytes' in row for row in value['rows']):continue
        keep=True
        for row in value['rows']:
            name=row.get('file','1ST.BIN');offset=int(row['offset'],16)
            size=int(row['capacity_bytes']);encoded=bytes.fromhex(row['encoded_hex'])
            assert len(encoded)==size
            seed_data=candidate.original_resources.get(name,(base.SOURCE/name).read_bytes())
            assert seed_data[offset:offset+size]==encoded,(key,name,hex(offset))
            current=by_position.get((name,offset));old_text=row.get('text',row.get('after',''))
            if current and old_text.replace('　','')!=current['translation'].replace('　',''):
                keep=False;break
        if not keep:continue
        for row in value['rows']:
            name=row.get('file','1ST.BIN');offset=int(row['offset'],16);size=int(row['capacity_bytes'])
            _overlay_span(candidate.resources,name,offset,bytes.fromhex(row['encoded_hex']))
            if (name,offset) not in canonical_positions:additional+=1
        preserved[key]=value

    native.update(preserved)
    native['preserved_additional_field_texts']=additional
    native['condition_label_count']=len(condition_rows)
    native['condition_labels_unchanged_from_source']=condition_source_preserved==len(condition_rows)
    native['condition_labels_matching_source']=condition_source_preserved
    native['core_translated_texts']=native['translated_texts']+len(condition_rows)
    native['translated_texts']=native['core_translated_texts']
    save(stage/'native_report.json',native)
    host_count=sum(row.get('kind')=='host' for row in language_rows)
    unity_count=sum(row.get('kind')=='unity' for row in language_rows)
    candidate.report.update({
        'version':stage.name,'language':'zh-CN','previous_candidate':seed.name,
        'translation_model':'GPT-6 Luna','translation_source':'original Japanese Steam resources',
        'core_translated_texts':native['core_translated_texts'],
        'host_strings':host_count,'unity_strings':unity_count,
        'condition_label_count':len(condition_rows),
        'translated_texts':native['core_translated_texts']+host_count+unity_count,
        'native_report':'native_report.json','build_status':'resources_only',
        'packable':native['packable'],'capacity_overflows':native['capacity_overflows'],
        'packing_block_reason':native['packing_block_reason'],
        'installed':False,
        'scope':f'All {len(language_rows) - len(condition_rows)} shared corpus text entries plus {len(condition_rows)} condition labels; current three-language UI themes preserved',
        'native_resources':native,
    })
    candidate.report['exe_patch']=base.patch_exe()
    from runtime_text import replay_runtime_text
    runtime_reports=replay_runtime_text(seed,stage)
    candidate.report.update(runtime_reports)
    if 'save_folder' in runtime_reports:
        assert runtime_reports['save_folder']['runtime_word_writes']['labels']
    if resources_only:
        result=candidate.finalize(resources_only=True)
        print('zh-CN all',result['translated_texts'],'native entries prepared from shared corpus',flush=True)
        return result

    candidate.finalize(resources_only=False)
    from unity_resources import build as build_unity
    build_unity(stage,read_corpus_rows('zh-CN',{'unity'}),'zh-CN')
    final=read_report(stage)
    expected=sum(row.get('kind') in ALL_BUILD_KINDS for row in language_rows)
    assert final['translated_texts']==expected,(final['translated_texts'],expected)
    print('Full candidate verified:',stage,flush=True)
    return final


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--language',required=True,choices=['zh-CN','ko','en','ko-KR','en-US'])
    parser.add_argument('--base',help='UI seed candidate under work/staging')
    parser.add_argument('--output',help='Output candidate under work/staging')
    parser.add_argument('--resources-only',action='store_true');args=parser.parse_args()
    language={'ko-KR':'ko','en-US':'en'}.get(args.language,args.language)
    old_name,new_name=VERSIONS[language]
    seed=stage_path(args.base or old_name);stage=stage_path(args.output or new_name)
    if seed==stage:raise ValueError('Base and output must differ')
    if stage.exists():raise FileExistsError(f'Use a new output directory: {stage}')
    verify_builder_seed(seed)
    if language=='zh-CN':
        build_chinese(seed,stage,args.resources_only)
        return
    stage.mkdir(parents=True);base.STAGE=stage
    language_name=LANGUAGE_NAMES[language]
    corpus=read_corpus()
    all_rows=[row for row in corpus['rows'] if row.get('kind') in ALL_BUILD_KINDS]
    rows=[row for row in all_rows if row.get('kind') in TEXT_KINDS]
    if len(all_rows)!=len(corpus['rows']):
        unsupported=sorted({row.get('kind') for row in corpus['rows'] if row.get('kind') not in ALL_BUILD_KINDS})
        raise ValueError(f'Corpus contains rows outside full-builder coverage: {unsupported}')
    for row in all_rows:validate(row,language=language_name)
    translated={row['id']:row for row in read_corpus_rows(language_name,TEXT_KINDS)}
    inputs=stage/'inputs';inputs.mkdir(exist_ok=True)
    if (seed/'inputs').exists():
        for path in (seed/'inputs').iterdir():
            target=inputs/path.name
            if path.is_dir():shutil.copytree(path,target,dirs_exist_ok=True)
            else:shutil.copyfile(path,target)
    _remove_legacy_input_dumps(inputs)
    shutil.copytree(seed/'font_license',stage/'font_license',dirs_exist_ok=True)
    for source in [REPO/'translation/corpus.json',REPO/'translation/glossary.json',
                   REPO/'translation/ui_labels.json',REPO/'translation/titles.json',
                   REPO/'translation/frontend_submenus.json']:
        if source.exists():shutil.copyfile(source,inputs/source.name)
    inherit_previews(seed,stage)
    for name in ['ui_labels_report.json','equipment_labels_report.json','title_layout_report.json']:
        if (seed/name).exists():shutil.copyfile(seed/name,stage/name)
    try:
        changes,report=resources(language,rows,translated,seed,stage,
                                 enforce_capacity=not args.resources_only)
    except base.ResourceCapacityError:
        shutil.rmtree(stage)
        raise
    frontend_files,frontend_metadata=preserve_extra_frontend_files(seed,stage)
    report.update(frontend_metadata)
    from runtime_text import replay_runtime_text
    report.update(replay_runtime_text(seed,stage))
    if frontend_files:report['preserved_frontend_files']=frontend_files
    manifest={r['path']:r for r in json.loads((base.SOURCE/'_manifest.json').read_text())['files']}
    for name in changes:assert base.digest(base.SOURCE/name)==manifest[name]['sha256'],name
    report.update({'version':stage.name if args.output else ('ko-KR' if language=='ko' else 'en-US')+' Unifont full08',
                   'translation_model':'gpt-6-luna','translation_source':'original Japanese Steam resources',
                   'third_party_korean_patch_used':False,'installed':False,'previous_candidate':seed.name,
                   'build_status':'resources_only',
                   'ui_labels':json.loads((stage/'ui_labels_report.json').read_text()),
                   'title_images':json.loads((stage/'title_layout_report.json').read_text()),
                   'font_source':json.loads((stage/'font_source.json').read_text()),
                   'original_resource_hashes':{name:manifest[name]['sha256'] for name in changes},
                   'scope':f'All {len(rows)} shared corpus text entries plus {len(all_rows) - len(rows)} condition labels; preserve trilingual picture and frontend themes',
                   'build_status':'resources_only','input_snapshots':{p.name:base.digest(p) for p in inputs.iterdir() if p.is_file()}})
    save(stage/'resource_report.json',report)
    print(language,'all',report['core_translated_texts'],'native entries encoded and verified',flush=True)
    if args.resources_only:return
    report.update(base.build_disc(changes));report['exe_patch']=patch_host(rows,translated,language,stage)
    report.update(replay_runtime_text(seed,stage))
    report['files']={name:{'original_sha256':base.digest(base.original_file(name)),
                           'patched_sha256':base.digest(stage/name),'size':(stage/name).stat().st_size}
                     for name in ['FarlandSaga.bin','FarlandSaga.exe']}
    report['files'].update(frontend_files)
    report['build_status']='candidate_ready'
    report['verification']='All known native text encoded and recovered exactly; original script instructions/entry addresses and resource sizes retained; runtime text and UI mirrors synchronized; stable prior glyph slots retained; final changed sectors independently decoded; untouched container blocks preserved; Steam overlay font synchronized; no game launch or installation.'
    save(stage/'build_report.json',report)
    unity_rows=[row for row in translated.values() if row['kind']=='unity']
    from unity_resources import build
    build(stage,unity_rows,language_name)
    final=json.loads((stage/'build_report.json').read_text())
    assert final['translated_texts']==len(rows)+report['condition_label_count']+report['preserved_additional_field_texts']
    assert len(final['files'])==5+len(frontend_files)
    print('Full candidate verified:',stage,flush=True)


if __name__=='__main__':main()
