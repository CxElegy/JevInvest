#!/usr/bin/env bash
# 「AI 投資 Skills 大全」の 16 個を vendor/skills/ に入れる（vendor/ は .gitignore 済み）。
# 中身の評価と使い分けは docs/TOOLS.md。クラウドのコンテナは使い捨てなので、新しいセッションでは再実行する。
#   bash tools/install_skills.sh          # 16 個を浅く取得し、Python で使う 2 個（OpenBB・backtrader）を pip で入れる
#   bash tools/install_skills.sh --all-pip # 他の Python 製（qlib・vnpy など）も pip で試す（重い・失敗しうる）
set -u
cd "$(dirname "$0")/.."
DEST=vendor/skills
mkdir -p "$DEST"
REPOS="
OpenBB-finance/OpenBB
ZhuLinsen/daily_stock_analysis
virattt/ai-hedge-fund
microsoft/qlib
vnpy/vnpy
Fincept-Corporation/FinceptTerminal
mementum/backtrader
QuantConnect/Lean
HKUDS/Vibe-Trading
AI4Finance-Foundation/FinGPT
NoFxAiOS/nofx
AI4Finance-Foundation/FinRL
TauricResearch/TradingAgents
microsoft/RD-Agent
AI4Finance-Foundation/FinRobot
ArvinLovegood/go-stock
"
for r in $REPOS; do
  name=${r#*/}
  if [ -d "$DEST/$name/.git" ]; then echo "skip  $r (取得済み)"; continue; fi
  if git clone -q --depth 1 --filter=blob:limit=5m "https://github.com/$r" "$DEST/$name" 2>/dev/null; then
    echo "ok    $r"
  else
    echo "FAIL  $r"
  fi
done
# このプロジェクトで実際に使うのは 2 つ（理由は docs/TOOLS.md）
# システムの Python と衝突しないよう vendor/.venv に入れる（既存の pandas などはそのまま使う）
[ -x vendor/.venv/bin/python ] || python3 -m venv --system-site-packages vendor/.venv
PIP=vendor/.venv/bin/pip
$PIP install -q "backtrader>=1.9.78" openbb-core openbb-yfinance openbb-fred openbb-equity openbb-etf openbb-index openbb-economy && echo "ok    pip: backtrader, openbb (vendor/.venv)" || echo "FAIL  pip: backtrader/openbb"
if [ "${1:-}" = "--all-pip" ]; then
  for p in pyqlib vnpy finrl rdagent; do $PIP install -q "$p" && echo "ok    pip: $p" || echo "FAIL  pip: $p"; done
fi
