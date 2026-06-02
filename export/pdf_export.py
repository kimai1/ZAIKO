"""
在庫レポートPDF生成（reportlab不要 → openpyxl経由でExcel→PDFは重いため、
ここでは WeasyPrint か pdfkit が入っていればそれを使い、
なければ HTML をそのまま返す簡易実装とする。
実際は Flask の send_file で HTML→ブラウザ印刷が最も手軽なため、
PDF出力は WeasyPrint を使う。WeasyPrintがなければ ImportError を出す。
"""
from datetime import datetime


def export_report_pdf(data: dict) -> bytes:
    try:
        from weasyprint import HTML, CSS
    except ImportError:
        raise RuntimeError(
            "PDF出力には weasyprint が必要です。\n"
            "pip install weasyprint でインストールしてください。"
        )

    html = _build_html(data)
    return HTML(string=html).write_pdf()


def _build_html(data: dict) -> str:
    cats = data["categories"]
    products = data["products"]
    generated = data["generated_at"]
    total_cost = data["total_cost"]
    total_sell = data["total_sell"]

    def yen(v):
        if not v:
            return "—"
        return f"¥{v:,.0f}"

    cat_rows = ""
    for c in cats:
        cat_rows += f"""
        <tr>
          <td>{c['cat']}</td>
          <td class="num">{c['cnt']}</td>
          <td class="num">{c['total_stock'] or 0}</td>
          <td class="num">{yen(c['total_cost'])}</td>
          <td class="num">{yen(c['total_sell'])}</td>
          <td class="num">{c['alert_cnt']}</td>
          <td class="num">{c['zero_cnt']}</td>
        </tr>"""

    prod_rows = ""
    for p in products:
        alert = "⚠" if p["alert_level"] > 0 and p["stock"] <= p["alert_level"] else ""
        prod_rows += f"""
        <tr {'class="alert-row"' if alert else ''}>
          <td>{p['code']}</td>
          <td>{p['name']}{' <span class="badge">要補充</span>' if alert else ''}</td>
          <td>{p['category'] or '未分類'}</td>
          <td class="num">{p['stock']} {p['unit']}</td>
          <td class="num">{p['alert_level']}</td>
          <td class="num">{yen(p['cost_price'])}</td>
          <td class="num">{yen(p['sell_price'])}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<style>
  @page {{ size: A4; margin: 15mm 12mm; }}
  body {{ font-family: 'Hiragino Kaku Gothic ProN','Yu Gothic',sans-serif; font-size:9pt; color:#222; }}
  h1 {{ font-size:16pt; color:#1a4a7a; border-bottom:2pt solid #1a4a7a; padding-bottom:4px; margin-bottom:4px; }}
  .meta {{ color:#666; font-size:8pt; margin-bottom:14px; }}
  h2 {{ font-size:11pt; color:#1a4a7a; margin:14px 0 4px; border-left:4px solid #1a4a7a; padding-left:6px; }}
  table {{ width:100%; border-collapse:collapse; font-size:8.5pt; page-break-inside:auto; }}
  thead tr {{ background:#1a4a7a; color:#fff; }}
  th {{ padding:4px 6px; text-align:left; }}
  td {{ padding:3px 6px; border-bottom:0.5pt solid #ddd; }}
  tr:nth-child(even) td {{ background:#f7f9fc; }}
  .num {{ text-align:right; }}
  .alert-row td {{ background:#fff0f0 !important; }}
  .badge {{ background:#e74c3c; color:#fff; font-size:7pt; padding:1px 4px; border-radius:3px; }}
  .summary-box {{ display:inline-block; background:#f0f5ff; border:1pt solid #c0d0f0;
                  border-radius:4px; padding:6px 14px; margin-right:10px; font-size:9pt; }}
  .summary-box .val {{ font-size:14pt; font-weight:bold; color:#1a4a7a; }}
</style>
</head>
<body>
  <h1>在庫レポート</h1>
  <div class="meta">作成日時：{generated}　／　商品数：{len(products)} 件</div>

  <div>
    <span class="summary-box">原価在庫評価額<br><span class="val">{yen(total_cost)}</span></span>
    <span class="summary-box">売価在庫評価額<br><span class="val">{yen(total_sell)}</span></span>
  </div>

  <h2>カテゴリ別集計</h2>
  <table>
    <thead>
      <tr>
        <th>カテゴリ</th><th>商品数</th><th>総在庫</th>
        <th>原価評価</th><th>売価評価</th><th>アラート</th><th>在庫切れ</th>
      </tr>
    </thead>
    <tbody>{cat_rows}</tbody>
  </table>

  <h2>商品別在庫一覧</h2>
  <table>
    <thead>
      <tr>
        <th>コード</th><th>商品名</th><th>カテゴリ</th>
        <th>在庫</th><th>アラート</th><th>原価</th><th>売価</th>
      </tr>
    </thead>
    <tbody>{prod_rows}</tbody>
  </table>
</body>
</html>"""
