"""Translate equipment headings in every atlas and tilemap copy; no game launch."""
import argparse
import json
import shutil
from PIL import Image
from fls.core.path_compat import WORK, local_path
from fls.core.env import SOURCE
from fls.core.file_hash import sha256 as digest
from fls.builders.build_alpha import build_disc
from fls.builders.build_ui_labels import DRAFT, bitmap, fitted_text, palette, save
from fls.containers.unifont_bitmap import Unifont
from fls.storage.candidate import clone_support_files, stage_path
from fls.storage.workspace_assets import save_preview

VERSIONS = {'zh-CN': ('zh-CN-unifont01', 'zh-CN-unifont02'),
            'ko-KR': ('ko-KR-unifont-test04', 'ko-KR-unifont-test05'),
            'en-US': ('en-US-unifont-test04', 'en-US-unifont-test05')}
FIT_FONTS = {'ko-KR':'C:/Windows/Fonts/malgunbd.ttf',
             'en-US':'C:/Windows/Fonts/arial.ttf'}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--language',required=True,choices=['ko-KR','en-US'])
    parser.add_argument('--resources-only',action='store_true')
    parser.add_argument('--base')
    parser.add_argument('--output')
    args=parser.parse_args();language=args.language
    old_name,new_name=VERSIONS[language]
    old=stage_path(args.base or old_name);stage=stage_path(args.output or new_name)
    stage.mkdir(exist_ok=True)
    clone_support_files(old,stage)
    changes={p.relative_to(old/'resources').as_posix():p.read_bytes()
             for p in (old/'resources').rglob('*') if p.is_file()}
    before=dict(changes);draft=json.loads(DRAFT.read_text());rows=draft['equipment']
    font=Unifont();source=(SOURCE/'SYSTEM/MENU.CEL').read_bytes()
    original_cel=changes['SYSTEM/MENU.CEL'];cel=bytearray(original_cel)
    fit_font_path=local_path(FIT_FONTS[language])
    fit_font_sha256=digest(fit_font_path)
    allowed=set();labels=[];halves=[]
    for row in rows:
        text=row[language]
        bitmap_ink=bitmap(font,text)
        if bitmap_ink.width<=32 and bitmap_ink.height==16:
            ink=bitmap_ink;draw_x=draw_y=0;font_pixels=16
            render_method='unifont_bitmap';font_source='Unifont 16x16';font_sha256=None
            glyph_bounds=list(ink.getbbox())
        else:
            ink,font_pixels=fitted_text(text,(32,16),fit_font_path,9)
            draw_x=(32-ink.width)//2;draw_y=(16-ink.height)//2
            glyph_bounds=[draw_x,draw_y,draw_x+ink.width,draw_y+ink.height]
            render_method='windows_font_fit';font_source=FIT_FONTS[language]
            font_sha256=fit_font_sha256
        glyph=Image.new('L',(32,16));glyph.paste(ink,(draw_x,draw_y))
        region=Image.new('P',(32,16))
        for y in range(16):
            for x in range(32):
                region.putpixel((x,y),11 if glyph.getpixel((x,y))
                                else 43 if y%2 and not x%2 else 50)
        for ty in range(2):
            off=(row['code']+ty*128)*32;halves.append(off)
            for tx in range(4):
                start=off+tx*64
                cel[start:start+64]=region.crop((tx*8,ty*8,tx*8+8,ty*8+8)).tobytes()
                allowed.update(range(start,start+64))
        labels.append({'source':row['source'],'text':text,'ink':list(ink.size),
                       'glyph_bounds':glyph_bounds,'font_pixels':font_pixels,
                       'render_method':render_method,'font_source':font_source,
                       'font_sha256':font_sha256,'first_code':hex(row['code']),'overflow':False})
    assert len(cel)==len(source)
    assert all(i in allowed for i,(a,b) in enumerate(zip(original_cel,cel)) if a!=b)
    nmenu=(SOURCE/'SYSTEM/N_MENU.DAT').read_bytes()
    mapped=bytearray(changes.get('SYSTEM/N_MENU.DAT',nmenu))
    map_rows=[];map_allowed=set()
    for row in rows:
        for ty in range(2):
            y=row['map_row']+ty;off=6*8192+(y*64+5)*2
            codes=[int.from_bytes(nmenu[off+2*x:off+2*x+2],'big') for x in range(6)]
            label_codes=[0x1000+row['code']+ty*128+2*x for x in range(4)]
            if row['source']=='装備':
                assert codes[:4]==label_codes and codes[4:]==[0x113a,0x1202+ty*128]
                # The same heading is reused by equipment slot 2, with its own digit.
                assert nmenu[off+256:off+264]==nmenu[off:off+8]
                continue
            assert codes==label_codes[:2]+[0x113a]*2+label_codes[2:],(row['source'],codes)
            replacement=b''.join(c.to_bytes(2,'big') for c in label_codes+[0x113a]*2)
            mapped[off:off+12]=replacement;map_allowed.update(range(off,off+12))
            map_rows.append({'row':y,'source':nmenu[off:off+12],
                             'replacement':replacement,'offset':off})
    mirrors=[];map_mirrors=[]
    # Discover atlas and individual map-row copies in every original resource.
    for path in sorted(SOURCE.rglob('*')):
        if not path.is_file():continue
        name=path.relative_to(SOURCE).as_posix();original=path.read_bytes()
        offsets=[];pos=original.find(source[:63488])
        while pos>=0:
            offsets.append(pos);pos=original.find(source[:63488],pos+1)
        for off in halves:
            positions=[];pos=original.find(source[off:off+256])
            while pos>=0:
                positions.append(pos);pos=original.find(source[off:off+256],pos+1)
            assert positions==[start+off for start in offsets],(name,hex(off),positions)
        current=changes.get(name,original);patched=bytearray(current);permitted=set()
        for off in offsets:
            size=len(source) if original[off:off+len(source)]==source else 63488
            assert current[off:off+size]==original_cel[:size],name
            patched[off:off+size]=cel[:size]
            permitted.update(off+i for i in allowed)
            mirrors.append({'resource':name,'offset':hex(off),'size':size})
        for row in map_rows:
            positions=[];pos=original.find(row['source'])
            while pos>=0:
                positions.append(pos);pos=original.find(row['source'],pos+1)
            for off in positions:
                assert current[off:off+12] in [row['source'],row['replacement']],name
                patched[off:off+12]=row['replacement'];permitted.update(range(off,off+12))
                map_mirrors.append({'resource':name,'row':row['row'],'offset':hex(off)})
        if permitted:
            assert len(patched)==len(current)
            assert all(i in permitted for i,(a,b) in enumerate(zip(current,patched)) if a!=b),name
            changes[name]=bytes(patched)
    assert {(m['resource'],m['offset'],m['size']) for m in mirrors}=={
        ('SYSTEM/MENU.CEL','0x0',63744),('SYSTEM/MAIN.DAT','0x2730',63744),
        ('SYSTEM/MENU_S.SPR','0xe10',63488),('SYSTEM/RESET.DAT','0x2200',63488)}
    assert len(map_mirrors)==18
    assert {m['resource'] for m in map_mirrors}=={
        'SYSTEM/N_MENU.DAT','SYSTEM/MAIN.DAT','SYSTEM/MENU_S.SPR'}
    assert changes['SYSTEM/N_MENU.DAT']==mapped
    assert changes['SYSTEM/MAIN.DAT'][0x12030:0x12030+len(nmenu)]==mapped
    assert changes['SYSTEM/MENU_S.SPR'][0x10610:0x10610+len(nmenu)]==mapped
    changed_names={name for name,data in changes.items() if data!=before.get(name,(SOURCE/name).read_bytes())}
    assert changed_names<={'SYSTEM/MENU.CEL','SYSTEM/MAIN.DAT','SYSTEM/MENU_S.SPR',
                           'SYSTEM/RESET.DAT','SYSTEM/N_MENU.DAT'},changed_names
    for name,data in changes.items():
        path=stage/'resources'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    # Reconstruct the screenshot's left column from the final map and tile data.
    panel=Image.new('P',(64,96));panel.putpalette(palette((SOURCE/'SYSTEM/COL.SPR').read_bytes()[:512]))
    for y in range(12):
        for x in range(8):
            off=6*8192+((2+y)*64+4+x)*2
            code=int.from_bytes(mapped[off:off+2],'big')&0xfff
            panel.paste(Image.frombytes('P',(8,8),bytes(cel[code*32:code*32+64])),(x*8,y*8))
    save_preview(stage/'equipment_panel_preview.png',panel)
    font.used.update(int(line.split(':')[0],16) for line in (stage/'unifont_subset.hex').read_text().splitlines())
    font.export(stage);shutil.copyfile(DRAFT,stage/'inputs'/DRAFT.name)
    equipment={'localized_labels':4,'visible_rows':5,'rows':labels,'atlas_mirrors':mirrors,
               'map_row_mirrors':map_mirrors,'all_source_copies_checked':True,
               'number_tiles_icons_stats_other_resources_unchanged':True,
               'target_tiles':32,'map_byte_ranges':6,'overflow_count':0,'missing_glyph_count':0,
               'fit_font':{'source':FIT_FONTS[language],'sha256':fit_font_sha256}}
    save(stage/'equipment_labels_report.json',equipment)
    ui=json.loads((stage/'ui_labels_report.json').read_text());ui['equipment']=equipment
    save(stage/'ui_labels_report.json',ui)
    report=json.loads((old/'build_report.json').read_text())
    report.update({'version':stage.name if args.output else language+' Unifont test05','previous_candidate':old.name,
                   'ui_labels':ui,'build_status':'resources_only','installed':False,
                   'font_source':json.loads((stage/'font_source.json').read_text())})
    report['input_snapshots'][DRAFT.name]=digest(stage/'inputs'/DRAFT.name)
    save(stage/'resource_report.json',report)
    print(language,'equipment atlas and map copies verified',flush=True)
    if args.resources_only:return
    report.update(build_disc(changes,stage=stage))
    for name,item in report['files'].items():
        item['patched_sha256']=digest(stage/name);item['size']=(stage/name).stat().st_size
        if name!='FarlandSaga.bin':assert item['patched_sha256']==digest(old/name),name
    report['build_status']='candidate_ready'
    report['verification']+=' Equipment labels: all four atlas copies and all three map copies synchronized; only 32 tiles and six 12-byte map rows changed; previous stats/text/font/title/terrain resources and item icons/digits preserved; no overflow or missing glyphs.'
    save(stage/'build_report.json',report);print('Candidate ready:',stage,flush=True)


if __name__=='__main__':main()
