# AI 投資ツール 16 個の評価（2026-10-01 取得版）

各ツールの構成・仕組み・動かした結果の詳細は [INGEST.md](INGEST.md)。

`bash tools/install_skills.sh` で `vendor/skills/` に取得したソースと README を読んだ結果。
基準は CLAUDE.md の「税・手数料を引いた後の、長期のリスク調整後リターン」と、独立性・実発注しない方針。

## 結論

| 判定 | ツール | 使い道 |
|---|---|---|
| **使う** | backtrader | 積立・配分・リバランスのルールを過去データで検証する。売買ルールの「本当に効くか」を買う前に確かめる |
| **使う** | OpenBB（Open Data Platform） | 株価・指数・経済指標の取得を 1 か所にまとめる。Yahoo が止まったときの取得先の切り替え |
| 参考に読む | FinRobot | 「数字はコード、文章は LLM」という設計。このプロジェクトの `src/analytics.py` と Claude の分担と同じ考え方 |
| 参考に読む | TradingAgents / ai-hedge-fund | 任意で Jev を使う経路がある（`TYPESAFE_API_KEY`）。`src/jev.py` を接続するときの実装例として読む。売買判断には使わない |
| 使わない | nofx | 暗号資産の自動売買。AI 用の USDC ウォレットと取引所への入金が前提。実発注しない方針と合わない |
| 使わない | Vibe-Trading、vnpy、Lean | 自動売買・先物や中国株の執行が中心 |
| 使わない | qlib、RD-Agent、FinRL、FinGPT | 機械学習で銘柄の短期の値動きを予測する研究用。予測の優位が手数料・税の後に残る根拠は弱く、学習には大きな計算資源が要る |
| 使わない | daily_stock_analysis、go-stock | 中国株中心の日次の「買い時・売り時」通知。売買を増やす方向に働く |
| 使わない | FinceptTerminal | デスクトップ用の C++ アプリ（AGPL、有料版あり）。タブレットとクラウドでは動かない |

## 1 つずつ

| ツール | 中身 | 言語・ライセンス | 必要なもの |
|---|---|---|---|
| OpenBB | 多数のデータ源をまとめる取得ライブラリと API サーバー（`pip install openbb`） | Python／Apache-2.0 | 無料源なら鍵なし。有料源は各社の鍵 |
| daily_stock_analysis | A 株・港股・米股などの自選株を毎日 AI 分析して WeChat などに通知 | Python・TS／MIT | LLM の鍵、ニュース検索の鍵 |
| ai-hedge-fund | 著名投資家を模した複数の AI が売買判断を出す教育用（発注はしない） | Python／MIT | Financial Datasets の鍵＋LLM の鍵（Jev も可） |
| qlib | Microsoft の定量投資の研究基盤。因子と予測モデル | Python／MIT | 大量の価格データと学習時間 |
| vnpy | 中国の先物・株の自動売買フレームワーク | Python／MIT | 証券・先物会社の接続 |
| FinceptTerminal | Qt6 のデスクトップ端末 | C++・Python／AGPL-3.0 | デスクトップ PC |
| backtrader | イベント駆動のバックテスト | Python／GPL-3.0 | 価格データだけ |
| Lean | QuantConnect の売買エンジン（C#） | C#／Apache-2.0 | .NET か Docker |
| Vibe-Trading | 対話で戦略を作ってバックテスト・実売買する AI | Python・TS／MIT | LLM の鍵、ブローカー接続 |
| FinGPT | 金融向けに調整した LLM（感情分析・予測） | Python／MIT | GPU、Hugging Face |
| nofx | LLM が戦略を担う暗号資産の自動売買端末 | Go・TS／AGPL-3.0 | USDC と取引所口座 |
| FinRL | 強化学習の売買（教育用。後継は FinRL-X） | Python／MIT | GPU と学習時間 |
| TradingAgents | アナリスト・リサーチャー・トレーダー役の AI が議論して売買判断 | Python／Apache-2.0 | LLM の鍵、Alpha Vantage など |
| RD-Agent | 因子とモデルを自動で研究する AI（qlib 上） | Python／MIT | LLM の鍵、Docker |
| FinRobot | 銘柄調査レポートを作る AI。数値計算はコード、文章は LLM | Python・TS／Apache-2.0 | OpenAI・FMP の鍵 |
| go-stock | 中国株向けのデスクトップ分析アプリ | Go／GPL-3.0 | Windows デスクトップ |

## なぜ 2 つだけか

16 個の大半は「どの銘柄が上がるかを当てる」か「自動で売買する」ためのもの。
今の持ち方（未保有、毎月の黒字を積み立てる段階）で手取りを一番増やすのは、
NISA の枠を使い切ること・手数料の安い分散投資・売買を減らすことで、予測の精度ではない。
予測と自動売買は、手数料・税・独立性の確認の手間を差し引くと、手取りを減らす方向に働きやすい。

backtrader はこの「売買を減らす・積立を続ける」が本当に効くかを数字で確かめるため、
OpenBB はその検証と日々の確認に使うデータを安定して取るために使う。
