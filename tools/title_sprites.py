"""Decode the three indexed images in each TITLE*.SPR without changing assets."""
from pathlib import Path
from PIL import Image, ImageDraw
import argparse
from path_compat import WORK
from workspace_assets import save_preview



def decode(data):
    assert len(data) in (9344, 10304), 'Unknown title sprite layout'
    palette = []
    for off in range(0, 512, 2):
        color = int.from_bytes(data[off:off+2], 'big')
        palette.append(tuple(((color >> shift) & 31)*255//31 for shift in (0, 5, 10)))
    images = {}
    offset = 512
    layout = [('stage', 80), ('number', 32), ('title', 256)] if len(data) == 9344 else [
        ('final', 72), ('stage', 80), ('title', 256)]
    for key, width in layout:
        image = Image.frombytes('P', (width, 24), data[offset:offset+width*24])
        image.putpalette([v for rgb in palette for v in rgb])
        image.info['transparency'] = 0
        images[key] = {'image': image, 'offset': offset, 'size': width*24}
        offset += width*24
    assert offset == len(data)
    return images


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--individual',action='store_true',help='Also export each decoded region')
    args=parser.parse_args()
    source = WORK/'extracted/steam/TITLE'
    target = WORK/'analysis/stage_title'
    target.mkdir(parents=True, exist_ok=True)
    paths = sorted(source.glob('TITLE*.SPR'))
    sheet = Image.new('RGB', (660, ((len(paths)+1)//2)*34), '#6d7060')
    draw = ImageDraw.Draw(sheet)
    for i, path in enumerate(paths):
        images = decode(path.read_bytes())
        if args.individual:
            for name, entry in images.items():
                save_preview(target/f'{path.stem}_{name}.png',entry['image'])
        x, y = (i//25)*330, (i%25)*34
        draw.text((x+2, y+8), path.stem[-2:], fill='white')
        rgba = images['title']['image'].convert('RGBA')
        sheet.paste(rgba, (x+30, y+4), rgba)
    save_preview(target/'original_title_contact_sheet.png',sheet)
    print('Decoded title sprites:', len(paths))


if __name__ == '__main__':
    main()
