import os
import psycopg2
import psycopg2.pool
import psycopg2.extras
from contextlib import contextmanager
from datetime import datetime

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        url = DATABASE_URL
        if not url:
            raise RuntimeError("DATABASE_URL 環境変数が設定されていません")
        # Render/Heroku は postgres:// を使う場合がある
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        # Supabase は SSL 必須（未指定なら付与）
        if "sslmode" not in url:
            sep = "&" if "?" in url else "?"
            url = url + sep + "sslmode=require"
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 5, dsn=url)
    return _pool


@contextmanager
def get_conn():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def _cur(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    with get_conn() as conn:
        cur = _cur(conn)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id          SERIAL PRIMARY KEY,
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
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS stock_logs (
                id           SERIAL PRIMARY KEY,
                product_id   INTEGER NOT NULL REFERENCES products(id),
                type         TEXT NOT NULL CHECK(type IN ('in','out','adjust')),
                quantity     INTEGER NOT NULL,
                before_stock INTEGER NOT NULL,
                after_stock  INTEGER NOT NULL,
                reason       TEXT DEFAULT '',
                operator     TEXT DEFAULT '',
                logged_at    TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id   SERIAL PRIMARY KEY,
                name TEXT UNIQUE NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS imaiya_sync_log (
                imaiya_entry_id TEXT PRIMARY KEY,
                synced_at       TEXT NOT NULL,
                log_id          INTEGER
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS suppliers (
                id         SERIAL PRIMARY KEY,
                name       TEXT NOT NULL,
                contact    TEXT DEFAULT '',
                phone      TEXT DEFAULT '',
                email      TEXT DEFAULT '',
                memo       TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        # stock_logs に supplier_id カラムを追加（既存DB対応）
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='stock_logs' AND column_name='supplier_id'
                ) THEN
                    ALTER TABLE stock_logs ADD COLUMN supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL;
                END IF;
            END $$;
        """)
        # stock_logs に unit_price カラムを追加（既存DB対応）
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='stock_logs' AND column_name='unit_price'
                ) THEN
                    ALTER TABLE stock_logs ADD COLUMN unit_price REAL;
                END IF;
            END $$;
        """)


# ---------- helpers ----------

def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- products ----------

def get_all_products(category=None, alert_only=False, search=None):
    sql = "SELECT * FROM products WHERE 1=1"
    params = []
    if category:
        sql += " AND category=%s"
        params.append(category)
    if alert_only:
        sql += " AND alert_level > 0 AND stock <= alert_level"
    if search:
        sql += " AND (name LIKE %s OR code LIKE %s)"
        params += [f"%{search}%", f"%{search}%"]
    sql += " ORDER BY category, name"
    with get_conn() as conn:
        cur = _cur(conn)
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def get_product(product_id):
    with get_conn() as conn:
        cur = _cur(conn)
        cur.execute("SELECT * FROM products WHERE id=%s", (product_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_product_by_code(code):
    with get_conn() as conn:
        cur = _cur(conn)
        cur.execute("SELECT * FROM products WHERE code=%s", (code,))
        row = cur.fetchone()
        return dict(row) if row else None


def add_product(code, name, category="", unit="個", alert_level=0,
                cost_price=0, sell_price=0, memo=""):
    now = _now()
    with get_conn() as conn:
        cur = _cur(conn)
        cur.execute(
            """INSERT INTO products
               (code,name,category,unit,stock,alert_level,cost_price,sell_price,memo,created_at,updated_at)
               VALUES (%s,%s,%s,%s,0,%s,%s,%s,%s,%s,%s)""",
            (code, name, category, unit, alert_level, cost_price, sell_price, memo, now, now)
        )


def update_product(product_id, **fields):
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=%s" for k in fields)
    vals = list(fields.values()) + [product_id]
    with get_conn() as conn:
        cur = _cur(conn)
        cur.execute(f"UPDATE products SET {cols} WHERE id=%s", vals)


def delete_product(product_id):
    with get_conn() as conn:
        cur = _cur(conn)
        cur.execute("DELETE FROM stock_logs WHERE product_id=%s", (product_id,))
        cur.execute("DELETE FROM products WHERE id=%s", (product_id,))


# ---------- stock movement ----------

def _apply_movement(conn, product_id, move_type, quantity, reason, operator, supplier_id=None, unit_price=None):
    cur = _cur(conn)
    cur.execute("SELECT stock FROM products WHERE id=%s FOR UPDATE", (product_id,))
    row = cur.fetchone()
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
    cur.execute(
        "UPDATE products SET stock=%s, updated_at=%s WHERE id=%s",
        (after, _now(), product_id)
    )
    cur.execute(
        """INSERT INTO stock_logs
           (product_id,type,quantity,before_stock,after_stock,reason,operator,logged_at,supplier_id,unit_price)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (product_id, move_type,
         abs(quantity if move_type != "adjust" else after - before),
         before, after, reason, operator, _now(), supplier_id, unit_price)
    )
    return after


def stock_in(product_id, quantity, reason="", operator="", supplier_id=None, unit_price=None):
    with get_conn() as conn:
        return _apply_movement(conn, product_id, "in", quantity, reason, operator, supplier_id=supplier_id, unit_price=unit_price)


def stock_out(product_id, quantity, reason="", operator="", unit_price=None):
    with get_conn() as conn:
        return _apply_movement(conn, product_id, "out", quantity, reason, operator, unit_price=unit_price)




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
        sql += " AND l.product_id=%s"
        params.append(product_id)
    if move_type:
        sql += " AND l.type=%s"
        params.append(move_type)
    if days:
        sql += " AND l.logged_at::timestamp >= NOW() - (%s || ' days')::interval"
        params.append(str(days))
    sql += " ORDER BY l.logged_at DESC LIMIT %s"
    params.append(limit)
    with get_conn() as conn:
        cur = _cur(conn)
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


# ---------- categories ----------

def get_categories():
    with get_conn() as conn:
        cur = _cur(conn)
        cur.execute("SELECT name FROM categories ORDER BY name")
        return [r["name"] for r in cur.fetchall()]


def add_category(name):
    with get_conn() as conn:
        cur = _cur(conn)
        cur.execute(
            "INSERT INTO categories(name) VALUES(%s) ON CONFLICT DO NOTHING",
            (name,)
        )


def delete_category(name):
    with get_conn() as conn:
        cur = _cur(conn)
        cur.execute("DELETE FROM categories WHERE name=%s", (name,))


# ---------- suppliers ----------

def get_suppliers():
    with get_conn() as conn:
        cur = _cur(conn)
        cur.execute("SELECT * FROM suppliers ORDER BY name")
        return [dict(r) for r in cur.fetchall()]


def get_supplier(supplier_id):
    with get_conn() as conn:
        cur = _cur(conn)
        cur.execute("SELECT * FROM suppliers WHERE id=%s", (supplier_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def add_supplier(name, contact="", phone="", email="", memo=""):
    now = _now()
    with get_conn() as conn:
        cur = _cur(conn)
        cur.execute(
            """INSERT INTO suppliers(name,contact,phone,email,memo,created_at,updated_at)
               VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (name, contact, phone, email, memo, now, now)
        )
        return cur.fetchone()["id"]


def update_supplier(supplier_id, **fields):
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=%s" for k in fields)
    vals = list(fields.values()) + [supplier_id]
    with get_conn() as conn:
        cur = _cur(conn)
        cur.execute(f"UPDATE suppliers SET {cols} WHERE id=%s", vals)


def delete_supplier(supplier_id):
    with get_conn() as conn:
        cur = _cur(conn)
        cur.execute("DELETE FROM suppliers WHERE id=%s", (supplier_id,))


# ---------- imaiya sync ----------

def get_synced_imaiya_ids():
    with get_conn() as conn:
        cur = _cur(conn)
        cur.execute("SELECT imaiya_entry_id FROM imaiya_sync_log")
        return {r["imaiya_entry_id"] for r in cur.fetchall()}


def import_imaiya_entries(entries, products_map):
    synced = get_synced_imaiya_ids()
    ok = skip = err = 0
    messages = []

    for entry in entries:
        eid = entry.get("id", "")
        if not eid or eid in synced or entry.get("type") != "out":
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

        note  = entry.get("note") or ""
        date  = entry.get("date") or ""
        staff = entry.get("staff") or ""
        reason = f"今井屋出荷 {date}" + (f" ({note})" if note else "")

        try:
            with get_conn() as conn:
                cur = _cur(conn)
                cur.execute(
                    "SELECT stock FROM products WHERE id=%s FOR UPDATE", (zaiko_pid,)
                )
                row = cur.fetchone()
                if not row:
                    err += 1
                    continue
                before = row["stock"]
                after  = before + qty
                cur.execute(
                    "UPDATE products SET stock=%s, updated_at=%s WHERE id=%s",
                    (after, _now(), zaiko_pid)
                )
                cur.execute(
                    """INSERT INTO stock_logs
                       (product_id,type,quantity,before_stock,after_stock,reason,operator,logged_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (zaiko_pid, "in", qty, before, after, reason,
                     staff or "今井屋", date + " 00:00:00")
                )
                log_id = cur.fetchone()["id"]
                cur.execute(
                    """INSERT INTO imaiya_sync_log(imaiya_entry_id,synced_at,log_id)
                       VALUES(%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (eid, _now(), log_id)
                )
                ok += 1
        except Exception as e:
            messages.append(str(e))
            err += 1

    return ok, skip, err, messages


# ---------- restore ----------

def restore_from_backup(data):
    products   = data.get("products", [])
    logs       = data.get("logs", [])
    categories = data.get("categories", [])

    with get_conn() as conn:
        cur = _cur(conn)
        cur.execute("DELETE FROM imaiya_sync_log")
        cur.execute("DELETE FROM stock_logs")
        cur.execute("DELETE FROM products")
        cur.execute("DELETE FROM categories")

        for cat in categories:
            cur.execute(
                "INSERT INTO categories(name) VALUES(%s) ON CONFLICT DO NOTHING",
                (cat,)
            )

        now = _now()
        for p in products:
            cur.execute("""
                INSERT INTO products
                  (id,code,name,category,unit,stock,alert_level,
                   cost_price,sell_price,memo,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                p.get("id"), p.get("code", ""), p.get("name", ""),
                p.get("category", ""), p.get("unit", "個"),
                int(p.get("stock", 0)), int(p.get("alert_level", 0)),
                float(p.get("cost_price", 0)), float(p.get("sell_price", 0)),
                p.get("memo", ""),
                p.get("created_at", now), p.get("updated_at", now),
            ))

        # SERIAL シーケンスをリセット
        cur.execute("""
            SELECT setval(pg_get_serial_sequence('products','id'),
                          COALESCE(MAX(id), 1)) FROM products
        """)
        cur.execute("""
            SELECT setval(pg_get_serial_sequence('categories','id'),
                          COALESCE(MAX(id), 1)) FROM categories
        """)

        for log in logs:
            cur.execute("""
                INSERT INTO stock_logs
                  (id,product_id,type,quantity,before_stock,after_stock,
                   reason,operator,logged_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                log.get("id"), log.get("product_id"),
                log.get("type", "adjust"),
                int(log.get("quantity", 0)),
                int(log.get("before_stock", 0)),
                int(log.get("after_stock", 0)),
                log.get("reason", ""), log.get("operator", ""),
                log.get("logged_at", now),
            ))

        if logs:
            cur.execute("""
                SELECT setval(pg_get_serial_sequence('stock_logs','id'),
                              COALESCE(MAX(id), 1)) FROM stock_logs
            """)

    return len(products), len(logs), len(categories)


# ---------- report ----------

def get_report_data():
    with get_conn() as conn:
        cur = _cur(conn)

        cur.execute("""
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
        """)
        cat_rows = cur.fetchall()

        cur.execute("SELECT * FROM products ORDER BY category, name")
        all_prods = cur.fetchall()

        cur.execute("""
            SELECT type, COUNT(*) AS ops, SUM(quantity) AS qty
            FROM stock_logs
            WHERE logged_at::timestamp >= NOW() - INTERVAL '30 days'
            GROUP BY type
        """)
        movement = cur.fetchall()

        cur.execute("""
            SELECT
                p.id, p.name, p.category, p.unit, p.stock,
                SUM(l.quantity)  AS total_out,
                COUNT(l.id)      AS out_count,
                MAX(l.logged_at) AS last_out
            FROM stock_logs l
            JOIN products p ON l.product_id = p.id
            WHERE l.type = 'out'
            GROUP BY p.id, p.name, p.category, p.unit, p.stock
            ORDER BY total_out DESC
            LIMIT 20
        """)
        top_sellers = cur.fetchall()

        cur.execute("""
            SELECT
                p.id, p.name, p.category, p.unit, p.stock,
                p.cost_price, p.sell_price,
                MAX(l.logged_at) AS last_out_at,
                EXTRACT(epoch FROM (
                    NOW() - COALESCE(MAX(l.logged_at), p.created_at)::timestamp
                ))::integer / 86400 AS days_since
            FROM products p
            LEFT JOIN stock_logs l ON l.product_id = p.id AND l.type = 'out'
            WHERE p.stock > 0
            GROUP BY p.id, p.name, p.category, p.unit, p.stock,
                     p.cost_price, p.sell_price, p.created_at
            HAVING
                EXTRACT(epoch FROM (
                    NOW() - COALESCE(MAX(l.logged_at), p.created_at)::timestamp
                ))::integer / 86400 >= 90
                OR MAX(l.logged_at) IS NULL
            ORDER BY days_since DESC NULLS LAST, p.stock DESC
        """)
        dead_stock = cur.fetchall()

    categories  = [dict(r) for r in cat_rows]
    products    = [dict(r) for r in all_prods]
    mv_map      = {r["type"]: dict(r) for r in movement}
    top_sellers = [dict(r) for r in top_sellers]
    dead_stock  = [dict(r) for r in dead_stock]

    total_cost = sum(float(c["total_cost"] or 0) for c in categories)
    total_sell = sum(float(c["total_sell"] or 0) for c in categories)
    max_out    = top_sellers[0]["total_out"] if top_sellers else 1

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
        cur = _cur(conn)
        cur.execute("SELECT COUNT(*) AS cnt FROM products")
        total = cur.fetchone()["cnt"]
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM products WHERE alert_level>0 AND stock<=alert_level"
        )
        alert = cur.fetchone()["cnt"]
        cur.execute("SELECT COUNT(*) AS cnt FROM products WHERE stock=0")
        zero = cur.fetchone()["cnt"]
        cur.execute(
            "SELECT COALESCE(SUM(quantity),0) AS s FROM stock_logs "
            "WHERE type='in' AND logged_at::date = CURRENT_DATE"
        )
        today_in = cur.fetchone()["s"]
        cur.execute(
            "SELECT COALESCE(SUM(quantity),0) AS s FROM stock_logs "
            "WHERE type='out' AND logged_at::date = CURRENT_DATE"
        )
        today_out = cur.fetchone()["s"]
    return {
        "total_products": total,
        "alert_count": alert,
        "zero_stock": zero,
        "today_in": today_in,
        "today_out": today_out,
    }
