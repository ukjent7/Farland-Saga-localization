"""Build interactive standalone review_fs2.html for browsing all 2,660 dialogues in 3 languages."""
import html
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def generate_review_html():
    corpus_path = REPO / 'translation/fs2/corpus.json'
    if not corpus_path.exists():
        raise FileNotFoundError(f"Corpus file not found: {corpus_path}")

    corpus = json.loads(corpus_path.read_text(encoding='utf-8'))
    rows = corpus['rows']

    e = html.escape

    html_content = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>FARLAND SAGA II Saturn Tribute - 多语言剧情对白审阅页</title>
  <style>
    body {{ background: #0d1117; color: #c9d1d9; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; margin: 0; padding: 20px; }}
    h1 {{ color: #58a6ff; font-size: 24px; margin-bottom: 8px; }}
    .subtitle {{ color: #8b949e; font-size: 14px; margin-bottom: 20px; }}
    .controls {{ position: sticky; top: 0; background: #161b22; padding: 12px 16px; border: 1px solid #30363d; border-radius: 6px; margin-bottom: 20px; display: flex; gap: 12px; align-items: center; z-index: 100; }}
    input[type="text"] {{ flex: 1; background: #0d1117; border: 1px solid #30363d; color: white; padding: 8px 12px; border-radius: 6px; font-size: 14px; }}
    select {{ background: #0d1117; border: 1px solid #30363d; color: white; padding: 8px 12px; border-radius: 6px; font-size: 14px; }}
    .stats {{ color: #7ee787; font-weight: bold; font-size: 14px; }}
    .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 6px; margin-bottom: 12px; padding: 14px; }}
    .card-header {{ display: flex; justify-content: space-between; margin-bottom: 8px; color: #8b949e; font-size: 13px; font-weight: bold; }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 12px; }}
    .col {{ background: #0d1117; border: 1px solid #21262d; border-radius: 4px; padding: 10px; }}
    .col-title {{ font-size: 12px; font-weight: bold; margin-bottom: 6px; color: #58a6ff; text-transform: uppercase; }}
    pre {{ margin: 0; white-space: pre-wrap; font-family: inherit; font-size: 14px; line-height: 1.5; }}
    .blank {{ color: #484f58; font-style: italic; }}
    @media (max-width: 900px) {{
      .grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <h1>FARLAND SAGA II Saturn Tribute - 三语剧情对白审阅</h1>
  <div class="subtitle">共收录 {len(rows)} 页剧情对白 · 涵盖 17 个剧情库 · 简体中文 / 한국어 / English 对照</div>

  <div class="controls">
    <input type="text" id="search" placeholder="实时搜索对白文本（支持日文、中文、韩文、英文或 ID）..." oninput="filter()">
    <select id="bank-filter" onchange="filter()">
      <option value="all">所有剧情库 (Bank 00 - 16)</option>
      {"".join(f'<option value="{b}">Bank {b:02d}</option>' for b in range(17))}
    </select>
    <div class="stats" id="counter">显示 {len(rows)} / {len(rows)} 条</div>
  </div>

  <div id="container">
"""

    card_elements = []
    for r in rows:
        bid = r['bank']
        did = r['id']
        ja = r.get('source', '')
        zh = r.get('translations', {}).get('zh-CN', '')
        ko = r.get('translations', {}).get('ko-KR', '')
        en = r.get('translations', {}).get('en-US', '')
        is_blank = r.get('blank', False)

        card_elements.append(f"""
    <div class="card" data-bank="{bid}" data-text="{e((str(did) + ' ' + ja + ' ' + zh + ' ' + ko + ' ' + en).lower())}">
      <div class="card-header">
        <span>ID: {did} · Bank {bid:02d}</span>
        <span>{'[空槽/无语音]' if is_blank else '有效剧情'}</span>
      </div>
      <div class="grid">
        <div class="col">
          <div class="col-title">日文原版 (JA)</div>
          <pre>{e(ja) if ja else '<span class="blank">[空]</span>'}</pre>
        </div>
        <div class="col">
          <div class="col-title">简体中文 (zh-CN)</div>
          <pre>{e(zh) if zh else '<span class="blank">[空]</span>'}</pre>
        </div>
        <div class="col">
          <div class="col-title">한국어 (ko-KR)</div>
          <pre>{e(ko) if ko else '<span class="blank">[공백]</span>'}</pre>
        </div>
        <div class="col">
          <div class="col-title">English (en-US)</div>
          <pre>{e(en) if en else '<span class="blank">[blank]</span>'}</pre>
        </div>
      </div>
    </div>""")

    html_content += "".join(card_elements)
    html_content += """
  </div>

  <script>
    function filter() {
      const q = document.getElementById('search').value.toLowerCase().trim();
      const b = document.getElementById('bank-filter').value;
      const cards = document.querySelectorAll('.card');
      let count = 0;
      cards.forEach(card => {
        const matchBank = (b === 'all' || card.getAttribute('data-bank') === b);
        const matchQuery = (!q || card.getAttribute('data-text').includes(q));
        if (matchBank && matchQuery) {
          card.style.display = '';
          count++;
        } else {
          card.style.display = 'none';
        }
      });
      document.getElementById('counter').innerText = `显示 ${count} / ${cards.length} 条`;
    }
  </script>
</body>
</html>
"""
    out_path = REPO / 'review_fs2.html'
    out_path.write_text(html_content, encoding='utf-8')
    print(f"Generated {out_path} ({out_path.stat().st_size // 1024} KiB)")


if __name__ == '__main__':
    generate_review_html()
