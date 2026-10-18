"""Patch declared text regions in SSF Texture/Parts PNG containers."""
from io import BytesIO
import json
import os
from pathlib import Path
import struct
from functools import lru_cache

from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont

from fls.core.path_compat import WORK, local_path
from fls.core.env import EXE
from fls.core.file_hash import sha256 as digest
from path_compat import local_path
from steam_container import parameters, transform
from workspace_assets import save_preview


def decode_container(path, logical_name):
    encoded = Path(path).read_bytes()
    params = parameters(path, EXE, logical_name)
    return encoded[:2048], transform(encoded[2048:], params), params


def parse_atlas(data):
    if data[:20] != b'Texture'.ljust(20):
        raise ValueError('Expected a Texture section')
    end = struct.unpack_from('<I', data, 20)[0]
    png_size = struct.unpack_from('<I', data, 32)[0]
    if end != 40 + png_size:
        raise ValueError('Texture section length mismatch')
    image = Image.open(BytesIO(data[40:end])).convert('RGBA')
    if image.size != struct.unpack_from('<HH', data, 36):
        raise ValueError('Texture dimensions mismatch')
    parts_start = (end + 3) & ~3
    if data[end:parts_start] != bytes(parts_start-end) or data[parts_start:parts_start+20] != b'Parts'.ljust(20):
        raise ValueError('Expected aligned Parts metadata')
    count = struct.unpack_from('<I', data, parts_start+24)[0]
    if struct.unpack_from('<I', data, parts_start+20)[0] != 28 + count*32:
        raise ValueError('Parts section length mismatch')
    boxes = []
    for index in range(count):
        values = struct.unpack_from('<8f', data, parts_start+28+index*32)
        box = tuple(round(v*scale) for v, scale in zip(values[4:], image.size*2))
        left, top, right, bottom = box
        if not (0 <= left < right <= image.width and 0 <= top < bottom <= image.height):
            raise ValueError(f'Part outside atlas: {index}')
        boxes.append(box)
    return image, boxes, data[:40], data[parts_start:]


@lru_cache(maxsize=128)
def _font(path, size, index=None):
    return ImageFont.truetype(path,size) if index is None else ImageFont.truetype(path,size,index=index)


def _text_bbox(image, text, font, align, stroke):
    measure=ImageDraw.Draw(image)
    return (measure.multiline_textbbox((0,0),text,font=font,spacing=4,align=align,stroke_width=stroke)
            if '\n' in text else font.getbbox(text,stroke_width=stroke))


def _mixed_glyph_layer(text, font, fallback_font, fallback_chars, bbox, fill, stroke):
    if '\n' in text:
        raise ValueError('Atlas fallback glyphs cannot be mixed into multiline text')
    layer=Image.new('RGBA',(bbox[2]-bbox[0],bbox[3]-bbox[1]),(0,0,0,0))
    draw=ImageDraw.Draw(layer);baseline_x=-bbox[0];baseline_y=-bbox[1]
    run_start=0
    for index,char in enumerate(text):
        if ord(char) not in fallback_chars:
            continue
        if run_start<index:
            run=text[run_start:index]
            draw.text((baseline_x+font.getlength(text[:run_start]),baseline_y),run,
                      font=font,fill=fill,stroke_width=stroke,stroke_fill=(0,0,0,255))
        advance_before=font.getlength(text[:index])
        advance_after=font.getlength(text[:index+1])
        cell_width=advance_after-advance_before
        glyph_box=fallback_font.getbbox(char,stroke_width=stroke)
        glyph_width=glyph_box[2]-glyph_box[0]
        glyph_height=glyph_box[3]-glyph_box[1]
        glyph_x=baseline_x+advance_before+(cell_width-glyph_width)/2-glyph_box[0]
        glyph_y=(bbox[3]-bbox[1]-glyph_height)/2-glyph_box[1]
        draw.text((glyph_x,glyph_y),char,font=fallback_font,fill=fill,
                  stroke_width=stroke,stroke_fill=(0,0,0,255))
        run_start=index+1
    if run_start<len(text):
        run=text[run_start:]
        draw.text((baseline_x+font.getlength(text[:run_start]),baseline_y),run,
                  font=font,fill=fill,stroke_width=stroke,stroke_fill=(0,0,0,255))
    return layer


def draw_text(image, box, text, font_path, size, align='left', stroke=0,
              fill=(255,255,255,255), max_lines=1, rotation=0,
              fallback_font_path=None, fallback_chars=(), minimum_size=9, context='atlas text',
              locale=None, identifier=None, part=None):
    left, top, right, bottom = box
    if (not isinstance(text, str) or not text.strip()
            or any(ord(c) < 32 and c != '\n' for c in text)
            or any(not line.strip() for line in text.split('\n'))
            or len(text.split('\n')) > max_lines):
        raise ValueError('Atlas text must be nonempty within the declared line count')
    if align not in ('left','center','right') or rotation not in (0,90,180,270):
        raise ValueError('Unsupported atlas text alignment or rotation')
    multiline = '\n' in text
    fallback_set=set(fallback_chars)
    fallback_used=fallback_font_path and any(ord(char) in fallback_set for char in text)
    minimum_size=max(1,min(int(minimum_size),int(size)))
    preferred_size=int(size);chosen=None
    for current_size in range(preferred_size,minimum_size-1,-1):
        font=_font(str(font_path),current_size)
        bbox=_text_bbox(image,text,font,align,stroke)
        width,height=bbox[2]-bbox[0],bbox[3]-bbox[1]
        if rotation in (90,270):width,height=height,width
        if width<=right-left-8 and height<=bottom-top-4:
            chosen=(current_size,font,bbox,width,height);break
    if chosen is None:
        identity = (f'locale={locale or "unknown"} id={identifier or "unknown"} '
                    f'part={part if part is not None else "unknown"}')
        raise ValueError(f'Atlas text overflow: {identity} context={context!r} text={text!r}, '
                         f'frame={right-left-8}x{bottom-top-4}, minimum_font_size={minimum_size}, '
                         f'last_bounds={width}x{height}')
    size,font,bbox,width,height=chosen
    mixed_layer=None
    if fallback_used:
        fallback_font=_font(str(fallback_font_path),size,0)
        mixed_layer=_mixed_glyph_layer(text,font,fallback_font,fallback_set,bbox,fill,stroke)
    x = left+4 if align == 'left' else right-width-4 if align == 'right' else left+(right-left-width)//2
    y = top+(bottom-top-height)//2
    if rotation:
        ink = mixed_layer if mixed_layer is not None else Image.new('RGBA',(bbox[2]-bbox[0],bbox[3]-bbox[1]))
        if mixed_layer is None:
            draw = ImageDraw.Draw(ink)
            draw.multiline_text((-bbox[0],-bbox[1]),text,font=font,fill=fill,spacing=4,
                                align=align,stroke_width=stroke,stroke_fill=(0,0,0,255))
        image.alpha_composite(ink.rotate(-rotation,expand=True),(x,y))
    elif mixed_layer is not None:
        image.alpha_composite(mixed_layer,(x,y))
    elif multiline:
        measure = ImageDraw.Draw(image)
        measure.multiline_text((x-bbox[0],y-bbox[1]),text,font=font,fill=fill,
                               spacing=4,align=align,stroke_width=stroke,stroke_fill=(0,0,0,255))
    else:
        measure = ImageDraw.Draw(image)
        measure.text((x-bbox[0], y-bbox[1]), text, font=font, fill=fill,
                     stroke_width=stroke, stroke_fill=(0,0,0,255))
    return {'text':text,'lines':text.split('\n'),'preferred_font_size':preferred_size,
            'font_size':size,'actual_font_size':size,'minimum_font_size':minimum_size,
            'line_count':len(text.split('\n')),'reduced_font':size<preferred_size,
            'ink_size':[width,height],'bounds':[x,y,x+width,y+height],'overflow':False,
            'fallback_codepoints':[f'U+{ord(char):04X}' for char in sorted(set(text))
                                   if ord(char) in fallback_set]}


def apply_atlases(candidate, language, configs, create_preview=True):
    reports = []
    for config in configs:
        name = config['file']
        source = (WORK/config['source']).resolve()
        if not source.is_relative_to(WORK.resolve()) or digest(source) != config['original_sha256']:
            raise ValueError('Frozen atlas source identity mismatch')
        font_root = local_path('C:/Windows/Fonts')
        font_path = font_root/config['fonts'][language]
        rows = config['labels']
        panel_text = config['panel_text'][language]
        required = {ord(c) for row in rows for c in row['text'][language] if c != '\n'}
        required.update(ord(c) for text in panel_text.values() for c in text)
        locations={}
        for row in rows:
            for char in row['text'][language]:
                if char!='\n':locations.setdefault(ord(char),[]).append(f"part {row['part']} {row.get('source','')!r}")
        for key,text in panel_text.items():
            for char in text:
                locations.setdefault(ord(char),[]).append(f'panel {key}')
        font = TTFont(str(font_path))
        try:
            missing=required-set(font.getBestCmap())
        finally:
            font.close()
        fallback_name=config.get('fallback_fonts',{}).get(language)
        fallback_path=font_root/fallback_name if fallback_name else None
        fallback_sha256=None
        if missing:
            if fallback_path is None or not fallback_path.is_file():
                points='; '.join(f'U+{point:04X} at {", ".join(locations.get(point, []))}'
                                 for point in sorted(missing))
                raise ValueError(f'{language} atlas font {font_path.name} lacks required glyphs: {points}')
            fallback_font=TTFont(str(fallback_path),fontNumber=0)
            try:
                unsupported=missing-set(fallback_font.getBestCmap())
            finally:
                fallback_font.close()
            if unsupported:
                points='; '.join(f'U+{point:04X} at {", ".join(locations.get(point, []))}'
                                 for point in sorted(unsupported))
                raise ValueError(f'{language} fallback font {fallback_name} lacks required glyphs: {points}')
            fallback_sha256=digest(fallback_path)
        encrypted_header, original_data, params = decode_container(source, name)
        original, boxes, section_header, suffix = parse_atlas(original_data)
        if list(original.size) != config['image_size'] or len(boxes) != config['part_count']:
            raise ValueError('Unexpected atlas dimensions or part count')
        patched = original.copy()
        allowed = Image.new('L', original.size)
        mask_draw = ImageDraw.Draw(allowed)
        details = []
        if len({row['part'] for row in rows}) != len(rows):
            raise ValueError('Duplicate atlas label part')
        for row in rows:
            box = boxes[row['part']]
            if list(box) != row['box']:
                raise ValueError(f'Atlas part moved: {row["part"]}')
            region = row.get('region',[0,0,box[2]-box[0],box[3]-box[1]])
            if not (0 <= region[0] < region[2] <= box[2]-box[0]
                    and 0 <= region[1] < region[3] <= box[3]-box[1]):
                raise ValueError('Atlas label region outside part')
            draw_box = (box[0]+region[0],box[1]+region[1],box[0]+region[2],box[1]+region[3])
            patched.paste(tuple(row.get('background',[255,255,255,0])), draw_box)
            mask_draw.rectangle((draw_box[0],draw_box[1],draw_box[2]-1,draw_box[3]-1), fill=255)
            minimum=row.get('minimum_font_size',9)
            if isinstance(minimum,dict):minimum=minimum.get(language,9)
            result = draw_text(patched, draw_box, row['text'][language], font_path,
                               row.get('font_size',{}).get(language,32),row.get('align','left'),
                               row.get('stroke',0),tuple(row.get('fill',[255,255,255,255])),
                               row.get('max_lines',1),row.get('rotation',0),fallback_path,missing,
                               minimum,f'{language} part {row["part"]} {row.get("translation_id","unbound")}',
                               locale=language,identifier=row.get('translation_id',f'part-{row["part"]}'),
                               part=row['part'])
            details.append({'part':row['part'],'translation_id':row.get('translation_id'),
                            'box':box,'draw_region':draw_box,**result})
        panels = []
        for part in config['panel_parts']:
            box = boxes[part]
            if box[2]-box[0] != 228 or box[3]-box[1] != 225:
                raise ValueError('Unexpected option panel dimensions')
            left,top,_,_ = box
            panel = patched.crop(box)
            # Reuse a text-free row, retaining the title's horizontal alpha gradient.
            header_box = (16,6,212,52)
            strip = original.crop((left+16,top+51,left+212,top+52)).resize((196,46))
            panel.paste(strip, (16,6))
            body_box = (16,61,212,207)
            panel.paste((0,0,0,255), body_box)
            title = draw_text(panel, header_box, panel_text['title'], font_path,32,'center',2,
                              fallback_font_path=fallback_path,fallback_chars=missing,
                              minimum_size=config.get('panel_minimum_font_size',9),
                              context=f'{language} panel title',locale=language,
                              identifier=config.get('panel_translation_ids',{}).get('title','panel-title'),
                              part=part)
            button = draw_text(panel, body_box, panel_text['button'], font_path,28,'center',
                               fallback_font_path=fallback_path,fallback_chars=missing,
                               minimum_size=config.get('panel_minimum_font_size',9),
                               context=f'{language} panel button',locale=language,
                               identifier=config.get('panel_translation_ids',{}).get('button','panel-button'),
                               part=part)
            patched.paste(panel,(left,top))
            for region in [header_box,body_box]:
                mask_draw.rectangle((left+region[0],top+region[1],left+region[2]-1,top+region[3]-1),fill=255)
            panels.append({'part':part,'box':box,'title':title,'button':button})
        before_bytes,after_bytes,mask = original.tobytes(),patched.tobytes(),allowed.tobytes()
        for pos, permitted in enumerate(mask):
            if not permitted and before_bytes[pos*4:pos*4+4] != after_bytes[pos*4:pos*4+4]:
                raise AssertionError(f'Pixel changed outside declared regions: {pos}')
        png = BytesIO();patched.save(png,format='PNG')
        header = bytearray(section_header)
        end = 40 + len(png.getvalue())
        struct.pack_into('<I',header,20,end);struct.pack_into('<I',header,32,len(png.getvalue()))
        payload = bytes(header)+png.getvalue()+bytes((-end)%4)+suffix
        encoded = encrypted_header + transform(payload,params,encode=True)
        candidate.write_frontend_file(name,encoded,config['original_sha256'])
        check_header,check_data,_ = decode_container(candidate.stage/name,name)
        checked,checked_boxes,checked_section,checked_suffix = parse_atlas(check_data)
        if (check_header != encrypted_header or check_data != payload or checked.tobytes() != after_bytes
                or checked_boxes != boxes or checked_suffix != suffix):
            raise AssertionError('Encoded atlas readback mismatch')
        if checked_section[:20] != section_header[:20] or checked_section[24:32] != section_header[24:32] or checked_section[36:] != section_header[36:]:
            raise AssertionError('Texture format changed')
        source_snapshot = candidate.stage/'inputs'/Path(source).name
        source_snapshot.write_bytes(source.read_bytes())
        report = {'file':name,'language':language,'labels':details,'panels':panels,
                  'original_sha256':config['original_sha256'],'patched_sha256':digest(candidate.stage/name),
                  'font_source':font_path.name,'font_sha256':digest(font_path),'font_cmap_coverage':'complete',
                  'part_count':len(boxes),'part_metadata_unchanged':True,'outside_pixels_unchanged':True,
                  'container_header_unchanged':True,'encrypted_readback':'passed','game_launched':False}
        if missing:
            report['fallback_font']={'source':fallback_name,'sha256':fallback_sha256,
                                     'codepoints':[f'U+{point:04X}' for point in sorted(missing)],
                                     'placement':'centered within primary-font advance cell'}
        if create_preview:
            if config.get('preview_sections'):
                previews=[]
                for section in config['preview_sections']:
                    ids=section['parts']
                    height=50+sum(boxes[p][3]-boxes[p][1]+12 for p in ids)
                    width=max(boxes[p][2]-boxes[p][0] for p in ids)+32
                    preview=Image.new('RGBA',(width,height),(18,24,33,255))
                    ImageDraw.Draw(preview).text((16,12),language+' / '+section['id'],fill='white',font_size=20)
                    y=46
                    for part in ids:
                        tile=patched.crop(boxes[part]);preview.alpha_composite(tile,(16,y))
                        y+=tile.height+12
                    path=save_preview(candidate.stage/('frontend_'+section['id']+'.png'),preview)
                    previews.append({'section':section['id'],'path':str(path.relative_to(WORK))})
                report['previews']=previews
                report['preview']=previews[0]['path']
            else:
                preview = Image.new('RGBA',(400,580),(18,24,33,255))
                ImageDraw.Draw(preview).text((16,12),language,fill='white',font_size=20)
                for i,row in enumerate(rows):
                    tile=patched.crop(boxes[row['part']]);preview.alpha_composite(tile,(16,45+i*36))
                preview.alpha_composite(patched.crop(boxes[config['panel_parts'][0]]),(16,345))
                report['preview'] = str(save_preview(candidate.stage/'frontend_menu.png',preview).relative_to(WORK))
        reports.append(report)
    return reports
