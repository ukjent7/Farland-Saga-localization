"""Build one searchable review page for all existing Chinese, Korean, and English text."""
import json
from pathlib import Path

from path_compat import REPO
from translation_data import LANGUAGES, read_corpus

LANGUAGE_NAMES = {'zh-CN': '中文', 'ko-KR': '한국어', 'en-US': 'English'}
THEME_NAMES = {
    'event': '剧情', 'catalog': '菜单与名称', 'description': '说明',
    'host': 'Steam文字', 'unity': 'Unity文字', 'condition': '状态标签',
    'glossary': '术语', 'title': '图片标题', 'ui:terrain': '地形标签',
    'ui:equipment': '装备标签', 'ui:stats': '状态标签', 'frontend': '前端界面',
}


def _row(identifier, theme, source, translations, reviews=None, **metadata):
    translations = {language: translations.get(language, '') for language in LANGUAGES}
    return {'id': identifier, 'theme': theme, 'theme_name': THEME_NAMES[theme],
            'source': source, 'translations': translations,
            'reviews': reviews or {}, **metadata}


def collect_rows():
    rows = []
    for source_row in read_corpus()['rows']:
        kind = source_row.get('kind', 'other')
        location = ':'.join(str(source_row[key]) for key in ('file', 'offset') if source_row.get(key))
        rows.append(_row(source_row['id'], kind, source_row.get('source', ''),
                         source_row['translations'], source_row.get('reviews'),
                         location=location, capacity=source_row.get('capacity_bytes', ''),
                         review_status=source_row.get('reviews', {}).get('zh-CN', {}).get('status', '')))

    glossary = json.loads((REPO / 'translation/glossary.json').read_text(encoding='utf-8'))
    for item in glossary['rows']:
        rows.append(_row(item['id'], 'glossary', item['source'], item['translations'], item.get('reviews'),
                         review_status=item.get('reviews', {}).get('zh-CN', {}).get('status', ''),
                         category=item.get('category', '')))

    titles = json.loads((REPO / 'translation/titles.json').read_text(encoding='utf-8'))
    for item in titles['rows']:
        notes = {'style': item.get('style', {}), 'short_translations': item.get('short_translations', {})}
        rows.append(_row(item['id'], 'title', item['source'], item['translations'],
                         {'shared': notes}, location=item.get('file', ''),
                         review_status=titles.get('status', '')))

    ui_labels = json.loads((REPO / 'translation/ui_labels.json').read_text(encoding='utf-8'))
    for section in ('terrain', 'equipment', 'stats'):
        for index, item in enumerate(ui_labels.get(section, []), 1):
            identifier = item.get('id') or f'ui:{section}:{item.get("source", index)}'
            translations = {language: item.get(language, '') for language in LANGUAGES}
            metadata = {key: value for key, value in item.items()
                        if key not in {'id', 'source', *LANGUAGES}}
            rows.append(_row(identifier, 'ui:' + section, item.get('source', ''), translations,
                             {'shared': metadata}, location=f'{section}[{index}]',
                             review_status=ui_labels.get('status', '')))

    frontend = json.loads((REPO / 'translation/frontend_submenus.json').read_text(encoding='utf-8'))
    for item in frontend['groups']:
        rows.append(_row(item['id'], 'frontend', item.get('source', ''), item.get('text', {}),
                         {'shared': {'parts': item.get('parts', [])}},
                         review_status=frontend.get('review', '')))
    return rows


def build_page(rows):
    themes = sorted({row['theme'] for row in rows})
    data = json.dumps(rows, ensure_ascii=False, separators=(',', ':')).replace('<', r'\u003c')
    theme_options = ''.join(f'<option value="{theme}">{THEME_NAMES[theme]}</option>' for theme in themes)
    page = r'''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FARLAND SAGA I · 三语译稿审阅</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f4f2ec;color:#292e30;font:14px/1.55 system-ui,"Microsoft YaHei",sans-serif}
main{max-width:1800px;margin:auto;padding:34px 22px}h1{font-size:26px;margin:0 0 5px}.muted{color:#687174}
.stats{display:flex;gap:12px;flex-wrap:wrap;margin:20px 0}.stat{background:white;padding:12px 18px;border-radius:8px;min-width:130px}.stat strong{display:block;font-size:24px;color:#17665e}
.bar{display:flex;gap:9px;flex-wrap:wrap;padding:12px;background:#e7eae4;border-radius:8px;position:sticky;top:0;z-index:1}
input,select{font:inherit;padding:8px;border:1px solid #c4cbc4;border-radius:5px;background:#fff}input{flex:1;min-width:230px}
.table-wrap{overflow:auto}table{width:100%;min-width:1180px;border-collapse:collapse;margin-top:14px;background:white}th,td{text-align:left;padding:10px 11px;border-bottom:1px solid #e2e6df;vertical-align:top}th{background:#17665e;color:#fff;position:sticky;top:59px}td:first-child{width:205px;font:12px/1.45 ui-monospace,monospace;color:#596669;overflow-wrap:anywhere}td:nth-child(2){width:150px;color:#687174}td:nth-child(n+3){width:18%}.empty{color:#abb1aa}.tag{display:inline-block;background:#e5efeb;color:#17665e;border-radius:4px;padding:1px 6px;margin:2px 0}.meta{color:#687174;font-size:12px;white-space:pre-wrap}.note{margin-top:20px;color:#687174;font-size:12px}
@media(max-width:700px){main{padding:20px 10px}}
</style><main>
<h1>FARLAND SAGA I · 中文、한국어、English 译稿</h1>
<div class="muted">统一审阅剧情、名称、说明、术语、图片文字、状态标签与前端界面；译文按稳定 ID 管理。</div>
<div class="stats"><div class="stat"><strong id="all"></strong>全部条目</div><div class="stat"><strong id="zh"></strong>中文已填</div><div class="stat"><strong id="ko"></strong>韩文已填</div><div class="stat"><strong id="en"></strong>英文已填</div></div>
<div class="bar"><input id="query" placeholder="搜索原文、三语译文、ID、位置或审校记录"><select id="theme"><option value="all">全部主题</option>__THEMES__</select><select id="filled"><option value="all">全部条目</option><option value="missing">存在空译文</option></select></div>
<p class="muted" id="count"></p><div class="table-wrap"><table><thead><tr><th>ID / 位置 / 主题</th><th>原文</th><th>中文</th><th>한국어</th><th>English</th><th>审校记录</th></tr></thead><tbody id="body"></tbody></table></div>
<p class="note">此页是统一只读审阅视图。合并译稿请使用 <code>flow merge --language zh-CN|ko-KR|en-US --draft work/...</code>；运行 <code>flow merge</code> 会只校验现有译稿。</p>
</main><script>
const rows=__DATA__, names=__LANGUAGES__;
document.querySelector('#all').textContent=rows.length.toLocaleString();
for(const code of ['zh-CN','ko-KR','en-US'])document.querySelector('#'+({'zh-CN':'zh','ko-KR':'ko','en-US':'en'}[code])).textContent=rows.filter(r=>r.translations[code]).length.toLocaleString();
const q=document.querySelector('#query'),theme=document.querySelector('#theme'),filled=document.querySelector('#filled'),body=document.querySelector('#body');
function plain(value){if(value===null||value===undefined)return '';return typeof value==='string'?value:JSON.stringify(value)}
function render(){const search=q.value.toLocaleLowerCase();const matched=rows.filter(r=>(theme.value==='all'||r.theme===theme.value)&&(filled.value==='all'||Object.values(r.translations).some(v=>!v))&&[r.id,r.theme_name,r.location,r.source,...Object.values(r.translations),plain(r.reviews),r.review_status,r.category].join(' ').toLocaleLowerCase().includes(search));body.replaceChildren();for(const r of matched){const tr=document.createElement('tr');const identity=document.createElement('td');identity.textContent=`${r.id}\n${r.location||''}\n`;const tag=document.createElement('span');tag.className='tag';tag.textContent=r.theme_name;identity.append(tag);tr.append(identity);for(const value of [r.source,r.translations['zh-CN'],r.translations['ko-KR'],r.translations['en-US']]){const td=document.createElement('td');td.textContent=value||'待补';if(!value)td.className='empty';tr.append(td)}const review=document.createElement('td');review.className='meta';review.textContent=[r.review_status,plain(r.reviews)].filter(Boolean).join('\n');tr.append(review);body.append(tr)}document.querySelector('#count').textContent=`显示 ${matched.length.toLocaleString()} 条`}
q.addEventListener('input',render);theme.addEventListener('change',render);filled.addEventListener('change',render);render();
</script></html>'''
    return page.replace('__DATA__', data).replace('__THEMES__', theme_options).replace(
        '__LANGUAGES__', json.dumps(LANGUAGE_NAMES, ensure_ascii=False))


def main():
    rows = collect_rows()
    (REPO / 'review.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    (REPO / 'review.html').write_text(build_page(rows), encoding='utf-8')
    print(f'Built shared Chinese/Korean/English review page for {len(rows)} entries')


if __name__ == '__main__':
    main()
