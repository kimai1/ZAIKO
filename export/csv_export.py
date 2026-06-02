import csv
import io
from datetime import datetime


def export_products_csv(products):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["商品コード", "商品名", "カテゴリ", "単位", "在庫数",
                     "アラート在庫数", "原価", "売価", "メモ"])
    for p in products:
        writer.writerow([
            p["code"], p["name"], p["category"], p["unit"], p["stock"],
            p["alert_level"], p["cost_price"], p["sell_price"], p["memo"]
        ])
    return output.getvalue().encode("utf-8-sig")


def export_logs_csv(logs):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["日時", "商品コード", "商品名", "区分", "数量",
                     "変動前在庫", "変動後在庫", "理由", "担当者"])
    type_label = {"in": "入庫", "out": "出庫", "adjust": "棚卸調整"}
    for l in logs:
        writer.writerow([
            l["logged_at"], l["product_code"], l["product_name"],
            type_label.get(l["type"], l["type"]),
            l["quantity"], l["before_stock"], l["after_stock"],
            l["reason"], l["operator"]
        ])
    return output.getvalue().encode("utf-8-sig")
