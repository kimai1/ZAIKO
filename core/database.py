import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# ローカル: ~/.zaiko/zaiko.db  /  Render: /data/zaiko.db (DATA_DIR=/data)
_data_dir = os.environ.get("DATA_DIR", str(Path.home() / ".zaiko"))
APP_DIR = Path(_data_dir)
APP_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = APP_DIR / "zaiko.db"


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS products (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                code        TEXT UNIQUE NOT NULL,
                name        TEXT NOT NULL,
                category    TEXT DEFAULT '',
                unit        TEXT DEFAULT '個',
                stock       INTEGER DEFAULT 0,
                alert_level INTEGER DEFAULT 0,
                cost_price  REAL DEFAULT 0,
                sell_price  REAL DEFAULT 0,
                memo        TEXT DEFAULT '',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS stock_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id  INTEGER NOT NULL REFERENCES products(id),
                type        TEXT NOT NULL CHECK(type IN ('in','out','adjust')),
                quantity    INTEGER NOT NULL,
                before_stock INTEGER NOT NULL,
                after_stock  INTEGER NOT NULL,
                reason      TEXT DEFAULT '',
                operator    TEXT DEFAULT '',
                logged_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS categories (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            );
        """)


# ---------- product helpers ----------

def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_all_products(category=None, alert_only=False, search=None):
    sql = "SELECT * FROM products WHERE 1=1"
    params = []
    if category:
        sql += " AND category=?"
        params.append(category)
    if alert_only:
        sql += " AND alert_level > 0 AND stock <= alert_level"
    if search:
        sql += " AND (name LIKE ? OR code LIKE ?)"
        params += [f"%{search}%", f"%{search}%"]
    sql += " ORDER BY category, name"
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_product(product_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        return dict(row) if row else None


def get_product_by_code(code):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM products WHERE code=?", (code,)).fetchone()
        return dict(row) if row else None


def add_product(code, name, category="", unit="個", alert_level=0,
                cost_price=0, sell_price=0, memo=""):
    now = _now()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO products
               (code,name,category,unit,stock,alert_level,cost_price,sell_price,memo,created_at,updated_at)
               VALUES (?,?,?,?,0,?,?,?,?,?,?)""",
            (code, name, category, unit, alert_level, cost_price, sell_price, memo, now, now)
        )


def update_product(product_id, **fields):
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [product_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE products SET {cols} WHERE id=?", vals)


def delete_product(product_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM stock_logs WHERE product_id=?", (product_id,))
        conn.execute("DELETE FROM products WHERE id=?", (product_id,))


# ---------- stock movement ----------

def _apply_movement(conn, product_id, move_type, quantity, reason, operator):
    row = conn.execute("SELECT stock FROM products WHERE id=?", (product_id,)).fetchone()
    if not row:
        raise ValueError("商品が見つかりません")
    before = row["stock"]
    if move_type == "in":
        after = before + quantity
    elif move_type == "out":
        after = before - quantity
        if after < 0:
            raise ValueError("在庫不足です")
    else:  # adjust
        after = quantity
    conn.execute(
        "UPDATE products SET stock=?, updated_at=? WHERE id=?",
        (after, _now(), product_id)
    )
    conn.execute(
        """INSERT INTO stock_logs
           (product_id,type,quantity,before_stock,after_stock,reason,operator,logged_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (product_id, move_type, abs(quantity if move_type != "adjust" else after - before),
         before, after, reason, operator, _now())
    )
    return after


def stock_in(product_id, quantity, reason="", operator=""):
    with get_conn() as conn:
        return _apply_movement(conn, product_id, "in", quantity, reason, operator)


def stock_out(product_id, quantity, reason="", operator=""):
    with get_conn() as conn:
        return _apply_movement(conn, product_id, "out", quantity, reason, operator)


def stock_adjust(product_id, new_quantity, reason="棚卸", operator=""):
    with get_conn() as conn:
        return _apply_movement(conn, product_id, "adjust", new_quantity, reason, operator)


# ---------- logs ----------

def get_logs(product_id=None, move_type=None, days=None, limit=200):
    sql = """
        SELECT l.*, p.name AS product_name, p.code AS product_code, p.unit
        FROM stock_logs l
        JOIN products p ON l.product_id = p.id
        WHERE 1=1
    """
    params = []
    if product_id:
        sql += " AND l.product_id=?"
        params.append(product_id)
    if move_type:
        sql += " AND l.type=?"
        params.append(move_type)
    if days:
        sql += " AND l.logged_at >= datetime('now',?)"
        params.append(f"-{days} days")
    sql += " ORDER BY l.logged_at DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ---------- categories ----------

def get_categories():
    with get_conn() as conn:
        return [r["name"] for r in conn.execute("SELECT name FROM categories ORDER BY name").fetchall()]


def add_category(name):
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO categories(name) VALUES(?)", (name,))


def delete_category(name):
    with get_conn() as conn:
        conn.execute("DELETE FROM categories WHERE name=?", (name,))


# ---------- imaiya sync ----------

def get_synced_imaiya_ids():
    """今井屋から取り込み済みのentry idセットを返す"""
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS imaiya_sync_log (
                imaiya_entry_id TEXT PRIMARY KEY,
                synced_at       TEXT NOT NULL,
                log_id          INTEGER
            )
        """)
        rows = conn.execute("SELECT imaiya_entry_id FROM imaiya_sync_log").fetchall()
        return {r[0] for r in rows}


def import_imaiya_entries(entries, products_map):
    """
    今井屋の inv_entries (type='out') を ZAIKO の stock_in として取り込む。
    products_map: {imaiya_product_id: zaiko_product_id}
    returns: (ok, skip, err, messages)
    """
    synced = get_synced_imaiya_ids()
    ok = skip = err = 0
    messages = []

    for entry in entries:
        eid = entry.get("id", "")
        if not eid:
            skip += 1
            continue

        # 取り込み済みはスキップ
        if eid in synced:
            skip += 1
            continue

        # 出庫のみ（今井屋が出荷 = 藤川が受け取る）
        if entry.get("type") != "out":
            skip += 1
            continue

        product_id_imaiya = entry.get("productId", "")
        zaiko_pid = products_map.get(product_id_imaiya)
        if not zaiko_pid:
            messages.append(f"商品未対応: {product_id_imaiya}")
            err += 1
            continue

        qty = int(entry.get("qty") or 0)
        if qty <= 0:
            skip += 1
            continue

        note = entry.get("note") or ""
        date = entry.get("date") or ""
        staff = entry.get("staff") or ""
        reason = f"今井屋出荷 {date}" + (f" ({note})" if note else "")

        try:
            with get_conn() as conn:
                row = conn.execute("SELECT stock FROM products WHERE id=?", (zaiko_pid,)).fetchone()
                if not row:
                    err += 1
                    continue
                before = row["stock"]
                after = before + qty
                conn.execute("UPDATE products SET stock=?, updated_at=? WHERE id=?",
                             (after, _now(), zaiko_pid))
                conn.execute(
                    """INSERT INTO stock_logs
                       (product_id,type,quantity,before_stock,after_stock,reason,operator,logged_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (zaiko_pid, "in", qty, before, after, reason, staff or "今井屋", date + " 00:00:00")
                )
                log_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    "INSERT OR IGNORE INTO imaiya_sync_log(imaiya_entry_id,synced_at,log_id) VALUES(?,?,?)",
                    (eid, _now(), log_id)
                )
                ok += 1
        except Exception as e:
            messages.append(str(e))
            err += 1

    return ok, skip, err, messages


# ---------- report ----------

def get_report_data():
    with get_conn() as conn:
        # カテゴリ別集計
        cat_rows = conn.execute("""
            SELECT
                COALESCE(NULLIF(category,''), '未分類') AS cat,
                COUNT(*) AS cnt,
                SUM(stock) AS total_stock,
                SUM(stock * cost_price) AS total_cost,
                SUM(stock * sell_price) AS total_sell,
                SUM(CASE WHEN alert_level>0 AND stock<=alert_level THEN 1 ELSE 0 END) AS alert_cnt,
                SUM(CASE WHEN stock=0 THEN 1 ELSE 0 END) AS zero_cnt
            FROM products
            GROUP BY cat
            ORDER BY cat
        """).fetchall()

        # 全商品（カテゴリ順）
        all_prods = conn.execute(
            "SELECT * FROM products ORDER BY category, name"
        ).fetchall()

        # 直近30日の入出庫サマリ
        movement = conn.execute("""
            SELECT
                type,
                COUNT(*) AS ops,
                SUM(quantity) AS qty
            FROM stock_logs
            WHERE logged_at >= datetime('now','-30 days')
            GROUP BY type
        """).fetchall()

        # よく売れた商品 TOP20（全期間の出庫合計）
        top_sellers = conn.execute("""
            SELECT
                p.id, p.name, p.category, p.unit, p.stock,
                SUM(l.quantity)  AS total_out,
                COUNT(l.id)      AS out_count,
                MAX(l.logged_at) AS last_out
            FROM stock_logs l
            JOIN products p ON l.product_id = p.id
            WHERE l.type = 'out'
            GROUP BY l.product_id
            ORDER BY total_out DESC
            LIMIT 20
        """).fetchall()

        # 不良在庫：在庫あり & 90日以上出庫なし（または一度も出庫なし）
        dead_stock = conn.execute("""
            SELECT
                p.id, p.name, p.category, p.unit, p.stock,
                p.cost_price, p.sell_price,
                MAX(l.logged_at) AS last_out_at,
                CAST(julianday('now') - julianday(COALESCE(MAX(l.logged_at), p.created_at)) AS INTEGER) AS days_since
            FROM products p
            LEFT JOIN stock_logs l ON l.product_id = p.id AND l.type = 'out'
            WHERE p.stock > 0
            GROUP BY p.id
            HAVING days_since >= 90 OR last_out_at IS NULL
            ORDER BY days_since DESC, p.stock DESC
        """).fetchall()

    categories  = [dict(r) for r in cat_rows]
    products    = [dict(r) for r in all_prods]
    mv_map      = {r["type"]: dict(r) for r in movement}
    top_sellers = [dict(r) for r in top_sellers]
    dead_stock  = [dict(r) for r in dead_stock]

    total_cost = sum(c["total_cost"] or 0 for c in categories)
    total_sell = sum(c["total_sell"] or 0 for c in categories)

    # TOP売上の最大値（棒グラフ幅計算用）
    max_out = top_sellers[0]["total_out"] if top_sellers else 1

    return {
        "categories": categories,
        "products": products,
        "movement": mv_map,
        "top_sellers": top_sellers,
        "dead_stock": dead_stock,
        "max_out": max_out,
        "total_cost": total_cost,
        "total_sell": total_sell,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# ---------- summary ----------

def get_summary():
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        alert = conn.execute(
            "SELECT COUNT(*) FROM products WHERE alert_level>0 AND stock<=alert_level"
        ).fetchone()[0]
        zero = conn.execute("SELECT COUNT(*) FROM products WHERE stock=0").fetchone()[0]
        today_in = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM stock_logs WHERE type='in' AND date(logged_at)=date('now')"
        ).fetchone()[0]
        today_out = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM stock_logs WHERE type='out' AND date(logged_at)=date('now')"
        ).fetchone()[0]
    return {
        "total_products": total,
        "alert_count": alert,
        "zero_stock": zero,
        "today_in": today_in,
        "today_out": today_out,
    }
