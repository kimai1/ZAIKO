import json
import os
import sys
from pathlib import Path

from flask import (Flask, jsonify, redirect, render_template, request,
                   send_file, url_for, flash)
import io

sys.path.insert(0, str(Path(__file__).parent))

from core.database import (
    init_db, get_all_products, get_product, get_product_by_code,
    add_product, update_product, delete_product,
    stock_in, stock_out, stock_adjust,
    get_logs, get_categories, add_category, delete_category,
    get_summary, get_report_data,
    import_imaiya_entries, get_synced_imaiya_ids,
    restore_from_backup,
    get_suppliers, get_supplier, add_supplier, update_supplier, delete_supplier,
)
from export.csv_export import export_products_csv, export_logs_csv
from export.excel_export import export_products_excel, export_logs_excel
from export.pdf_export import export_report_pdf

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "zaiko-secret-2024-local")

# 起動時にDB初期化（失敗してもクラッシュさせずログに残す）
with app.app_context():
    try:
        init_db()
        print("[ZAIKO] DB初期化完了")
    except Exception as e:
        print(f"[ZAIKO] DB初期化失敗（DATABASE_URL確認が必要）: {e}")

@app.route("/health")
def health():
    return "ok", 200


@app.route("/debug/db")
def debug_db():
    import os
    from core.database import get_conn, _cur, DATABASE_URL
    result = {"DATABASE_URL": DATABASE_URL[:40] + "..." if DATABASE_URL else "未設定"}
    try:
        with get_conn() as conn:
            cur = _cur(conn)
            cur.execute("SELECT COUNT(*) AS cnt FROM products")
            result["products"] = cur.fetchone()["cnt"]
            cur.execute("SELECT COUNT(*) AS cnt FROM categories")
            result["categories"] = cur.fetchone()["cnt"]
            cur.execute("SELECT current_database(), version()")
            row = cur.fetchone()
            result["db_name"] = row[0]
            result["pg_version"] = row[1][:40]
        result["status"] = "ok"
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
    return jsonify(result)


# ─────────────────────────── Dashboard ───────────────────────────

@app.route("/")
def index():
    summary = get_summary()
    alerts = get_all_products(alert_only=True)
    recent_logs = get_logs(limit=10)
    # 商品が0件 = DB初期化済み（Render再起動後など）
    if summary["total_products"] == 0:
        flash("⚠️ 商品データがありません。バックアップから復元してください。", "warning")
    return render_template("index.html", summary=summary, alerts=alerts,
                           recent_logs=recent_logs)


# ─────────────────────────── Products ────────────────────────────

@app.route("/products")
def products():
    category = request.args.get("category", "")
    search = request.args.get("search", "")
    alert_only = request.args.get("alert_only") == "1"
    items = get_all_products(category=category or None,
                             alert_only=alert_only,
                             search=search or None)
    categories = get_categories()
    return render_template("products.html", products=items,
                           categories=categories, category=category,
                           search=search, alert_only=alert_only)


@app.route("/products/new", methods=["GET", "POST"])
def product_new():
    categories = get_categories()
    if request.method == "POST":
        f = request.form
        try:
            add_product(
                code=f["code"].strip(),
                name=f["name"].strip(),
                category=f.get("category", "").strip(),
                unit=f.get("unit", "個").strip(),
                alert_level=int(f.get("alert_level") or 0),
                cost_price=float(f.get("cost_price") or 0),
                sell_price=float(f.get("sell_price") or 0),
                memo=f.get("memo", "").strip(),
            )
            flash("商品を登録しました", "success")
            return redirect(url_for("products"))
        except Exception as e:
            flash(f"エラー: {e}", "danger")
    return render_template("product_form.html", product=None,
                           categories=categories, mode="new")


@app.route("/products/<int:pid>/edit", methods=["GET", "POST"])
def product_edit(pid):
    product = get_product(pid)
    categories = get_categories()
    if not product:
        flash("商品が見つかりません", "danger")
        return redirect(url_for("products"))
    if request.method == "POST":
        f = request.form
        try:
            update_product(pid,
                code=f["code"].strip(),
                name=f["name"].strip(),
                category=f.get("category", "").strip(),
                unit=f.get("unit", "個").strip(),
                alert_level=int(f.get("alert_level") or 0),
                cost_price=float(f.get("cost_price") or 0),
                sell_price=float(f.get("sell_price") or 0),
                memo=f.get("memo", "").strip(),
            )
            flash("商品情報を更新しました", "success")
            return redirect(url_for("products"))
        except Exception as e:
            flash(f"エラー: {e}", "danger")
    return render_template("product_form.html", product=product,
                           categories=categories, mode="edit")


@app.route("/products/<int:pid>/delete", methods=["POST"])
def product_delete(pid):
    try:
        delete_product(pid)
        flash("商品を削除しました", "success")
    except Exception as e:
        flash(f"エラー: {e}", "danger")
    return redirect(url_for("products"))


# ─────────────────────────── Stock Movement ──────────────────────

@app.route("/stock/in", methods=["GET", "POST"])
def stock_in_view():
    categories = get_categories()
    products = get_all_products()
    suppliers = get_suppliers()
    if request.method == "POST":
        f = request.form
        try:
            pid = int(f["product_id"])
            qty = int(f["quantity"])
            if qty <= 0:
                raise ValueError("数量は1以上を入力してください")
            sid = f.get("supplier_id")
            supplier_id = int(sid) if sid else None
            unit_price = float(f.get("unit_price") or 0) or None
            # 仕入れ先名をreasonに自動付与
            reason = f.get("reason", "")
            if supplier_id and not reason:
                s = get_supplier(supplier_id)
                if s:
                    reason = s["name"] + "より入庫"
            after = stock_in(pid, qty, reason=reason,
                             operator=f.get("operator", ""),
                             supplier_id=supplier_id,
                             unit_price=unit_price)
            p = get_product(pid)
            flash(f"入庫完了：{p['name']} → 在庫 {after}{p['unit']}", "success")
            return redirect(url_for("stock_in_view"))
        except Exception as e:
            flash(f"エラー: {e}", "danger")
    return render_template("stock_move.html", move_type="in",
                           products=products, categories=categories,
                           suppliers=suppliers)


@app.route("/stock/out", methods=["GET", "POST"])
def stock_out_view():
    categories = get_categories()
    products = get_all_products()
    if request.method == "POST":
        f = request.form
        try:
            pid = int(f["product_id"])
            qty = int(f["quantity"])
            if qty <= 0:
                raise ValueError("数量は1以上を入力してください")
            unit_price = float(f.get("unit_price") or 0) or None
            after = stock_out(pid, qty, reason=f.get("reason", ""),
                              operator=f.get("operator", ""),
                              unit_price=unit_price)
            p = get_product(pid)
            flash(f"出庫完了：{p['name']} → 在庫 {after}{p['unit']}", "success")
            return redirect(url_for("stock_out_view"))
        except Exception as e:
            flash(f"エラー: {e}", "danger")
    return render_template("stock_move.html", move_type="out",
                           products=products, categories=categories)


@app.route("/stock/adjust", methods=["GET", "POST"])
def stock_adjust_view():
    categories = get_categories()
    products = get_all_products()
    if request.method == "POST":
        f = request.form
        try:
            pid = int(f["product_id"])
            qty = int(f["quantity"])
            if qty < 0:
                raise ValueError("数量は0以上を入力してください")
            after = stock_adjust(pid, qty, reason=f.get("reason", "棚卸"),
                                 operator=f.get("operator", ""))
            p = get_product(pid)
            flash(f"棚卸調整完了：{p['name']} → 在庫 {after}{p['unit']}", "success")
            return redirect(url_for("stock_adjust_view"))
        except Exception as e:
            flash(f"エラー: {e}", "danger")
    return render_template("stock_move.html", move_type="adjust",
                           products=products, categories=categories)


# ─────────────────────────── History ─────────────────────────────

@app.route("/history")
def history():
    move_type = request.args.get("type", "")
    days = request.args.get("days", "")
    pid = request.args.get("product_id", "")
    logs = get_logs(
        product_id=int(pid) if pid else None,
        move_type=move_type or None,
        days=int(days) if days else None,
        limit=500,
    )
    products = get_all_products()
    return render_template("history.html", logs=logs, products=products,
                           move_type=move_type, days=days, product_id=pid)


# ─────────────────────────── Categories ──────────────────────────

@app.route("/categories", methods=["GET", "POST"])
def categories():
    if request.method == "POST":
        action = request.form.get("action")
        name = request.form.get("name", "").strip()
        if action == "add" and name:
            add_category(name)
            flash(f"カテゴリ「{name}」を追加しました", "success")
        elif action == "delete" and name:
            delete_category(name)
            flash(f"カテゴリ「{name}」を削除しました", "success")
        return redirect(url_for("categories"))
    cats = get_categories()
    return render_template("categories.html", categories=cats)


# ─────────────────────────── Export ──────────────────────────────

@app.route("/export/products/csv")
def export_prod_csv():
    products = get_all_products()
    data = export_products_csv(products)
    return send_file(io.BytesIO(data), mimetype="text/csv",
                     as_attachment=True, download_name="在庫一覧.csv")


@app.route("/export/products/excel")
def export_prod_excel():
    products = get_all_products()
    data = export_products_excel(products)
    return send_file(io.BytesIO(data),
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name="在庫一覧.xlsx")


@app.route("/export/logs/csv")
def export_logs_csv_view():
    logs = get_logs(limit=10000)
    data = export_logs_csv(logs)
    return send_file(io.BytesIO(data), mimetype="text/csv",
                     as_attachment=True, download_name="入出庫履歴.csv")


@app.route("/export/logs/excel")
def export_logs_excel_view():
    logs = get_logs(limit=10000)
    data = export_logs_excel(logs)
    return send_file(io.BytesIO(data),
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name="入出庫履歴.xlsx")


# ─────────────────────────── API (JSON) ──────────────────────────

@app.route("/api/backup")
def api_backup():
    from datetime import datetime as dt
    products = get_all_products()
    logs = get_logs(limit=100000)
    categories = get_categories()
    data = {
        "version": 2,
        "exportedAt": dt.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "company": "株式会社　今井屋",
        "products": products,
        "logs": logs,
        "categories": categories,
    }
    return app.response_class(
        response=json.dumps(data, ensure_ascii=False, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename=zaiko_backup_{dt.utcnow().strftime('%Y-%m-%d')}.json"},
    )


@app.route("/api/product/search")
def api_product_search():
    code = request.args.get("code", "")
    if code:
        p = get_product_by_code(code)
        if p:
            return jsonify(p)
        return jsonify({"error": "not found"}), 404
    q = request.args.get("q", "")
    items = get_all_products(search=q or None)
    return jsonify(items)


# ─────────────────────────── Import ──────────────────────────────

@app.route("/import", methods=["GET", "POST"])
def import_json():
    if request.method == "POST":
        f = request.files.get("file")
        if not f:
            flash("ファイルを選択してください", "danger")
            return redirect(url_for("import_json"))
        try:
            data = json.load(f)
        except Exception:
            flash("JSONの読み込みに失敗しました", "danger")
            return redirect(url_for("import_json"))

        products = data.get("products", [])
        if not products:
            flash("商品データが見つかりません", "warning")
            return redirect(url_for("import_json"))

        ok = skip = err = 0
        existing_categories = set(get_categories())

        for i, p in enumerate(products, 1):
            raw_sku = str(p.get("sku", "")).strip()
            # SKUが"—"等の場合は連番コードを生成
            if raw_sku in ("—", "-", "", "null", "None"):
                code = f"IMP-{i:04d}"
            else:
                code = raw_sku

            name = str(p.get("name", "")).strip()
            if not name:
                skip += 1
                continue

            category = str(p.get("category", "")).strip()
            unit = str(p.get("unit", "個")).strip() or "個"
            alert_level = int(p.get("lowStock") or 0)

            # カテゴリを自動登録
            if category and category not in existing_categories:
                add_category(category)
                existing_categories.add(category)

            # 重複コードはスキップ
            if get_product_by_code(code):
                skip += 1
                continue

            try:
                add_product(code=code, name=name, category=category,
                            unit=unit, alert_level=alert_level)
                ok += 1
            except Exception:
                err += 1

        flash(f"インポート完了：登録 {ok} 件 ／ スキップ {skip} 件 ／ エラー {err} 件", "success")
        return redirect(url_for("products"))

    return render_template("import.html")


@app.route("/restore", methods=["GET", "POST"])
def restore():
    if request.method == "POST":
        f = request.files.get("file")
        if not f:
            flash("ファイルを選択してください", "danger")
            return redirect(url_for("restore"))
        try:
            data = json.load(f)
        except Exception:
            flash("JSONの読み込みに失敗しました", "danger")
            return redirect(url_for("restore"))

        if data.get("version") != 2:
            flash("対応していない形式です（version 2 のみ対応）", "danger")
            return redirect(url_for("restore"))

        try:
            p_cnt, l_cnt, c_cnt = restore_from_backup(data)
            flash(f"✅ 完全復元完了：商品 {p_cnt} 件 ／ 入出庫ログ {l_cnt} 件 ／ カテゴリ {c_cnt} 件", "success")
        except Exception as e:
            flash(f"復元エラー: {e}", "danger")
        return redirect(url_for("products"))

    return render_template("restore.html")


# ─────────────────────────── Report ──────────────────────────────

@app.route("/report")
def report():
    data = get_report_data()
    return render_template("report.html", **data)


@app.route("/report/pdf")
def report_pdf():
    data = get_report_data()
    pdf_bytes = export_report_pdf(data)
    return send_file(io.BytesIO(pdf_bytes), mimetype="application/pdf",
                     as_attachment=True, download_name="在庫レポート.pdf")


# ─────────────────────────── 今井屋 同期 ─────────────────────────

@app.route("/sync/imaiya", methods=["GET", "POST"])
def sync_imaiya():
    products = get_all_products()
    if request.method == "POST":
        f = request.files.get("file")
        if not f:
            flash("ファイルを選択してください", "danger")
            return redirect(url_for("sync_imaiya"))
        try:
            data = json.load(f)
        except Exception:
            flash("JSONの読み込みに失敗しました", "danger")
            return redirect(url_for("sync_imaiya"))

        imaiya_products = {p["id"]: p for p in data.get("products", [])}
        imaiya_entries  = data.get("entries", [])

        if not imaiya_entries:
            flash("入出庫データ（entries）がありません。今井屋アプリで出庫を記録してからバックアップしてください。", "warning")
            return redirect(url_for("sync_imaiya"))

        # 商品名でマッピング（今井屋productId → ZAIKO product id）
        zaiko_by_name = {p["name"]: p["id"] for p in products}
        products_map = {}
        unmatched = []
        for iid, ip in imaiya_products.items():
            zid = zaiko_by_name.get(ip["name"])
            if zid:
                products_map[iid] = zid
            else:
                unmatched.append(ip["name"])

        ok, skip, err, msgs = import_imaiya_entries(imaiya_entries, products_map)

        if ok:
            flash(f"✅ 今井屋 → ZAIKO 同期完了：入庫 {ok} 件 ／ スキップ {skip} 件 ／ エラー {err} 件", "success")
        else:
            flash(f"新規入庫なし（スキップ {skip} 件・エラー {err} 件）", "warning")

        if unmatched:
            flash(f"商品名が一致しない商品: {', '.join(unmatched[:5])}", "warning")
        if msgs:
            flash("エラー詳細: " + " / ".join(msgs[:3]), "danger")

        return redirect(url_for("history"))

    # 出庫件数プレビュー用
    return render_template("sync_imaiya.html", products=products)


# ─────────────────────────── 仕入れ先管理 ────────────────────────

@app.route("/suppliers", methods=["GET", "POST"])
def suppliers_view():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            name = request.form.get("name", "").strip()
            if not name:
                flash("仕入れ先名を入力してください", "danger")
            else:
                add_supplier(
                    name=name,
                    contact=request.form.get("contact", "").strip(),
                    phone=request.form.get("phone", "").strip(),
                    email=request.form.get("email", "").strip(),
                    memo=request.form.get("memo", "").strip(),
                )
                flash(f"「{name}」を追加しました", "success")
        elif action == "delete":
            sid = request.form.get("supplier_id")
            if sid:
                s = get_supplier(int(sid))
                delete_supplier(int(sid))
                flash(f"「{s['name'] if s else sid}」を削除しました", "success")
        return redirect(url_for("suppliers_view"))
    return render_template("suppliers.html", suppliers=get_suppliers())


@app.route("/suppliers/<int:sid>/edit", methods=["GET", "POST"])
def supplier_edit(sid):
    s = get_supplier(sid)
    if not s:
        flash("仕入れ先が見つかりません", "danger")
        return redirect(url_for("suppliers_view"))
    if request.method == "POST":
        update_supplier(sid,
            name=request.form.get("name", "").strip(),
            contact=request.form.get("contact", "").strip(),
            phone=request.form.get("phone", "").strip(),
            email=request.form.get("email", "").strip(),
            memo=request.form.get("memo", "").strip(),
        )
        flash("仕入れ先情報を更新しました", "success")
        return redirect(url_for("suppliers_view"))
    return render_template("supplier_edit.html", supplier=s)


# ─────────────────────────── 一括入庫（今井屋受け取り）─────────────

@app.route("/receive", methods=["GET", "POST"])
def receive():
    products = get_all_products()
    categories = get_categories()
    suppliers = get_suppliers()

    if request.method == "POST":
        operator   = request.form.get("operator", "").strip()
        reason     = request.form.get("reason", "一括入庫").strip()
        sid        = request.form.get("supplier_id", "")
        supplier_id = int(sid) if sid else None
        # 仕入れ先名をreasonに自動付与
        if supplier_id and not reason:
            s = get_supplier(supplier_id)
            if s:
                reason = s["name"] + "より入庫"
        items  = []
        errors = []

        for key, val in request.form.items():
            if key.startswith("qty_") and val.strip():
                try:
                    pid = int(key[4:])
                    qty = int(val)
                    if qty > 0:
                        price_val = request.form.get(f"price_{pid}", "")
                        unit_price = float(price_val) if price_val.strip() else None
                        items.append((pid, qty, unit_price))
                except ValueError:
                    pass

        if not items:
            flash("数量を入力してください", "warning")
            return render_template("receive.html", products=products,
                                   categories=categories, suppliers=suppliers)

        ok = 0
        for pid, qty, unit_price in items:
            try:
                stock_in(pid, qty, reason=reason, operator=operator,
                         supplier_id=supplier_id, unit_price=unit_price)
                ok += 1
            except Exception as e:
                p = get_product(pid)
                errors.append(f"{p['name'] if p else pid}: {e}")

        if ok:
            flash(f"✅ {ok} 商品を入庫しました", "success")
        if errors:
            flash("エラー: " + " / ".join(errors), "danger")
        return redirect(url_for("history"))

    return render_template("receive.html", products=products,
                           categories=categories, suppliers=suppliers)


@app.route("/api/ocr/invoice", methods=["POST"])
def ocr_invoice():
    import anthropic, base64, re
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "ファイルが必要です"}), 400

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY が未設定です"}), 500

    file_bytes = f.read()
    media_type = f.content_type or "image/jpeg"
    b64 = base64.standard_b64encode(file_bytes).decode()

    products     = get_all_products()
    product_names = [p["name"] for p in products]

    prompt = f"""この伝票・納品書の画像から商品名と数量を読み取り、
以下の商品マスターと照合して一致するものだけをJSONで返してください。
商品マスター: {json.dumps(product_names, ensure_ascii=False)}

レスポンスはJSON配列のみ（説明不要）:
[{{"name":"商品名（マスターと完全一致）","qty":数量,"unit_price":単価またはnull}}]

マスターに存在しない商品は除外してください。"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=1024,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": prompt}
            ]}]
        )
        text = msg.content[0].text.strip()
        m = re.search(r'\[.*\]', text, re.DOTALL)
        items = json.loads(m.group() if m else text)
        return jsonify({"items": items})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5001)
