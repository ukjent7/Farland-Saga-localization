"""Integration regressions against frozen production resources; no game/install.

Run normally for resource/executable replay; add --pack for one full encrypted
disc replay. Temporary candidate outputs are removed after the tests.

Each language gets its own test method, so the shard balancer in
``fls.core.test_shards`` can spread the three replays across processes. The
shared body of a per-language group lives in one private helper; the test
methods only pass the language, source candidate and expected candidate.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from fls.storage.candidate import stage_path
from fls.text.native_patches import apply_recipe
import fls.builders.build_full_languages as build_full_languages
import fls.disc.iso_layout as iso_layout
from fls.cli.main import invoke
from fls.core.test_shards import SHARD_ENV, artifact_path, balance, current_shard
from fls.storage.workspace_assets import forget_preview_aliases
from fls.storage.version_storage import materialize
import fls.core.path_compat as path_compat

WORK=path_compat.WORK

PACK='--pack' in sys.argv
if PACK:sys.argv.remove('--pack')
if '--shard' in sys.argv:
    at=sys.argv.index('--shard')
    os.environ[SHARD_ENV]=sys.argv[at+1]
    del sys.argv[at:at+2]

class Workflows(unittest.TestCase):
    durations=[]
    @classmethod
    def setUpClass(cls):
        parent=WORK/'analysis/maintenance-tests';parent.mkdir(exist_ok=True)
        cls.temp=tempfile.TemporaryDirectory(dir=parent)
        cls.output=Path(cls.temp.name);cls.proof=[]

    def setUp(self):
        self._started=time.monotonic()

    def tearDown(self):
        Workflows.durations.append(('.'.join(self.id().split('.')[-2:]),round(time.monotonic()-self._started,3)))

    @classmethod
    def tearDownClass(cls):
        proof=artifact_path('replay_checks')
        previous=json.loads(proof.read_text()) if proof.exists() else []
        combined={json.dumps(item,sort_keys=True):item for item in previous+cls.proof}
        proof.write_text(json.dumps(list(combined.values()),ensure_ascii=False,indent=2))
        durations=artifact_path('test_durations')
        recorded=json.loads(durations.read_text()) if durations.exists() else {}
        recorded.update(dict(cls.durations))
        durations.write_text(json.dumps(recorded,ensure_ascii=False,indent=2))
        if cls.durations:
            summary=sorted(cls.durations,key=lambda item:-item[1])
            print('durations: '+', '.join(f'{name} {seconds:.1f}s' for name,seconds in summary),file=sys.stderr)
        forget_preview_aliases(cls.output)
        cls.temp.cleanup()

    def compare(self,result,expected,packed=False):
        frozen=WORK/'staging'/expected
        materialize(frozen)
        actual_files={p.relative_to(result.stage/'resources').as_posix():p for p in (result.stage/'resources').rglob('*') if p.is_file()}
        expected_files={p.relative_to(frozen/'resources').as_posix():p for p in (frozen/'resources').rglob('*') if p.is_file()}
        self.assertEqual(set(actual_files),set(expected_files))
        for name,path in actual_files.items():self.assertEqual(path.read_bytes(),expected_files[name].read_bytes(),name)
        report=json.loads((frozen/'build_report.json').read_text())
        from fls.builders.build_alpha import digest
        for name,item in report['files'].items():
            if name=='FarlandSaga.bin' and not packed:continue
            self.assertEqual(digest(result.stage/name),item['patched_sha256'],name)
        self.proof.append({'expected':expected,'native_resources_equal':len(actual_files),
                           'executable_and_unity_files_equal':True,'encrypted_disc_equal':packed})

    def _condition_tables(self, language,code):
        result=apply_recipe(code+'-unifont-full01',self.output/(code+'-conditions'),language,
                            'condition-labels',resources_only=True,create_preview=False)
        self.assertEqual(result.report['translated_texts'],2786)
        self.compare(result,code+'-unifont-full02')

    def test_condition_tables_ko_kr(self):
        self._condition_tables('ko-KR', 'ko-KR')

    def test_condition_tables_en_us(self):
        self._condition_tables('en-US', 'en-US')

    def _menu_spacing(self, code,source,expected):
        packed=PACK and code=='ko-KR'
        result=apply_recipe(source,self.output/(code+'-spacing'),code,'menu-spacing',
                            resources_only=not packed,create_preview=False)
        self.assertEqual(result.report['menu_spacing']['target_width_pixels'],48)
        if packed:self.assertFalse((result.stage/'inner.bin').exists())
        self.compare(result,expected,packed)

    def test_menu_spacing_ko_kr(self):
        self._menu_spacing('ko-KR', 'ko-KR-unifont-full02', 'ko-KR-unifont-full03')

    def test_menu_spacing_zh_cn(self):
        self._menu_spacing('zh-CN', 'zh-CN-unifont02', 'zh-CN-unifont03')

    def test_dash_glyph(self):
        result=apply_recipe('alpha06',self.output/'dash','zh-CN','dash-center',
                            resources_only=True,create_preview=False)
        self.compare(result,'alpha06-dashfix')

    def test_idempotent_condition_table(self):
        result=apply_recipe('ko-KR-unifont-full02',self.output/'idempotent','ko-KR',
                            'condition-labels',resources_only=True,create_preview=False)
        self.assertEqual(result.report['translated_texts'],2786)
        self.compare(result,'ko-KR-unifont-full02')

    def test_full_builder_preserves_later_fixes(self):
        output=self.output/'full'
        materialize('staging/ko-KR-unifont-full03')
        invoke('fls.builders.build_full_languages',['--language','ko','--base','ko-KR-unifont-full03',
                                      '--output',str(output),'--resources-only'])
        expected=WORK/'staging/ko-KR-unifont-full03/resources'
        for p in expected.rglob('*'):
            if p.is_file():
                data=bytearray(p.read_bytes());name=p.relative_to(expected).as_posix()
                original=(WORK/'extracted/steam/SYSTEM/ITEM.DAT').read_bytes()
                if name in {'SYSTEM/ITEM.DAT','SYSTEM/MAIN.DAT'}:
                    start=0 if name=='SYSTEM/ITEM.DAT' else 0x2a5e4
                    for off in range(0,len(original),36):data[start+off+18:start+off+20]=original[off+18:off+20]
                self.assertEqual(bytes(data),(output/'resources'/p.relative_to(expected)).read_bytes(),str(p))
        report=json.loads((output/'resource_report.json').read_text())
        self.assertEqual(report['core_translated_texts'],2697)
        self.assertEqual(report['condition_label_count'],9)
        self.assertEqual(report['preserved_additional_field_texts'],0)
        self.proof.append({'full_builder_latest_resources_equal':True,'fixed_labels_and_spacing_retained':True})

    def _item_prices_restored(self, code,source):
        from fls.builders import build_alpha as base
        original=(base.SOURCE/'SYSTEM/ITEM.DAT').read_bytes()
        self.assertEqual(int.from_bytes(original[12*36+18:12*36+20],'big'),300)
        install_record=(WORK/'install_report.json').read_bytes()
        packed=PACK and code=='zh-CN'
        result=apply_recipe(source,self.output/(code+'-prices'),code,'item-prices',
                            resources_only=not packed,create_preview=False)
        item=result.resources['SYSTEM/ITEM.DAT']
        for off in range(0,len(original),36):
            self.assertEqual(item[off+18:off+36],original[off+18:off+36])
            self.assertIn(b'\0',item[off:off+18])
        self.assertEqual(result.resources['SYSTEM/MAIN.DAT'][0x2a5e4:0x2a5e4+7200],item)
        for name,data in result.resources.items():
            if name not in {'SYSTEM/ITEM.DAT','SYSTEM/MAIN.DAT','SYSTEM/KANJI.FON'}:
                self.assertEqual(data,result.original_resources[name],name)
        if code in {'ko-KR','en-US'}:
            rebuild=self.output/(code+'-price-rebuild')
            invoke('fls.builders.build_full_languages',['--language','ko' if code=='ko-KR' else 'en',
                                          '--base',source,'--output',str(rebuild),'--resources-only'])
            self.assertEqual((rebuild/'resources/SYSTEM/ITEM.DAT').read_bytes(),item)
        self.proof.append({'item_prices_language':code,'all_200_records_attributes_original':True,
                           'runtime_mirror_matches':True,'encrypted_disc_verified':packed})
        self.assertEqual((WORK/'install_report.json').read_bytes(),install_record)

    def test_item_prices_restored_zh_cn(self):
        self._item_prices_restored('zh-CN', 'zh-CN-unifont06')

    def test_item_prices_restored_ko_kr(self):
        self._item_prices_restored('ko-KR', 'ko-KR-unifont-full06')

    def test_item_prices_restored_en_us(self):
        self._item_prices_restored('en-US', 'en-US-unifont-full06')

    def test_item_price_guard_rejects_legacy_zeroing(self):
        from fls.builders import build_alpha as base
        data=bytearray((base.SOURCE/'SYSTEM/ITEM.DAT').read_bytes())
        data[12*36+18:12*36+20]=b'\0\0'
        with self.assertRaisesRegex(AssertionError,'Item attributes changed'):
            base.sync_resource_mirrors({'SYSTEM/ITEM.DAT':bytes(data)},[])
        self.assertEqual(base.catalog_capacity({'file':'SYSTEM/ITEM.DAT','id':'legacy',
                                              'offset':'0x1b0','capacity_bytes':20}),18)

    def test_chinese_builder_preserves_item_values(self):
        from fls.builders import build_alpha as base
        from fls.text.extract_text import event_header,event_strings
        from fls.text.translation_data import read_corpus_rows
        output=self.output/'chinese-resources';output.mkdir()
        changes,report=base.build_resources('unifont',enforce_capacity=False,stage=output)
        base.verify_item_attributes(changes['SYSTEM/ITEM.DAT'])
        original=(base.SOURCE/'SYSTEM/ITEM.DAT').read_bytes()
        start=(base.SOURCE/'SYSTEM/MAIN.DAT').read_bytes().index(original)
        self.assertEqual(changes['SYSTEM/MAIN.DAT'][start:start+len(original)],changes['SYSTEM/ITEM.DAT'])
        materialize('staging/zh-CN-unifont06')
        frozen=(WORK/'staging/zh-CN-unifont06/resources/SYSTEM/ITEM.DAT').read_bytes()
        for off in range(0,len(original),36):
            self.assertEqual(changes['SYSTEM/ITEM.DAT'][off:off+18],frozen[off:off+18])

        target='EVE/EVE09_1.EVT'
        # The rebuilt payload outgrows the frozen 8192-byte extent, so the
        # builder moves it into an unclaimed sector run and repoints the
        # directory record instead of rejecting the build.
        self.assertTrue(report['packable'])
        self.assertEqual([item for item in report['capacity_overflows'] if item['file']==target],[])
        source_event=(base.SOURCE/target).read_bytes()
        patched_event=changes[target]
        event_report=json.loads((output/'event_capacity.json').read_text(encoding='utf-8'))
        target_report=next(row for row in event_report if row['file']==target)
        self.assertEqual(target_report['patched_bytes'],len(patched_event))
        self.assertGreater(len(patched_event),8192)
        relocated_sectors=(len(patched_event)+2047)//2048
        self.assertEqual(target_report['overflow_bytes'],0)
        self.assertEqual(target_report['relocated_sectors'],relocated_sectors)
        self.assertEqual(target_report['allocated_bytes'],relocated_sectors*2048)
        self.assertEqual(target_report['available_sectors'],relocated_sectors)
        relocated_lba=target_report['relocated_lba']
        self.assertTrue(any(start<=relocated_lba and relocated_lba+relocated_sectors<=start+count
                            for start,count in iso_layout.free_extents()),(relocated_lba,relocated_sectors))
        source_header=event_header(source_event);patched_header=event_header(patched_event)
        source_strings=event_strings(source_event,source_header)
        patched_strings=event_strings(patched_event,patched_header)
        self.assertEqual(len(patched_strings),len(source_strings))
        self.assertEqual(patched_header['events'],source_header['events'])
        self.assertEqual([item['offset']-patched_header['script_start'] for item in patched_strings],
                         [item['offset']-source_header['script_start'] for item in source_strings])
        glyph_map=json.loads((output/'glyph_map.json').read_text(encoding='utf-8'))['all']
        reverse={bytes.fromhex(pair):character for character,pair in glyph_map.items()}
        event_rows=[row for row in read_corpus_rows('zh-CN',{'event'}) if row['file']==target]
        translated_rows=[row for row in event_rows if row['translation']]
        self.assertEqual(set(target_report['translation_ids']),{row['id'] for row in translated_rows})
        pair_start=patched_header['table_offset']+2
        pairs=[patched_event[pos:pos+2] for pos in range(pair_start,patched_header['script_start'],2)]
        decoded_ids=set()
        for row,string in zip(event_rows,patched_strings):
            if not row['translation']:
                continue
            recovered=''.join(reverse[pairs[index-1]] for index in string['codes'])
            self.assertEqual(recovered,row['translation'],row['id'])
            decoded_ids.add(row['id'])
        self.assertEqual(decoded_ids,{row['id'] for row in translated_rows})

        native={character:bytes.fromhex(pair) for character,pair in glyph_map.items()}
        _,_,capacity=base.build_events(read_corpus_rows('zh-CN',{'event'}),native)
        enforced=next(row for row in capacity if row['file']==target)
        self.assertEqual(enforced['overflow_bytes'],0)
        self.assertEqual((enforced['relocated_lba'],enforced['relocated_sectors']),
                         (relocated_lba,relocated_sectors))
        relocated=self.output/'chinese-full-capacity'
        invoke('fls.builders.build_full_languages',['--language','zh-CN','--base','zh-CN-unifont07',
                                       '--output',str(relocated),'--resources-only'])
        staged=json.loads((relocated/'resource_report.json').read_text(encoding='utf-8'))
        self.assertTrue(staged['packable'])
        self.assertEqual([item for item in staged['capacity_overflows'] if item['file']==target],[])
        self.assertEqual((relocated/'resources'/target).read_bytes(),patched_event)
        self.assertEqual(len(patched_event)-len(source_event),
                         2*(target_report['glyphs']-target_report['original_glyphs']))
        self.proof.append({'chinese_source_builder_original_item_attributes':True,'item_names_preserved':True,
                           'event_relocation':{'lba':relocated_lba,'sectors':relocated_sectors,
                                               'bytes':len(patched_event)},
                           'all_translated_event_rows_decoded':len(decoded_ids),
                           'instruction_relative_offsets_preserved':True})

    def _frontend_menu_containers(self, code,source):
        from fls.containers.container_atlases import decode_container, parse_atlas
        from fls.builders.build_alpha import digest
        install_record = (WORK/'install_report.json').read_bytes()
        policy = (WORK/'config/storage_policy.json').read_bytes()
        result = apply_recipe(source,self.output/(code+'-frontend'),code,'frontend-menu',
                              resources_only=True,create_preview=False)
        self.assertEqual(result.resources,result.original_resources)
        self.assertFalse((result.stage/'FarlandSaga.bin').exists())
        atlas = result.report['frontend_menu']['container_atlases'][0]
        self.assertEqual([row['part'] for row in atlas['labels']],[404,399,382,426,360,398,375,386])
        self.assertEqual([row['part'] for row in atlas['panels']],list(range(104,111)))
        for name in ['FarlandSaga.exe','S-Tribute_Data/resources.assets',
                     'S-Tribute_Data/sharedassets0.assets','S-Tribute_Data/level0']:
            self.assertEqual(digest(result.stage/name),digest(result.source/name),name)
        header,data,_ = decode_container(result.stage/'Texture.tex','Texture.tex')
        original_header,original_data,_ = decode_container(WORK/'assets/sources/Texture.tex','Texture.tex')
        image,boxes,_,metadata = parse_atlas(data)
        original,original_boxes,_,original_metadata = parse_atlas(original_data)
        self.assertEqual(header,original_header)
        self.assertEqual(boxes,original_boxes)
        self.assertEqual(metadata,original_metadata)
        # Seven language slots must show exactly the same candidate prompt.
        first = image.crop(boxes[104]).crop((16,61,212,207))
        for index in range(105,111):
            self.assertEqual(first.tobytes(),image.crop(boxes[index]).crop((16,61,212,207)).tobytes())
        changed_parts={row['part'] for row in atlas['labels']}|set(range(104,111))
        for index,box in enumerate(boxes):
            if index not in changed_parts:self.assertEqual(image.crop(box).tobytes(),original.crop(box).tobytes(),index)
        if code=='ko-KR':
            rebuild=self.output/'frontend-full-rebuild'
            invoke('fls.builders.build_full_languages',['--language','ko','--base',str(result.stage),
                                           '--output',str(rebuild),'--resources-only'])
            rebuilt_report=json.loads((rebuild/'resource_report.json').read_text())
            self.assertEqual(digest(rebuild/'Texture.tex'),digest(result.stage/'Texture.tex'))
            self.assertEqual(rebuilt_report['frontend_menu'],json.loads(json.dumps(result.report['frontend_menu'])))
        self.proof.append({'frontend_menu_language':code,'encrypted_atlas_readback':True,
                           'sprite_metadata_and_unrelated_parts_unchanged':True,'native_resources_unchanged':True})
        self.assertEqual((WORK/'install_report.json').read_bytes(),install_record)
        self.assertEqual((WORK/'config/storage_policy.json').read_bytes(),policy)

    def test_frontend_menu_containers_zh_cn(self):
        self._frontend_menu_containers('zh-CN', 'zh-CN-unifont03')

    def test_frontend_menu_containers_ko_kr(self):
        self._frontend_menu_containers('ko-KR', 'ko-KR-unifont-full03')

    def test_frontend_menu_containers_en_us(self):
        self._frontend_menu_containers('en-US', 'en-US-unifont-full02')

    def test_frontend_menu_rejects_wrong_source_and_overflow(self):
        recipe=json.loads((REPO/'config/patches/frontend-menu.json').read_text())
        path=self.output/'bad-atlas.json'
        recipe['container_atlases'][0]['original_sha256']='0'*64
        path.write_text(json.dumps(recipe,ensure_ascii=False))
        with self.assertRaisesRegex(ValueError,'Frozen atlas source identity mismatch'):
            apply_recipe('en-US-unifont-full02',self.output/'bad-atlas','en-US',path,resources_only=True)
        recipe=json.loads((REPO/'config/patches/frontend-menu.json').read_text())
        recipe['container_atlases'][0]['labels'][0]['text']['en-US']='Rewind '*100
        path.write_text(json.dumps(recipe,ensure_ascii=False))
        with self.assertRaisesRegex(ValueError,'Atlas text overflow'):
            apply_recipe('en-US-unifont-full02',self.output/'atlas-overflow','en-US',path,resources_only=True)

    def test_frontend_container_fit_contract(self):
        from fls.containers.container_atlases import draw_text
        from fls.core.path_compat import local_path
        from PIL import Image, ImageDraw, ImageFont

        recipe=json.loads((REPO/'config/patches/frontend-menu.json').read_text())
        atlas=recipe['container_atlases'][0]
        text='Rewind';preferred=30;box=(0,0,140,52);font_path=local_path(
            Path('C:/Windows/Fonts')/atlas['fonts']['en-US'])
        rendered=Image.new('RGBA',(140,52),(0,0,0,0))
        report=draw_text(rendered,box,text,font_path,preferred,align='left')

        # This is the pre-fit renderer: preferred size, original baseline and
        # original left inset. A fitting no-op must keep every pixel identical.
        legacy=Image.new('RGBA',(140,52),(0,0,0,0))
        font=ImageFont.truetype(str(font_path),preferred)
        bounds=font.getbbox(text)
        x=box[0]+4;y=box[1]+(box[3]-box[1]-(bounds[3]-bounds[1]))//2
        ImageDraw.Draw(legacy).text((x-bounds[0],y-bounds[1]),text,font=font,
                                    fill=(255,255,255,255))
        self.assertEqual(rendered.tobytes(),legacy.tobytes())
        self.assertEqual(report['preferred_font_size'],preferred)
        self.assertEqual(report['actual_font_size'],preferred)
        self.assertEqual(report['line_count'],1)
        self.assertFalse(report['reduced_font'])
        self.assertEqual(report['bounds'][0],x)

        long_text='게임을 종료하고 게임 선택 화면으로 돌아가시겠습니까?'
        frame=(0,0,400,48)
        fitted=draw_text(Image.new('RGBA',(400,48),(0,0,0,0)),frame,long_text,
                         local_path(Path('C:/Windows/Fonts')/atlas['fonts']['ko-KR']),30,
                         align='center',locale='ko-KR',identifier='fit-long-fixture',part=15)
        self.assertEqual(fitted['text'],long_text)
        self.assertEqual(fitted['lines'],[long_text])
        self.assertEqual(fitted['line_count'],1)
        self.assertEqual(fitted['preferred_font_size'],30)
        self.assertLess(fitted['actual_font_size'],30)
        self.assertTrue(fitted['reduced_font'])
        left,top,right,bottom=fitted['bounds']
        self.assertGreaterEqual(left,frame[0]);self.assertGreaterEqual(top,frame[1])
        self.assertLessEqual(right,frame[2]);self.assertLessEqual(bottom,frame[3])

        too_long='너무긴라벨'*40
        with self.assertRaisesRegex(ValueError,
                                    r'locale=ko-KR id=fit-overflow-fixture part=321') as caught:
            draw_text(Image.new('RGBA',(80,24),(0,0,0,0)),(0,0,80,24),too_long,
                      local_path(Path('C:/Windows/Fonts')/atlas['fonts']['ko-KR']),30,
                      locale='ko-KR',identifier='fit-overflow-fixture',part=321)
        self.assertIn(repr(too_long),str(caught.exception))
        self.assertIn('minimum_font_size=9',str(caught.exception))

    def _frontend_submenus(self, code,source):
        from fls.containers.container_atlases import decode_container,parse_atlas
        from PIL import Image,ImageDraw
        from fls.builders.build_alpha import digest
        config=json.loads((REPO/'config/patches/frontend-submenus.json').read_text())['container_atlases'][0]
        draft=json.loads((REPO/'translation/frontend_submenus.json').read_text())
        expected=(set(range(15,94))|set(range(111,114))|set(range(123,342))
                  |set(range(359,436))|{494,495})-{363,367,372,373,374,380,381,413,415}
        declared={row['part'] for row in config['labels']}
        self.assertEqual(declared,expected)
        self.assertEqual(len(declared),len(config['labels']))
        # Static native default-language table must contain no uncovered text.
        materialize('analysis/frontend-localization')
        table=json.loads((WORK/'analysis/frontend-localization/language-parts-table.json').read_text())[0]
        protected=set(range(499))-(expected|set(range(104,111)))
        self.assertEqual(set(table)-protected, (set(table)&declared)|{108})
        groups={group['id']:group for group in draft['groups']}
        install_record=(WORK/'install_report.json').read_bytes()
        policy=(WORK/'config/storage_policy.json').read_bytes()
        original_header,original_data,_=decode_container(WORK/config['source'],'Texture.tex')
        original,boxes,_,metadata=parse_atlas(original_data)
        result=apply_recipe(source,self.output/(code+'-submenus'),code,'frontend-submenus',
                            resources_only=True,create_preview=False)
        self.assertEqual(result.resources,result.original_resources)
        atlas=result.report['frontend_menu']['container_atlases'][0]
        for name in ['FarlandSaga.exe','S-Tribute_Data/resources.assets',
                     'S-Tribute_Data/sharedassets0.assets','S-Tribute_Data/level0']:
            self.assertEqual(digest(result.stage/name),digest(result.source/name),name)
        header,data,_=decode_container(result.stage/'Texture.tex','Texture.tex')
        image,actual_boxes,_,actual_metadata=parse_atlas(data)
        self.assertEqual(header,original_header)
        self.assertEqual(actual_boxes,boxes)
        self.assertEqual(actual_metadata,metadata)
        _,base_data,_=decode_container(result.source/'Texture.tex','Texture.tex')
        previous,_,_,_=parse_atlas(base_data)
        # The top-level menu is shared with the frontend-menu recipe. The
        # frozen base candidate was rasterized by an earlier FreeType build
        # (the locked venv was reinstalled afterwards), so replay that recipe
        # in this environment and require identical pixels instead of
        # comparing against the stored candidate bytes.
        menu=apply_recipe(source,self.output/(code+'-submenus-menu'),code,'frontend-menu',
                          resources_only=True,create_preview=False)
        _,menu_data,_=decode_container(menu.stage/'Texture.tex','Texture.tex')
        menu_image,menu_boxes,_,_=parse_atlas(menu_data)
        # The top-level menu must stay pixel-identical to that replay. View
        # panel text is bound to the current theme, so preserve every pixel
        # outside the declared title/body rectangles and verify its actual
        # text below.
        for part in [404,399,382,426,360,398,375,386]:
            self.assertEqual(image.crop(boxes[part]).tobytes(),menu_image.crop(menu_boxes[part]).tobytes(),part)
        panel_reports={row['part']:row for row in atlas['panels']}
        panel_ids=config['panel_translation_ids']
        for part in range(104,111):
            before_panel=previous.crop(boxes[part]);after_panel=image.crop(boxes[part])
            text_mask=Image.new('L',before_panel.size,0);mask_draw=ImageDraw.Draw(text_mask)
            for region in [(16,6,212,52),(16,61,212,207)]:
                mask_draw.rectangle((region[0],region[1],region[2]-1,region[3]-1),fill=255)
            preserved=after_panel.copy();preserved.paste(before_panel,(0,0),text_mask)
            self.assertEqual(preserved.tobytes(),before_panel.tobytes(),part)
            self.assertEqual(panel_reports[part]['title']['text'],
                             groups[panel_ids['title']]['text'][code])
            self.assertEqual(panel_reports[part]['button']['text'],
                             groups[panel_ids['button']]['text'][code])
        for part in protected:
            self.assertEqual(image.crop(boxes[part]).tobytes(),original.crop(boxes[part]).tobytes(),part)
        for part in range(430,436):
            box=boxes[part];spinner=(box[0]+228,box[1],box[2],box[3])
            self.assertEqual(image.crop(spinner).tobytes(),original.crop(spinner).tobytes(),part)
        rendered_by_part={row['part']:row for row in atlas['labels']}
        for row in config['labels']:
            actual=rendered_by_part[row['part']]
            self.assertEqual(actual['translation_id'],row['translation_id'])
            reviewed=groups[row['translation_id']]['text'][code]
            normalize=lambda text:''.join(text.split())
            self.assertEqual(normalize(actual['text']),normalize(reviewed))
            preferred=row.get('font_size',{}).get(code,32)
            self.assertEqual(actual['preferred_font_size'],preferred)
            self.assertEqual(actual['actual_font_size'],actual['font_size'])
            self.assertEqual(actual['line_count'],len(actual['lines']))
            self.assertEqual(actual['reduced_font'],actual['actual_font_size']<preferred)
            draw_box=actual['draw_region'];left,top,right,bottom=actual['bounds']
            self.assertGreaterEqual(left,draw_box[0]);self.assertGreaterEqual(top,draw_box[1])
            self.assertLessEqual(right,draw_box[2]);self.assertLessEqual(bottom,draw_box[3])
            self.assertLessEqual(actual['line_count'],row.get('max_lines',1))
        self.assertEqual(len(atlas['labels']),371)
        self.assertTrue(atlas['outside_pixels_unchanged'])
        self.assertTrue(all(not row['overflow'] for row in atlas['labels']))
        if code=='zh-CN':
            self.assertEqual(atlas['fallback_font']['codepoints'],['U+30FB'])
            self.assertEqual(sorted({cp for row in atlas['labels']
                                     for cp in row['fallback_codepoints']}),['U+30FB'])
        self.proof.append({'frontend_submenus_language':code,'all_text_parts':371,
                           'original_main_menu_and_view_panel_art_retained':True,
                           'spinner_art_and_sprite_metadata_unchanged':True,
                           'fit_sizes_lines_bounds_and_current_theme_text_verified':True,
                           'simhei_missing_glyph_30fb_fallback_only':code=='zh-CN'})
        self.assertEqual((WORK/'install_report.json').read_bytes(),install_record)
        self.assertEqual((WORK/'config/storage_policy.json').read_bytes(),policy)

    def test_frontend_submenus_zh_cn(self):
        self._frontend_submenus('zh-CN', 'zh-CN-unifont04')

    def test_frontend_submenus_ko_kr(self):
        self._frontend_submenus('ko-KR', 'ko-KR-unifont-full04')

    def test_frontend_submenus_en_us(self):
        self._frontend_submenus('en-US', 'en-US-unifont-full04')

    def _save_folder_runtime_hooks(self, code,source,expected):
        from fls.builders.build_alpha import digest,EXE
        from fls.text.runtime_text import replay_runtime_text
        install_record=(WORK/'install_report.json').read_bytes()
        policy=(WORK/'config/storage_policy.json').read_bytes()
        original=EXE.read_bytes()
        recipe=json.loads((REPO/'config/patches/save-folder.json').read_text())
        labels=recipe['runtime_word_writes']['labels']
        self.assertEqual(len(labels),6)
        self.assertEqual({row['ordinal'] for row in labels},{1,2})
        self.assertEqual({int(write['memory_address'],0) for row in labels for write in row['word_writes']},
                         set(range(0x0601b828,0x0601b830,2))|set(range(0x0601b83c,0x0601b844,2)))
        result=apply_recipe(source,self.output/(code+'-folder'),code,'save-folder',
                            resources_only=True,create_preview=False)
        self.assertEqual(result.resources,result.original_resources)
        self.compare(result,expected)
        before=(result.source/'FarlandSaga.exe').read_bytes()
        patched=(result.stage/'FarlandSaga.exe').read_bytes()
        permitted=set()
        runtime=result.report['save_folder']['runtime_word_writes']
        self.assertEqual(runtime['word_write_count'],24)
        mapping=json.loads((result.stage/'glyph_map.json').read_text())
        mapping=mapping.get('all',{row.get('token',row.get('character')):row['cp932_hex'] for row in mapping['added']})
        for row in runtime['labels']:
            encoded=bytes.fromhex(row['encoded_hex'])
            recovered=[]
            for i,write in enumerate(row['word_writes']):
                off=int(write['offset'],0);permitted.update(range(off,off+4))
                pair=encoded[i*2:i*2+2]
                self.assertEqual(patched[off:off+4],int.from_bytes(pair,'big').to_bytes(4,'little'))
                for token,cp in mapping.items():
                    if bytes.fromhex(cp)==pair:recovered.append(token);break
                else:self.assertEqual(pair,bytes.fromhex('8140'))
                self.assertEqual(patched[off-4:off],original[off-4:off])
                self.assertEqual(patched[off+4:off+14],original[off+4:off+14])
            self.assertEqual(''.join(recovered).replace('　',''),row['text'])
        self.assertTrue(all(i in permitted for i,(a,b) in enumerate(zip(before,patched)) if a!=b))
        for name in ['Texture.tex','S-Tribute_Data/resources.assets','S-Tribute_Data/sharedassets0.assets','S-Tribute_Data/level0']:
            self.assertEqual(digest(result.stage/name),digest(result.source/name),name)
        # Reapplying the recipe must leave the already-fixed code identical.
        repeated=apply_recipe(expected,self.output/(code+'-folder-repeat'),code,'save-folder',
                              resources_only=True,create_preview=False)
        self.assertEqual((repeated.stage/'FarlandSaga.exe').read_bytes(),patched)
        if code=='ko-KR':
            rebuild=self.output/'folder-full-rebuild'
            invoke('fls.builders.build_full_languages',['--language','ko','--base',expected,
                                          '--output',str(rebuild),'--resources-only'])
            self.assertEqual((rebuild/'FarlandSaga.exe').read_bytes(),patched)
            rebuilt_report=json.loads((rebuild/'resource_report.json').read_text())
            self.assertEqual(rebuilt_report['save_folder'],json.loads((WORK/'staging'/expected/'save_folder_report.json').read_text()))
            # Full builds regenerate the executable before replaying hooks.
            import shutil
            shutil.copyfile(result.source/'FarlandSaga.exe',rebuild/'FarlandSaga.exe')
            replay_runtime_text(WORK/'staging'/expected,rebuild)
            self.assertEqual((rebuild/'FarlandSaga.exe').read_bytes(),patched)
        if code=='zh-CN':
            # Later font edits must preserve host runtime text immediates.
            font=bytearray(result.resources['SYSTEM/KANJI.FON']);font[100]^=1
            result.resources['SYSTEM/KANJI.FON']=bytes(font)
            result.sync_description_font()
            synced=(result.stage/'FarlandSaga.exe').read_bytes()
            for off in permitted:self.assertEqual(synced[off],patched[off])
        self.proof.append({'save_folder_language':code,'all_six_hooks_localized':True,
                           'instruction_addresses_calls_and_unrelated_files_unchanged':True})
        self.assertEqual((WORK/'install_report.json').read_bytes(),install_record)
        self.assertEqual((WORK/'config/storage_policy.json').read_bytes(),policy)

    def test_save_folder_runtime_hooks_zh_cn(self):
        self._save_folder_runtime_hooks('zh-CN', 'zh-CN-unifont05', 'zh-CN-unifont06')

    def test_save_folder_runtime_hooks_ko_kr(self):
        self._save_folder_runtime_hooks('ko-KR', 'ko-KR-unifont-full05', 'ko-KR-unifont-full06')

    def test_save_folder_runtime_hooks_en_us(self):
        self._save_folder_runtime_hooks('en-US', 'en-US-unifont-full05', 'en-US-unifont-full06')

    def test_save_folder_rejects_changed_instructions_and_overflow(self):
        import copy
        recipe=json.loads((REPO/'config/patches/save-folder.json').read_text())
        bad_source=copy.deepcopy(recipe)
        bad_source['runtime_word_writes']['original_sha256']='0'*64
        bad_code=copy.deepcopy(recipe)
        bad_code['runtime_word_writes']['labels'][0]['word_writes'][0]['instruction_hex']='00'*18
        too_long=copy.deepcopy(recipe)
        too_long['runtime_word_writes']['labels'][0]['text']['zh-CN']='文件夹文件夹'
        for key,value,error in [('source',bad_source,'Frozen executable identity mismatch'),
                                ('instructions',bad_code,'Frozen runtime word-write instruction mismatch'),
                                ('capacity',too_long,'Runtime text capacity overflow')]:
            path=self.output/('runtime-'+key+'.json');path.write_text(json.dumps(value,ensure_ascii=False))
            with self.assertRaisesRegex(ValueError,error):
                apply_recipe('zh-CN-unifont05',self.output/('runtime-bad-'+key),'zh-CN',path,
                             resources_only=True,create_preview=False)

    def test_ui_builders_can_reuse_localized_base(self):
        from fls.builders.build_ui_labels import LEGEND_END,LEGEND_START
        from PIL import Image

        source=WORK/'staging/ko-KR-unifont-full03'
        materialize(source)
        before={p.relative_to(source/'resources').as_posix():p.read_bytes()
                for p in (source/'resources').rglob('*') if p.is_file()}
        draft=json.loads((REPO/'translation/ui_labels.json').read_text(encoding='utf-8'))
        outputs=[]
        for name in ['ui-first','ui-repeat']:
            output=self.output/name
            invoke('fls.builders.build_ui_labels',['--language','ko-KR','--base',str(source),
                                     '--output',str(output),'--resources-only'])
            outputs.append(output)
        resources=[{p.relative_to(output/'resources').as_posix():p.read_bytes()
                    for p in (output/'resources').rglob('*') if p.is_file()} for output in outputs]
        self.assertEqual(resources[0],resources[1])
        report=json.loads((outputs[0]/'ui_labels_report.json').read_text(encoding='utf-8'))
        terrain_rows=report['terrain']['rows']
        self.assertEqual([(row['source'],row['text']) for row in terrain_rows],
                         [(row['source'],row['ko-KR']) for row in draft['terrain']])
        self.assertTrue(all(not row['overflow'] for row in terrain_rows))
        wasteland=next(row for row in terrain_rows if row['source']=='荒地')
        self.assertEqual(wasteland['text'],'황무지')
        self.assertEqual(wasteland['font_pixels'],9)
        self.assertLessEqual(wasteland['ink'][0],28)
        self.assertLessEqual(wasteland['ink'][1],16)
        stats_rows=report['stats']['rows']
        self.assertEqual([(row['source'],row['text']) for row in stats_rows],
                         [(row['source'],row['ko-KR']) for row in draft['stats']])
        self.assertTrue(all(not row['overflow'] for row in stats_rows))
        attribute=next(row for row in stats_rows if row['source']=='属性')
        self.assertEqual(attribute['atlas_x'],96)
        self.assertTrue(report['stats']['tilemap_unchanged'])
        self.assertEqual(resources[0]['SYSTEM/N_MENU.DAT'],before['SYSTEM/N_MENU.DAT'])

        terrain_resources={name for name in before if name.startswith('SYSTEM/SUBMAP') and name.endswith('.DAT')}
        self.assertEqual(len(terrain_resources),30)
        mirrors=report['stats']['mirrors']
        changed_regions=terrain_resources|{row['resource'] for row in mirrors}
        self.assertEqual(set(resources[0]),set(before))
        for name,data in before.items():
            if name not in changed_regions:
                self.assertEqual(resources[0][name],data,name)
        boxes=[row['box'] for row in terrain_rows]
        allowed_terrain={y*72+x for x0,y0,x1,y1 in boxes
                         for y in range(y0,y1) for x in range(x0,x1)}
        for name in terrain_resources:
            old,new=before[name],resources[0][name]
            self.assertEqual(new[:LEGEND_START],old[:LEGEND_START],name)
            self.assertEqual(new[8:520],old[8:520],name)
            self.assertEqual(new[LEGEND_END:],old[LEGEND_END:],name)
            old_pixels=Image.frombytes('P',(72,120),old[LEGEND_START:LEGEND_END]).tobytes()
            new_pixels=Image.frombytes('P',(72,120),new[LEGEND_START:LEGEND_END]).tobytes()
            self.assertTrue(all(a==b for i,(a,b) in enumerate(zip(old_pixels,new_pixels))
                                if i not in allowed_terrain),name)
            for y in range(22,118):
                for x in [*range(5,13),*range(44,71)]:
                    self.assertEqual(new_pixels[y*72+x],old_pixels[y*72+x],name)

        menu=resources[0]['SYSTEM/MENU.CEL']
        stat_tiles={((16+2*ty)*32+row['atlas_x']//8+tx)*64+byte
                    for row in draft['stats'] for ty in range(2) for tx in range(4)
                    for byte in range(64)}
        for mirror in mirrors:
            name=mirror['resource'];offset=int(mirror['offset'],0);size=mirror['size']
            self.assertEqual(resources[0][name][offset:offset+size],menu[:size],name)
            old_chunk,new_chunk=before[name][offset:offset+size],resources[0][name][offset:offset+size]
            self.assertTrue(all(a==b for i,(a,b) in enumerate(zip(old_chunk,new_chunk))
                                if i not in stat_tiles),name)
        self.assertEqual(resources[0]['SYSTEM/ITEM.DAT'],before['SYSTEM/ITEM.DAT'])
        item_offset=0x2a5e4;item_data=resources[0]['SYSTEM/ITEM.DAT']
        self.assertEqual(resources[0]['SYSTEM/MAIN.DAT'][item_offset:item_offset+len(item_data)],item_data)
        self.assertEqual(resources[0]['SYSTEM/MAIN.DAT'][item_offset:item_offset+len(item_data)],
                         before['SYSTEM/MAIN.DAT'][item_offset:item_offset+len(item_data)])
        self.proof.append({'builder':'build_ui_labels','current_draft_text_rendered':True,
                           'terrain_황무지_font_pixels':9,'terrain_and_stats_regions_protected':True,
                           'property_label_slot_0x418_remains_independent':True,
                           'item_attributes_and_mirror_preserved':True,'same_input_rebuild_idempotent':True})

    def test_equipment_builder_can_reuse_localized_base(self):
        from fls.builders import build_alpha as base

        source=WORK/'staging/ko-KR-unifont-full03'
        materialize(source)
        before={p.relative_to(source/'resources').as_posix():p.read_bytes()
                for p in (source/'resources').rglob('*') if p.is_file()}
        draft=json.loads((REPO/'translation/ui_labels.json').read_text(encoding='utf-8'))
        outputs=[]
        for name in ['equipment-first','equipment-repeat']:
            output=self.output/name
            invoke('fls.builders.build_equipment_labels',['--language','ko-KR','--base',str(source),
                                             '--output',str(output),'--resources-only'])
            outputs.append(output)
        resources=[{p.relative_to(output/'resources').as_posix():p.read_bytes()
                    for p in (output/'resources').rglob('*') if p.is_file()} for output in outputs]
        self.assertEqual(resources[0],resources[1])
        report=json.loads((outputs[0]/'equipment_labels_report.json').read_text(encoding='utf-8'))
        rows=report['rows']
        self.assertEqual([(row['source'],row['text']) for row in rows],
                         [(row['source'],row['ko-KR']) for row in draft['equipment']])
        self.assertTrue(all(not row['overflow'] for row in rows))
        fit_rows=[row for row in rows if row['render_method']=='windows_font_fit']
        self.assertEqual({row['source'] for row in fit_rows},{'防具','持物'})
        for row in fit_rows:
            self.assertGreaterEqual(row['font_pixels'],9)
            self.assertLessEqual(row['glyph_bounds'][0],32)
            self.assertLessEqual(row['glyph_bounds'][1],16)
            self.assertLessEqual(row['glyph_bounds'][2],32)
            self.assertLessEqual(row['glyph_bounds'][3],16)
            self.assertEqual(base.digest(base.local_path(row['font_source'])),row['font_sha256'])
            self.assertEqual(row['font_sha256'],report['fit_font']['sha256'])
        normal_rows=[row for row in rows if row['render_method']=='unifont_bitmap']
        self.assertTrue(all(row['font_pixels']==16 for row in normal_rows))

        atlas_mirrors=report['atlas_mirrors'];map_mirrors=report['map_row_mirrors']
        touched={row['resource'] for row in atlas_mirrors+map_mirrors}
        self.assertEqual(set(resources[0]),set(before))
        for name,data in before.items():
            if name not in touched:
                self.assertEqual(resources[0][name],data,name)
        allowed_atlas={(int(row['first_code'],16)+ty*128)*32+byte
                       for row in rows for ty in range(2) for byte in range(256)}
        allowed_resources={name:set() for name in touched}
        for mirror in atlas_mirrors:
            name=mirror['resource'];offset=int(mirror['offset'],0);size=mirror['size']
            old=before[name][offset:offset+size]
            new=resources[0][name][offset:offset+size]
            self.assertEqual(new,resources[0]['SYSTEM/MENU.CEL'][:size],name)
            self.assertTrue(all(a==b for i,(a,b) in enumerate(zip(old,new))
                                if i not in allowed_atlas),name)
            allowed_resources[name].update(offset+i for i in allowed_atlas if i<size)
        for row in map_mirrors:
            off=int(row['offset'],0);allowed_resources[row['resource']].update(range(off,off+12))
        for name,allowed in allowed_resources.items():
            self.assertTrue(all(a==b for i,(a,b) in enumerate(zip(before[name],resources[0][name]))
                                if i not in allowed),name)
        self.assertEqual(resources[0]['SYSTEM/ITEM.DAT'],before['SYSTEM/ITEM.DAT'])
        item_offset=0x2a5e4;item_data=resources[0]['SYSTEM/ITEM.DAT']
        self.assertEqual(resources[0]['SYSTEM/MAIN.DAT'][item_offset:item_offset+len(item_data)],item_data)
        self.assertEqual(resources[0]['SYSTEM/MAIN.DAT'][item_offset:item_offset+len(item_data)],
                         before['SYSTEM/MAIN.DAT'][item_offset:item_offset+len(item_data)])
        self.proof.append({'builder':'build_equipment_labels','current_draft_text_rendered':True,
                           'only_overwide_labels_ttf_fit':True,'font_hashes_and_glyph_bounds_reported':True,
                           'atlas_and_map_regions_protected':True,'item_attributes_and_mirror_preserved':True,
                           'same_input_rebuild_idempotent':True})

    def test_reject_output_outside_workspace(self):
        with self.assertRaises(ValueError):stage_path(WORK.parent/'illegal-output')

    def test_unsupported_language_rejected(self):
        with self.assertRaises(ValueError):
            apply_recipe('en-US-unifont-full02',self.output/'bad-language','en-US','menu-spacing',resources_only=True)

    def test_field_overflow_rejected(self):
        recipe=json.loads((REPO/'config/patches/menu-spacing.json').read_text())
        recipe['fields'][0]['text']['ko-KR']='장비정보'
        path=self.output/'overflow.json';path.write_text(json.dumps(recipe,ensure_ascii=False))
        with self.assertRaisesRegex(ValueError,'NUL field overflow'):
            apply_recipe('ko-KR-unifont-full02',self.output/'overflow-output','ko-KR',path,
                         resources_only=True,create_preview=False)

def _discover():
    """Flat list of every test id in this script, for shard selection."""
    loader = unittest.TestLoader()
    ids = []

    def walk(suite):
        for item in suite:
            if isinstance(item, unittest.TestSuite):
                walk(item)
            else:
                ids.append(f'{item.__class__.__name__}.{item._testMethodName}')

    walk(loader.loadTestsFromModule(sys.modules[__name__]))
    return ids

if __name__=='__main__':
    explicit=[name for name in sys.argv[1:] if not name.startswith('-')]
    if explicit:
        chosen=explicit
    else:
        shard=current_shard()
        chosen=[]
        if shard is not None:
            index,jobs=shard
            chosen=balance(_discover(),jobs)[index-1]
            if not chosen:
                sys.stderr.write(f'shard {index}/{jobs}: no tests assigned\n')
                sys.exit(0)
    unittest.main(argv=[sys.argv[0],*chosen],verbosity=2)
