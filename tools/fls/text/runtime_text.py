"""Localize fixed native word writes in the host executable's runtime hooks.

Only declared mov edx, imm32 operands change. Write addresses, call targets,
instruction opcodes and every other byte retain their frozen original values.
"""
import copy
import json
from pathlib import Path
import shutil

from PIL import Image,ImageDraw
from fls.core.path_compat import WORK
from fls.core.env import EXE, font_index
from fls.core.file_hash import sha256 as digest
from workspace_assets import save_preview


def _rewrite(executable, frozen, row, encoded):
    writes=row['word_writes']
    if len(encoded)!=len(writes)*2:
        raise ValueError('Runtime text word count mismatch')
    allowed=set()
    for index,write in enumerate(writes):
        off=int(write['offset'],0)
        expected=bytes.fromhex(write['instruction_hex'])
        start=off-4;end=start+18
        if (len(expected)!=18 or expected[:4]!=bytes.fromhex('4533c0ba')
                or expected[8]!=0xb9 or expected[13]!=0xe8
                or int.from_bytes(expected[9:13],'little')!=int(write['memory_address'],0)
                or frozen[start:end]!=expected):
            raise ValueError('Frozen runtime word-write instruction mismatch')
        pair=encoded[index*2:index*2+2]
        replacement=int.from_bytes(pair,'big').to_bytes(4,'little')
        patched=expected[:4]+replacement+expected[8:]
        if bytes(executable[start:end]) not in (expected,patched):
            raise ValueError('Runtime word-write base mismatch')
        if allowed.intersection(range(off,off+4)):
            raise ValueError('Overlapping runtime word writes')
        executable[off:off+4]=replacement
        allowed.update(range(off,off+4))
    return allowed


def apply_runtime_text(candidate,language,config,mapping,parts):
    frozen=EXE.read_bytes()
    if digest(EXE)!=config['original_sha256']:
        raise ValueError('Frozen executable identity mismatch')
    path=candidate.stage/'FarlandSaga.exe';before=path.read_bytes();patched=bytearray(before)
    reports=[];allowed=set()
    for spec in config['labels']:
        text=spec['text'][language];tokens=parts(text)
        if len(tokens)>spec['capacity_cells'] or len(spec['word_writes'])!=spec['capacity_cells']:
            raise ValueError('Runtime text capacity overflow')
        encoded=b''.join(mapping[token] for token in tokens)
        encoded+=mapping['　']*(spec['capacity_cells']-len(tokens))
        permitted=_rewrite(patched,frozen,spec,encoded)
        if allowed.intersection(permitted):raise ValueError('Duplicate runtime text write')
        allowed.update(permitted)
        reports.append({**copy.deepcopy(spec),'text':text,'glyph_tokens':tokens,
                        'encoded_hex':encoded.hex(),'overflow':False})
    if len(patched)!=len(before) or any(i not in allowed for i,(a,b) in enumerate(zip(before,patched)) if a!=b):
        raise AssertionError('Runtime patch changed undeclared executable bytes')
    path.write_bytes(patched)
    candidate.report['files']['FarlandSaga.exe']={**candidate.prior['files']['FarlandSaga.exe'],
        'patched_sha256':digest(path),'size':len(patched)}
    return {'original_sha256':config['original_sha256'],'labels':reports,
            'word_write_count':sum(len(row['word_writes']) for row in reports),
            'opcodes_addresses_calls_unchanged':True,'outside_bytes_unchanged':True,
            'folder_numbers_and_save_operations_unchanged':True,'game_launched':False}


def runtime_preview(candidate,report):
    mono=candidate.resources['SYSTEM/KANJI.FON']
    image=Image.new('RGB',(360,118),'#9b9e86');draw=ImageDraw.Draw(image)
    draw.text((8,6),'Runtime save folder headings',fill='black')
    # Three hooks repeat the same two labels; render one pair at native grid size.
    for row in report['labels'][:2]:
        y=30 if row['ordinal']==1 else 68
        text=bytes.fromhex('81408140'+row['encoded_hex']+row['ordinal_word']+'81408140')
        line=Image.new('RGB',(144,16),'#9b9e86')
        for cell in range(len(text)//2):
            off=font_index(text[cell*2:cell*2+2])*32;glyph=mono[off:off+32]
            for yy in range(16):
                bits=int.from_bytes(glyph[yy*2:yy*2+2],'big')
                for xx in range(16):
                    if bits&(1<<(15-xx)):line.putpixel((cell*16+xx,yy),(0,0,0))
        image.paste(line.resize((288,32),Image.Resampling.NEAREST),(8,y))
    return str(save_preview(candidate.stage/'save_folder_preview.png',image).relative_to(WORK))


def replay_runtime_text(source,target):
    """Carry hooks through a full text rebuild with stable native glyph slots."""
    source,target=Path(source),Path(target)
    from candidate import read_report
    seed=read_report(source)
    reports={key:copy.deepcopy(value) for key,value in seed.items()
             if isinstance(value,dict) and value.get('runtime_word_writes')}
    if not reports:return {}
    path=target/'FarlandSaga.exe'
    if not path.exists():shutil.copyfile(source/'FarlandSaga.exe',path)
    before=path.read_bytes();patched=bytearray(before);frozen=EXE.read_bytes()
    seed_exe=(source/'FarlandSaga.exe').read_bytes()
    glyphs=json.loads((target/'glyph_map.json').read_text())
    mapping=(glyphs['all'] if 'all' in glyphs else
             {row.get('token',row.get('character')):row['cp932_hex'] for row in glyphs['added']})
    allowed=set()
    for key,report in reports.items():
        runtime=report['runtime_word_writes']
        if digest(EXE)!=runtime['original_sha256']:
            raise ValueError('Frozen executable identity mismatch')
        for row in runtime['labels']:
            encoded=bytes.fromhex(row['encoded_hex'])
            for index,token in enumerate(row['glyph_tokens']):
                if bytes.fromhex(mapping[token])!=encoded[index*2:index*2+2]:
                    raise ValueError('Runtime hook glyph assignment changed during rebuild')
            for index,write in enumerate(row['word_writes']):
                off=int(write['offset'],0)
                word=int.from_bytes(encoded[index*2:index*2+2],'big')
                if seed_exe[off:off+4]!=word.to_bytes(4,'little'):
                    raise ValueError('Seed runtime hook mismatch')
            allowed.update(_rewrite(patched,frozen,row,encoded))
        (target/(key+'_report.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2))
    if any(i not in allowed for i,(a,b) in enumerate(zip(before,patched)) if a!=b):
        raise AssertionError('Runtime replay changed undeclared executable bytes')
    path.write_bytes(patched)
    return reports
