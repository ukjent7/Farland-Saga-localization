"""Localize native terrain sprites and shared status tiles, without game execution.

SUBMAP*: palette at 8, terrain bitmap 72x120 at 0x2488; map and other
sprites remain byte-identical. MENU.CEL is an 8x8 tile atlas, 32 tiles per
row. Seven 32x16 status labels use atlas rows y=128 and y=144. Discover and
synchronize standalone, MENU_S.SPR, MAIN.DAT and RESET.DAT copies.
English uses measured short UI labels.
"""
import argparse
import json
import shutil
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from fontTools.ttLib import TTFont
import build_alpha as base
from path_compat import REPO
from unifont_bitmap import Unifont
from candidate import clone_support_files, stage_path
from workspace_assets import save_preview

DRAFT = REPO/'translation/ui_labels.json'
VERSIONS = {'zh-CN': ('zh-CN-unifont01', 'zh-CN-unifont02'),
            'ko-KR': ('ko-KR-unifont-test02', 'ko-KR-unifont-test04'),
            'en-US': ('en-US-unifont-test02', 'en-US-unifont-test04')}
LEGEND_START, LEGEND_END = 0x2488, 0x4648


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def palette(raw):
    return [((int.from_bytes(raw[o:o+2], 'big') >> s)&31)*8
            for o in range(0, 512, 2) for s in (0, 5, 10)]


def bitmap(font, text):
    cells = []
    for ch in text:
        raw = font.glyph(ch)
        width = 8 if len(raw) == 16 else 16
        tile = Image.new('L', (width, 16))
        for y in range(16):
            value = int.from_bytes(raw[y*width//8:(y+1)*width//8], 'big')
            for x in range(width):
                if value & (1 << (width-1-x)):
                    tile.putpixel((x, y), 255)
        cells.append(tile)
    im = Image.new('L', (sum(c.width for c in cells), 16))
    x = 0
    for cell in cells:
        im.paste(cell, (x, 0)); x += cell.width
    assert im.getbbox(), text
    return im


def fitted_text(text, frame, font_path, minimum):
    for size in range(14, minimum-1, -1):
        font = ImageFont.truetype(str(font_path), size)
        left, top, right, bottom = font.getbbox(text)
        ink = Image.new('L', (right-left, bottom-top))
        ImageDraw.Draw(ink).text((-left, -top), text, font=font, fill=255)
        ink = ink.crop(ink.getbbox())
        if ink.width <= frame[0] and ink.height <= frame[1]:
            return ink, size
    raise ValueError(f'UI overflow: {text!r}, frame={frame}')


def terrain(language, rows, font, stage):
    paths = sorted(base.SOURCE.glob('SYSTEM/SUBMAP*.DAT'))
    originals = [p.read_bytes() for p in paths]
    assert len(paths) == 30 and all(len(b) == 18248 for b in originals)
    first = originals[0]
    assert all(b[8:520] == first[8:520] and b[LEGEND_START:LEGEND_END] ==
               first[LEGEND_START:LEGEND_END] for b in originals)
    im = Image.frombytes('P', (72, 120), first[LEGEND_START:LEGEND_END])
    im.putpalette(palette(first[8:520]))
    original_pixels = im.tobytes()
    font_path = base.local_path({'zh-CN':'C:/Windows/Fonts/simhei.ttf',
                                'ko-KR':'C:/Windows/Fonts/malgunbd.ttf',
                                'en-US':'C:/Windows/Fonts/arial.ttf'}[language])
    cmap = TTFont(str(font_path)).getBestCmap()
    assert all(ord(ch) in cmap for row in rows for ch in row[language])
    # Insets protect raised borders. Numeric columns start at x=44.
    boxes = [(4, 6, 39, 18), (44, 6, 69, 18)] + [
        (15, 22+16*i, 43, 38+16*i) for i in range(6)]
    reports, allowed = [], set()
    for row, (x0, y0, x1, y1) in zip(rows, boxes):
        text = row[language]; width, height = x1-x0, y1-y0
        header = len(reports) < 2
        background, foreground = (17, 20) if header else (32, 35)
        # Render at the actual small UI size; shrinking Unifont's 16px grid
        # drops entire strokes. Status tiles below retain its native 16px grid.
        # The reviewed Korean label 황무지 measures 28x11 at 9 px in Malgun;
        # the protected terrain label cell is 28x16. Let it fit at that
        # measured size without changing the cell or its raised border.
        minimum = 10 if language == 'en-US' else 9 if language == 'ko-KR' else 12
        ink, size = fitted_text(text, (width, height), font_path, minimum)
        assert ink.width <= width and ink.height <= height
        x = x0+(width-ink.width)//2 if header else x0
        y = y0+(height-ink.height)//2
        for yy in range(y0, y1):
            for xx in range(x0, x1):
                im.putpixel((xx, yy), background); allowed.add(yy*72+xx)
        for yy in range(ink.height):
            for xx in range(ink.width):
                opacity = ink.getpixel((xx, yy))
                # Quantize antialiasing using the existing gray/blue ramp.
                index = (17+round(opacity*3/255)) if header else (32+round(opacity*3/255))
                im.putpixel((x+xx, y+yy), index)
        reports.append({'source': row['source'], 'text': text, 'box': [x0,y0,x1,y1],
                        'font_pixels': size, 'ink': list(ink.size), 'overflow': False})
    payload = im.tobytes()
    assert all(i in allowed for i,(a,b) in enumerate(zip(original_pixels,payload)) if a != b)
    # Explicitly check every percentage and every colored swatch rectangle.
    for y in range(22, 118):
        for x in [*range(5,13), *range(44,71)]:
            assert payload[y*72+x] == original_pixels[y*72+x]
    changes = {}
    for path, original in zip(paths, originals):
        patched = original[:LEGEND_START]+payload+original[LEGEND_END:]
        assert len(patched) == len(original)
        assert patched[:LEGEND_START] == original[:LEGEND_START]
        assert patched[LEGEND_END:] == original[LEGEND_END:]
        assert Image.frombytes('P', (72,120), patched[LEGEND_START:LEGEND_END]).tobytes() == payload
        changes[path.relative_to(base.SOURCE).as_posix()] = patched
    save_preview(stage/'terrain_preview.png',im)
    return changes, {'files':len(paths), 'rows':reports, 'font':font_path.name,
                     'font_sha256':base.digest(font_path),'percentages_and_swatches_unchanged':True,
                     'palettes_maps_other_sprites_unchanged':True, 'bitmap_round_trip':'passed'}


def read_atlas(cel):
    atlas = Image.new('P', (256, 256))
    atlas.putpalette(palette((base.SOURCE/'SYSTEM/COL.SPR').read_bytes()[:512]))
    for tile in range(len(cel)//64):
        atlas.paste(Image.frombytes('P',(8,8),cel[tile*64:(tile+1)*64]),
                    ((tile%32)*8,(tile//32)*8))
    return atlas


def stats(language, rows, font, changes, stage):
    source = (base.SOURCE/'SYSTEM/MENU.CEL').read_bytes()
    def strip(cel):
        atlas = read_atlas(cel)
        result = Image.new('P',(256,16));result.putpalette(atlas.getpalette())
        result.paste(atlas.crop((0,128,256,136)),(0,0))
        result.paste(atlas.crop((0,144,256,152)),(0,8))
        return result
    if language == 'zh-CN':
        save_preview(stage/'stats_preview.png',strip(source))
        return {'localized_labels':0, 'chinese_labels_unchanged':True}
    original_cel=changes.get('SYSTEM/MENU.CEL',source)
    cel = bytearray(original_cel); allowed = set(); reports = []
    for row in rows:
        ink = bitmap(font,row[language]); assert ink.width <= 32 and ink.height == 16
        region = Image.new('P',(32,16))
        for y in range(16):
            for x in range(32):
                region.putpixel((x,y),43 if y%2 and not x%2 else 50)
        for y in range(16):
            for x in range(ink.width):
                if ink.getpixel((x,y)):
                    region.putpixel((x,y),11)
        for ty in range(2):
            for tx in range(4):
                tile = (16+2*ty)*32 + row['atlas_x']//8 + tx
                off = tile*64; data = region.crop((tx*8,ty*8,tx*8+8,ty*8+8)).tobytes()
                cel[off:off+64]=data; allowed.update(range(off,off+64))
        reports.append({'source':row['source'],'text':row[language], 'ink':list(ink.size),
                        'atlas_x':row['atlas_x'], 'atlas_tile_rows':[16,18], 'overflow':False})
    assert len(cel)==len(source)
    assert all(i in allowed for i,(a,b) in enumerate(zip(original_cel,cel)) if a != b)
    # Tilemap codes continue addressing the same tiles in every status window.
    nmenu = (base.SOURCE/'SYSTEM/N_MENU.DAT').read_bytes()
    for i,row in enumerate(rows):
        # The shared attribute label is an independently addressed atlas slot
        # (0x418), populated by the legacy full-language builder. It is not
        # one of the seven sequential N_MENU status-window rows.
        if row.get('id') == 'ui:stats:attribute':
            continue
        for ty in range(2):
            for tx in range(4):
                pos=4*8192+((3+i*2+ty)*64+19+tx)*2
                code=int.from_bytes(nmenu[pos:pos+2],'big')&0xfff
                expected=((16+2*ty)*32+row['atlas_x']//8+tx)*2
                assert code==expected,(row['source'],hex(code),hex(expected))
    # RESET.DAT carries the same atlas without its final four tiles. Scanning
    # every source file prevents a new bundle from silently retaining labels.
    mirrors = []
    label_halves = [((16+2*ty)*32+row['atlas_x']//8)*64
                    for row in rows for ty in range(2)]
    for path in sorted(base.SOURCE.rglob('*')):
        if not path.is_file():continue
        original = path.read_bytes(); name = path.relative_to(base.SOURCE).as_posix()
        offsets = []; pos = original.find(source[:63488])
        while pos >= 0:
            offsets.append(pos); pos = original.find(source[:63488],pos+1)
        # Check smaller label sequences too, detecting any partial atlas copy.
        for label_off in label_halves:
            pattern=source[label_off:label_off+256]; hits=[]
            pos=original.find(pattern)
            while pos >= 0:
                hits.append(pos);pos=original.find(pattern,pos+1)
            assert hits == [off+label_off for off in offsets], (name,label_off,hits,offsets)
        if not offsets:continue
        patched=bytearray(changes.get(name,original)); spans=[]
        for off in offsets:
            length=len(source) if original[off:off+len(source)]==source else 63488
            assert patched[off:off+length]==original_cel[:length],name
            patched[off:off+length]=cel[:length];spans.append((off,off+length))
            assert patched[off:off+length]==cel[:length]
            mirrors.append({'resource':name,'offset':hex(off),'size':length})
        before=changes.get(name,original)
        assert len(patched)==len(before)
        assert all(any(start<=i<end for start,end in spans)
                   for i,(a,b) in enumerate(zip(before,patched)) if a!=b),name
        changes[name]=bytes(patched)
    assert {(m['resource'],m['offset'],m['size']) for m in mirrors} == {
        ('SYSTEM/MENU.CEL','0x0',63744),('SYSTEM/MENU_S.SPR','0xe10',63488),
        ('SYSTEM/MAIN.DAT','0x2730',63744),('SYSTEM/RESET.DAT','0x2200',63488)}
    save_preview(stage/'stats_preview.png',strip(cel))
    # Produce the same panel as screenshot 2, using the original tilemap.
    panel = Image.new('P',(40,112));panel.putpalette(palette((base.SOURCE/'SYSTEM/COL.SPR').read_bytes()[:512]))
    for y in range(14):
        for x in range(5):
            pos=4*8192+((3+y)*64+18+x)*2
            off=(int.from_bytes(nmenu[pos:pos+2],'big')&0xfff)*32
            panel.paste(Image.frombytes('P',(8,8),bytes(cel[off:off+64])),(x*8,y*8))
    save_preview(stage/'stats_panel_preview.png',panel)
    return {'localized_labels':len(reports),'rows':reports,'standalone_bundle_main_mirrors':'passed',
            'mirrors':mirrors,'all_source_label_copies_checked':True,
            'tilemap_unchanged':True,'unrequested_atlas_pixels_unchanged':True}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--language',required=True,choices=list(VERSIONS))
    parser.add_argument('--base')
    parser.add_argument('--output')
    parser.add_argument('--resources-only',action='store_true');args=parser.parse_args()
    language=args.language;old_name,new_name=VERSIONS[language]
    old=stage_path(args.base or old_name);stage=stage_path(args.output or new_name)
    stage.mkdir(exist_ok=True);base.STAGE=stage
    report=json.loads((old/'build_report.json').read_text(encoding='utf-8'))
    clone_support_files(old,stage)
    changes={p.relative_to(old/'resources').as_posix():p.read_bytes()
             for p in (old/'resources').rglob('*') if p.is_file()}
    before=dict(changes);draft=json.loads(DRAFT.read_text(encoding='utf-8'));font=Unifont()
    terrain_changes,terrain_report=terrain(language,draft['terrain'],font,stage)
    changes.update(terrain_changes)
    stat_report=stats(language,draft['stats'],font,changes,stage)
    # All previous text, fonts and title sprites must remain intact; only the
    # menu atlas embedded in MAIN.DAT may differ from its previous release.
    for name,data in before.items():
        if name not in terrain_changes and name not in {m['resource'] for m in stat_report.get('mirrors',[])}:
            assert changes[name]==data,name
    for name,data in changes.items():
        path=stage/'resources'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    subset=stage/'unifont_subset.hex'
    font.used.update(int(line.split(':')[0],16) for line in subset.read_text().splitlines())
    font.export(stage)
    shutil.copyfile(DRAFT,stage/'inputs'/DRAFT.name)
    ui_report={'language':language,'terrain':terrain_report,'stats':stat_report,
               'translation_status':draft['status'],'overflow_count':0,'missing_glyph_count':0,
               'previous_text_font_titles_unchanged':True,'game_launched':False}
    previous_ui=report.get('ui_labels',{})
    if 'equipment' in previous_ui:ui_report['equipment']=previous_ui['equipment']
    extras=[row for row in previous_ui.get('stats',{}).get('rows',[])
            if row['source'] not in {row['source'] for row in stat_report.get('rows',[])}]
    if extras and language!='zh-CN':
        stat_report['rows'].extend(extras);stat_report['localized_labels']=len(stat_report['rows'])
    save(stage/'ui_labels_report.json',ui_report)
    report.update({'version':stage.name if args.output else language+' Unifont UI02' if language=='zh-CN' else language+' Unifont test04',
                   'previous_candidate':old.name,'ui_labels':ui_report,'installed':False,
                   'build_status':'resources_only','font_source':json.loads((stage/'font_source.json').read_text())})
    report['input_snapshots'][DRAFT.name]=base.digest(stage/'inputs'/DRAFT.name)
    save(stage/'resource_report.json',report)
    print(language,'terrain and status resources verified',flush=True)
    if args.resources_only:return
    report.update(base.build_disc(changes))
    for name,item in report['files'].items():
        item['patched_sha256']=base.digest(stage/name);item['size']=(stage/name).stat().st_size
    assert report['files']['FarlandSaga.exe']['patched_sha256']==base.digest(old/'FarlandSaga.exe')
    report['build_status']='candidate_ready'
    report['verification']+=' UI update: all 30 terrain images synchronized; percentages, swatches, palettes and maps preserved; status atlas mirrors synchronized; Chinese status labels unchanged; previous texts/fonts/titles preserved; no overflow or missing glyphs.'
    save(stage/'build_report.json',report)
    print('Candidate ready:',stage,flush=True)


if __name__=='__main__':main()
