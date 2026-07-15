#!/bin/bash
# ZAIKO 在庫管理システム 起動スクリプト
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "仮想環境を作成中..."
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install -q -r requirements.txt

echo ""
echo "==============================="
echo "  ZAIKO 在庫管理システム"
echo "  http://localhost:5001"
echo "==============================="
echo ""

python app.py
