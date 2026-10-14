"""Patch remaining Steam text fields and enable a CJK font fallback; no game launch."""
import copy
import hashlib
import json
import re
import struct
import zipfile
from pathlib import Path
from pathlib import PurePosixPath
import UnityPy
from UnityPy.helpers.TypeTreeGenerator import TypeTreeGenerator
from fontTools.ttLib import TTFont
from build_alpha import WORK, digest
from candidate import stage_path
from path_compat import REPO, local_path

UNITY_SOURCE=WORK/'assets/sources/unity'
UNITY_INPUT=WORK/'assets/sources/unity.json'
CORPUS=REPO/'translation/corpus.json'
MANAGED_ARCHIVE=WORK/'assets/sources/unity-managed.zip'
MANAGED_MANIFEST_KEY='../unity-managed.zip'
BASELINE=UNITY_SOURCE
FILES=['S-Tribute_Data/resources.assets','S-Tribute_Data/sharedassets0.assets','S-Tribute_Data/level0']

def _rows_for_language(language, rows):
    inputs=json.loads(UNITY_INPUT.read_text(encoding='utf-8'))
    if rows is None:
        corpus=json.loads(CORPUS.read_text(encoding='utf-8'))
        rows=[row for row in corpus['rows'] if row.get('kind')=='unity']
    by_id={row['id']:row for row in rows}
    assert len(inputs)==len(rows)==len(by_id)
    normalized=[]
    for before in inputs:
        row=by_id[before['id']]
        for key,value in before.items():
            if key not in {'translation','kind'}:
                assert row.get(key)==value,(before['id'],key)
        text=row.get('translation') or row.get('translations',{}).get(language)
        assert isinstance(text,str) and text.strip(),before['id']
        normalized.append({**before,'translation':text})
    return inputs,normalized


def _load_frozen_type_tree_generator(original_hashes):
    expected=original_hashes.get(MANAGED_MANIFEST_KEY)
    if not expected or not MANAGED_ARCHIVE.is_file() or digest(MANAGED_ARCHIVE)!=expected:
        raise ValueError('Frozen Unity Managed archive identity mismatch')
    generator=TypeTreeGenerator('2022.3.52f1')
    with zipfile.ZipFile(MANAGED_ARCHIVE) as archive:
        names=archive.namelist()
        if len(names)!=len(set(names)) or 'source.json' not in names:
            raise ValueError('Frozen Unity Managed archive has duplicate entries or no source manifest')
        manifest=json.loads(archive.read('source.json').decode('utf-8'))
        if manifest.get('schema_version')!=1 or manifest.get('unity_version')!='2022.3.52f1':
            raise ValueError('Unsupported frozen Unity Managed source manifest')
        files=manifest.get('files')
        if not isinstance(files,list) or not files:
            raise ValueError('Frozen Unity Managed source manifest has no DLL entries')
        rows={}
        for row in files:
            entry=row.get('archive_path')
            archive_path=PurePosixPath(entry or '')
            source_path=PurePosixPath(row.get('source_path') or '')
            if (not entry or archive_path.is_absolute() or '..' in archive_path.parts
                    or len(archive_path.parts)!=2 or archive_path.parts[0]!='Managed'
                    or archive_path.suffix.lower()!='.dll'
                    or source_path.is_absolute() or '..' in source_path.parts
                    or len(source_path.parts)!=3
                    or source_path.parts[:2]!=('S-Tribute_Data','Managed')
                    or source_path.suffix.lower()!='.dll'
                    or source_path.name!=archive_path.name or entry in rows):
                raise ValueError(f'Invalid frozen Unity Managed source row: {row!r}')
            rows[entry]=row
        if set(names)!=(set(rows)|{'source.json'}):
            raise ValueError('Frozen Unity Managed archive entries do not match source.json')
        for entry,row in sorted(rows.items()):
            payload=archive.read(entry)
            if len(payload)!=row.get('size') or hashlib.sha256(payload).hexdigest()!=row.get('sha256'):
                raise ValueError('Frozen Unity Managed DLL identity mismatch: '+entry)
            generator.load_dll(payload)
    return generator


def build(stage, rows=None, language='zh-CN'):
    stage=stage_path(stage)
    stage.mkdir(parents=True,exist_ok=True)
    inputs,rows=_rows_for_language(language,rows)
    seen={}
    pattern=r'<sprite="TMP_Buttons" index=\d+>'
    for before,row in zip(inputs,rows):
        assert all(row[k]==v for k,v in before.items() if k!='translation'),row['id']
        assert row['translation'] and len(row['translation'].encode('utf-8'))<=row['capacity_bytes'],row['id']
        assert re.findall(pattern,row['source'])==re.findall(pattern,row['translation']),row['id']
        if row['source'] in seen:assert seen[row['source']]==row['translation']
        seen[row['source']]=row['translation']
    font_path=local_path('C:/Windows/Fonts/'+{'zh-CN':'simhei.ttf','ko-KR':'malgun.ttf','en-US':'arial.ttf'}[language])
    font_name={'zh-CN':'SimHei','ko-KR':'Malgun Gothic','en-US':'Arial'}[language]
    font_bytes=font_path.read_bytes()
    cmap=TTFont(str(font_path)).getBestCmap()
    needed={ord(c) for row in rows for c in re.sub(r'<[^>]*>','',row['translation']) if ord(c)>127}
    assert needed.issubset(cmap),'Fallback source lacks '+str(needed-set(cmap))
    original_hash_path=BASELINE/'unity_original_files.json'
    original_hashes=json.loads(original_hash_path.read_text(encoding='utf-8'))
    generator=_load_frozen_type_tree_generator(original_hashes)
    for filename in ['S-Tribute_Data/globalgamemanagers',
                     'S-Tribute_Data/globalgamemanagers.assets']:
        source=UNITY_SOURCE/filename
        expected=original_hashes.get(filename)
        if not expected or digest(source)!=expected:
            raise ValueError('Frozen Unity type-tree source identity mismatch: '+filename)
    type_tree_assets=UNITY_SOURCE/'S-Tribute_Data/globalgamemanagers.assets'
    reports=[]
    for filename in FILES:
        baseline=BASELINE/filename
        expected=original_hashes.get(filename)
        if not expected or not baseline.is_file() or digest(baseline)!=expected:
            raise ValueError('Frozen Unity source identity mismatch: '+filename)
        env=UnityPy.load(str(baseline))
        source_file=env.file
        env.load_file(str(type_tree_assets))
        env.typetree_generator=generator
        objects=dict(source_file.objects)
        original_raw={path_id:o.get_raw_data() for path_id,o in objects.items()}
        patches={}
        for row in rows:
            if row['file']==filename:patches.setdefault(row['path_id'],[]).append(row)
        for path_id,strings in patches.items():
            data=bytearray(original_raw[path_id]);allowed=set()
            for row in strings:
                off,capacity=row['object_offset'],row['capacity_bytes']
                assert struct.unpack_from('<I',data,off-4)[0]==capacity,row['id']
                assert data[off:off+capacity]==row['source'].encode('utf-8'),row['id']
                payload=row['translation'].encode('utf-8')
                data[off:off+capacity]=payload+b' '*(capacity-len(payload))
                allowed.update(range(off,off+capacity))
            assert all(i in allowed for i,(a,b) in enumerate(zip(data,original_raw[path_id])) if a!=b)
            objects[path_id].set_raw_data(bytes(data))
        modified=set(patches)
        if filename.endswith('/resources.assets'):
            font=objects[155].read_typetree()
            font['m_Name']=font_name;font['m_FontNames']=[font_name];font['m_FontData']=font_bytes
            objects[155].save_typetree(font);modified.add(155)
            fallback=objects[2019].read_typetree()
            fallback['m_SourceFontFile']={'m_FileID':0,'m_PathID':155}
            fallback['m_AtlasPopulationMode']=1
            fallback['m_IsMultiAtlasTexturesEnabled']=1
            objects[2019].save_typetree(fallback);modified.add(2019)
            for path_id in [2006,2007,2008,2020]:
                tree=objects[path_id].read_typetree()
                tree['m_FallbackFontAssetTable'].append({'m_FileID':0,'m_PathID':2019})
                objects[path_id].save_typetree(tree);modified.add(path_id)
        elif filename.endswith('/sharedassets0.assets'):
            external=copy.deepcopy(source_file.externals[0])
            external.path='resources.assets'
            external.guid=bytes(16)
            external.type=0
            source_file.externals.append(external)
            file_id=len(source_file.externals)
            for path_id in [47,48,49]:
                tree=objects[path_id].read_typetree()
                tree['m_FallbackFontAssetTable'].append({'m_FileID':file_id,'m_PathID':2019})
                objects[path_id].save_typetree(tree);modified.add(path_id)
        target=stage/filename;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(source_file.save())
        # Read the serialized candidate and require unchanged bytes for every other object.
        check=UnityPy.load(str(target));candidate={o.path_id:o for o in check.objects}
        assert set(candidate)==set(original_raw)
        for path_id,obj in candidate.items():
            expected=objects[path_id].data if objects[path_id].data is not None else original_raw[path_id]
            assert obj.get_raw_data()==expected,(filename,path_id)
            if path_id not in modified:assert expected==original_raw[path_id]
        for path_id,strings in patches.items():
            data=candidate[path_id].get_raw_data()
            for row in strings:
                off,size=row['object_offset'],row['capacity_bytes']
                assert struct.unpack_from('<I',data,off-4)[0]==size
                assert data[off:off+size].decode('utf-8').rstrip()==row['translation'].rstrip(),row['id']
        reports.append({'file':filename,'original_sha256':digest(baseline),'patched_sha256':digest(target),
                        'original_size':baseline.stat().st_size,'size':target.stat().st_size,
                        'translated_strings':sum(len(v) for v in patches.values()),
                        'modified_objects':sorted(modified),'other_objects_unchanged':True})
    record={'status':'passed','translated_strings':len(rows),'unique_source_strings':len(seen),
            'font_source':'Windows '+font_name,'font_sha256':hashlib.sha256(font_bytes).hexdigest(),
            'needed_unicode_glyphs':len(needed),'font_cmap_coverage':'complete',
            'fallback':'TMP dynamic fallback, multiple atlas textures enabled',
            'files':reports,'images_modified':False,'game_launched':False}
    (stage/'unity_report.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    (stage/'inputs').mkdir(exist_ok=True)
    (stage/'inputs/unity.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    report_path=stage/'build_report.json'
    report=json.loads(report_path.read_text(encoding='utf-8'))
    report['core_translated_texts']=report.get('core_translated_texts',report['translated_texts'])
    report['host_strings']=report['exe_patch']['host_strings']
    report['unity_strings']=len(rows)
    report['translated_texts']=report['core_translated_texts']+report['host_strings']+len(rows)
    report['translation_model']='GPT-6 Luna'
    report['unity_report']='unity_report.json'
    inputs_dir=stage/'inputs'
    snapshots={p.name:digest(p) for p in sorted(inputs_dir.iterdir(),key=lambda path:path.name) if p.is_file()}
    report['input_snapshots']=snapshots
    for item in reports:
        report['files'][item['file']]={k:item[k] for k in ['original_sha256','patched_sha256','size']}
    report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    resource_report_path=stage/'resource_report.json'
    if resource_report_path.is_file():
        resource_report=json.loads(resource_report_path.read_text(encoding='utf-8'))
        resource_report['input_snapshots']=snapshots
        resource_report_path.write_text(json.dumps(resource_report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(record,ensure_ascii=False,indent=2))
    return record
