# JevInvest — 持仓分析・管理ツール

計算はローカルの Python、判断は Claude Code。詳細は README.md と docs/PLAN.md。

## 基本の流れ

- `python -m src.cli validate` 台帳の検証 / `quotes` 価格確認 / `report` レポート生成
- `python -m src.cli plan` 積立計画 / `compliance` 独立性の確認状況 / `transactions` 取引履歴と XIRR
- `python -m src.cli taxplan --salary N` iDeCo の節税額・ふるさと納税の上限（概算）。年収は本人の手元で入れてもらう
- `python -m src.cli check-trade buy SYMBOL --pct 3` 売買前の点検（やめる理由を先に出す）
- `python -m src.cli nextbuy` 今月の積立の振り分け（売らずに目標へ寄せる。`--pct-only` で比率だけ）
- `python -m src.cli harvest --realized N` 年末の損出し・益出しの点検（課税口座。損出しの多くは繰り延べ）
- `python -m src.cli checkup` 目標配分の健康診断（下落・外貨・NISA 枠。シミュレーターの「我的计划体检」と同じ）
- `python -m src.cli stress` 過去の急落・円高を今の配分に当てはめる（値動きと為替に分けて出す）
- `python tools/papertrade.py run` ペーパートレード（仮想 500 万円・実データ）を進める。結果は `paper/REPORT.md`
- `python -m src.cli import-csv FILE.csv` 証券会社の取引履歴 CSV を取り込む（SBI・楽天。金額は画面に出さない）
- 全体マップ: `python tools/build_project_map.py <出力先>`（全プログラム・データの流れ・自動処理を 1 枚に。新しいファイルは META に分類を足す）
- 出力した `reports/latest.md` を読んで、集中度・相関・目標配分・為替・税制の観点で判断する
- スラッシュコマンド: `/review` `/add-trade` `/import-csv` `/before-trade` `/harvest` `/independence-mail` `/monthly-check` `/plan` `/compliance` `/taxplan` `/influencers` `/influencers-watch`（定期実行用）
- 金額を Claude に渡したくないとき: `web/ledger.html`（ブラウザ内だけに保存）で本人が入力し、
  比率だけの要約を貼ってもらう。金額を聞き出さない。

- 注目アカウント（X の Jukan・Serenity など）: `python tools/influencers.py fetch` → `reports/influencers.md`、
  `/influencers` でその人の視点の分析。投稿は取り込んだものだけを根拠にし、本人の意見を作らない。
  取得元（`X_BEARER_TOKEN` / `TWEETS_RSS_URL`）が無ければ投稿を貼ってもらう。対象は `config/influencers.yaml`。
- 著名投資家の 13F データ更新: `tools/fetch_13f.py` → `tools/export_13f_json.py` → `web/investors.json`。
  SEC の連絡先は環境変数 `SEC_USER_AGENT` で渡し、コードやコミットに書かない。

## 判断の基準（ユーザーの指示）

- **依頼をそのまま作るのではなく、ユーザーの手取りのリターンが上がるかを常に先に考える。**
  効かない・逆効果だと思えば、理由と代案を添えて先に言う。
- 「リターン」は、税・手数料を引いた後の、長期のリスク調整後リターン。一時的な高利回りを狙って
  大きな損失の可能性を増やす提案はしない（大きく負けると取り戻すのに何倍も時間がかかる）。
- 根拠の強い順に効く: ①NISA などの税制と手数料 ②資産配分と分散 ③売買を減らすこと（行動の誤り）
  ④銘柄選び。13F の後追いや短期売買の予測は、①〜③より根拠が弱いと伝えたうえで扱う。
- 下の「守ること」（独立性・実発注しない など）はこの方針でも変えない。

## 守ること

- **個人の金額データはコミットしない。** `config/holdings.yaml` `config/compliance.yaml`
  `config/transactions.yaml` `reports/` `data/` は .gitignore 済み。`git add -A` の前に確認する。
- **独立性（independence）が最優先。** 購入可と記録されていない銘柄について配分を勧めない。
  このツールは制限対象かどうかを判定しない。判定の正は DESC と Independence & Conflicts Network。
  記録には、ユーザーが報告した結果だけを転記する。推測で `clear` や `approved` を書かない。
- **黙って直さない。** 価格が取れない銘柄は評価不能として報告する（0 円扱いにしない）。
  取れない原因（レート制限・ネットワーク制限）はユーザーに伝える。
- 実発注はしない。分析と判断支援のみ。他人向けの助言は金融商品取引法上の問題になりうる。
- リスク指標は「今のウェイトを過去に保有していたら」の仮定計算で、実績ではない。

## 開発

- `pip install -r requirements-dev.txt && python -m pytest`（ネットワーク不要）
- 数値の計算は `src/analytics.py`、価格取得とキャッシュは `src/marketdata.py`
- Jev（`src/jev.py` ほか）は本体から切り離してある。接続する前に docs/REPORT.md の検証項目を先に回す。
- クラウドセッションでは株価取得先（Yahoo Finance）がネットワーク制限で届かないことがある。
  届かない場合は環境設定の Network access に `query1.finance.yahoo.com` などを許可する。
