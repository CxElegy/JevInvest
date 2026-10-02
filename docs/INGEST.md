# AI 投資ツール 16 個の詳細 ingest

2026-10-02 に `bash tools/install_skills.sh` で `vendor/skills/` に取得した全ソースを読んだ記録。
README だけでなく、構成・入口・中心のコード・必要な鍵・動かした結果まで書く。
判定の要約は [TOOLS.md](TOOLS.md)。この文書はその根拠。

- 取得物は `vendor/`（git 管理外）。新しいクラウドセッションでは再取得が要る
- Python で使う分は `vendor/.venv`（システムの Python と衝突したため仮想環境に分けた）
- 「このプロジェクトで」の欄は、CLAUDE.md の方針（税・手数料後の長期の手取り、独立性、実発注しない）で判断した

## 目次

| # | ツール | 取得したコミット | ファイル数 | 判定 |
|---|---|---|---|---|
| 1 | [OpenBB](#1-openbb) | bbf1ab2（2026-09-29） | 4,134 | **使う（動作確認済み）** |
| 2 | [backtrader](#2-backtrader) | b853d7c（2023-04-19） | 378 | **使う（動作確認済み）** |
| 3 | [FinRobot](#3-finrobot) | 2717499（2026-09-28） | 1,049 | 参考（同梱のスキルが有用） |
| 4 | [TradingAgents](#4-tradingagents) | 8b22d43（2026-09-29） | 204 | 参考（Jev の使い方） |
| 5 | [ai-hedge-fund](#5-ai-hedge-fund) | 5d2c7ca（2026-09-26） | 102 | 参考（Jev の使い方） |
| 6 | [daily_stock_analysis](#6-daily_stock_analysis) | be148f3（2026-10-01） | 1,169 | 使わない |
| 7 | [qlib](#7-qlib) | be72549（2026-09-16） | 626 | 使わない |
| 8 | [RD-Agent](#8-rd-agent) | 484776c（2026-09-23） | 920 | 使わない |
| 9 | [FinRL](#9-finrl) | e60e26e（2026-09-28） | 208 | 使わない |
| 10 | [FinGPT](#10-fingpt) | fdb04c9（2026-09-23） | 436 | 使わない |
| 11 | [Vibe-Trading](#11-vibe-trading) | 0866c37（2026-10-01） | 2,643 | 使わない |
| 12 | [vnpy](#12-vnpy) | fa5206f（2026-08-06） | 196 | 使わない |
| 13 | [Lean](#13-lean) | 41c6e60（2026-09-30） | 6,354 | 使わない |
| 14 | [nofx](#14-nofx) | 638d404（2026-09-05） | 647 | 使わない |
| 15 | [go-stock](#15-go-stock) | f001d93（2026-10-01） | 552 | 使わない |
| 16 | [FinceptTerminal](#16-finceptterminal) | 3f444cb（2026-10-01） | 3,598 | 使わない |

## 全体を読んで分かったこと

1. **16 個のうち 12 個は「どの銘柄がいつ上がるか」を当てるか、自動で売買するための道具。**
   毎月の黒字を NISA で積み立てる段階では、手取りへの効き目が小さい。
   検証（`tools/backtest_habits.py`）では、売買のタイミングを工夫するほど結果が悪くなった
   （20 年積立の中央値：買い続ける 2.12 倍、下落を待つ 1.89 倍、下落で売る 1.53 倍）。
2. **予測の研究成果は、条件を見ると小さい。** qlib の公開ベンチマークで最も良いモデルでも、
   中国 A 株（CSI300）の 2017〜2020 年に、指数を年 11.6% 上回った程度（手数料 0.05%／0.15% 込み、
   税は含まない、20 回の平均）。日本の個人が NISA で同じことをするのは難しい（買うたびに年間枠を消費し、売った分の枠が戻るのは翌年）。
3. **Jev を使う実装が 2 つある**（ai-hedge-fund と TradingAgents）。このプロジェクトの `src/jev.py` と同じ API
   （`https://api.typesafe.ai/v1/systemone`）。比べると、こちらが足りないもの・向こうが足りないものがある（[5](#5-ai-hedge-fund) と [4](#4-tradingagents)）。
4. **FinRobot に Anthropic の資産管理スキル（financial-services-plugins 由来、MIT）が同梱されている。**
   財務計画・リバランス・損出しなど。米国の税制前提なので、日本の制度に直せば `/plan` `/review` を強くできる（[3](#3-finrobot)）。
5. **データ取得の不具合は、どのプロジェクトでも「黙って別の値になる」形で起きている。**
   Vibe-Trading の変更履歴には、欠けた終値が 0% のリターンになる、8 年分のつもりが最後の 2 年分だけ返る、
   などの実例が並ぶ。このプロジェクトの「黙って直さない」（評価不能として報告する）方針が正しいことの裏付け。

---

## 1. OpenBB

**何か**：株価・指数・ETF・経済指標・債券などの多数のデータ源を、1 つの Python API（`obb.…`）と REST API にまとめるライブラリ。
「Open Data Platform（ODP）」と呼ばれ、有料のアナリスト向け画面（OpenBB Workspace）の土台。

**中身**
- `openbb_platform/extensions/`：分野ごとの API（equity、etf、index、economy、fixedincome、currency、crypto、derivatives、news、technical、quantitative、econometrics、**mcp_server**）
- `openbb_platform/providers/`：リポジトリに入っているデータ源は bls・cboe・cftc・deribit・ecb・eia・famafrench・federal_reserve・finra・fred・government_us・imf・jodi・nasdaq・oecd・sec・tmx。yfinance などは別パッケージ（`openbb-yfinance`）
- `cli/`：対話型の端末。`desktop/`：デスクトップ版
- ライセンス：Apache-2.0（TOOLS.md に「AGPL 系」と書いたのは誤り。修正済み）

**仕組み**：拡張と提供元をプラグインとして登録し、初回の import 時に `obb` オブジェクトを組み立てる（このとき "Extensions to add … Building…" と表示される）。
同じ関数（例：`obb.equity.price.historical`）で `provider=` を切り替えられ、返り値は共通の型（`to_dataframe()` で表）。

**動かした結果**
- `vendor/.venv` に `openbb-core` `openbb-yfinance` `openbb-fred` `openbb-equity` `openbb-etf` `openbb-index` `openbb-economy` を入れた
- `obb.equity.price.historical("VT", provider="yfinance")` で 2026-10-01 の終値 157.80、`^GSPC` で 7,666.45 を取得できた
- 同じ時刻に Yahoo の API を直接叩くと 429（回数制限）だった。OpenBB 経由（yfinance ライブラリ）では取れた
- 拡張（equity など）を入れないと `obb.equity` が存在しない。最初に失敗したのはこれが原因

**必要なもの**：無料の提供元は鍵なし。FRED は無料の鍵、FMP・Polygon などは有料の鍵。

**このプロジェクトで**：使う。用途は 2 つ。
1. `src/marketdata.py` の取得先が止まったときの代替（今は yfinance 直）
2. 経済指標（FRED の金利・物価）と Fama-French の要因データで、配分の検証を広げる

`mcp_server` 拡張を使うと Claude から直接データを引ける。これは次の段階で検討する。

---

## 2. backtrader

**何か**：Python のイベント駆動型バックテスト。価格の足を 1 本ずつ進め、戦略の `next()` を呼び、注文をブローカー模擬で約定させる。

**中身**：`cerebro.py`（全体の進行役）、`strategy.py`、`broker.py`／`brokers/`（模擬ブローカー）、`feeds/`（CSV・pandas・Yahoo など）、
`indicators/`（移動平均など 100 以上）、`analyzers/`（シャープレシオ・ドローダウンなど）、`sizers/`、`commissions/`。

**状態**：最後のコミットは 2023-04-19（版 1.9.78.123）。**保守は止まっている。** GPL-3.0。

**動かした結果**：`tools/backtest_habits.py` で使用。1871〜2023 年の S&P500 月次（配当込み・物価調整後）で、20 年積立を 133 通り × 4 つの行動で回した（約 28 秒）。
使ってみて分かった癖：
- 初期資金 0 で始めると、内部の基準価額の計算が 0 除算で落ちる → ごく小さい初期資金（1e-9）を入れて回避
- `broker.add_cash()` で入れたお金は**次の足から**使える。最後の月の入金は評価額に入らない → 評価時に足し戻した
- 月次データでは `set_coc(True)`（その足の終値で約定）にしないと 1 か月ずれる

**このプロジェクトで**：使う。積立・配分・リバランスのルールを、買う前に過去データで確かめるために使う。
保守が止まっているので、将来は `bt`（FinRL-X が採用）などへの移行も選択肢。今の用途（月次・少数の資産）では十分。

---

## 3. FinRobot

**何か**：AI4Finance 財団の、銘柄調査レポートを作る AI エージェント基盤。3 世代が同じリポジトリにある。

| フォルダ | 世代 | 中身 |
|---|---|---|
| `finrobot_autogen/` | V0 | AutoGen 製の教育用。論文の再現用（`pip install finrobot` はこれ） |
| `finrobot_equity/` | V1 | OpenAI Agents SDK 製。ティッカーを入れると HTML／PDF の調査レポートを作る Web アプリ |
| `finrobot_desktop/` | V2 | PydanticAI＋FastAPI＋React/Tauri 製のデスクトップ版。「本番」と位置づけ |

**設計の核**：「**数字はコードで計算し、文章は LLM が書き、すべての数字に出どころを記録する**」。
DCF・WACC・DDM・LBO などの評価は `engine/compute/operators`（純粋な Python）で計算し、LLM は説明と統合だけを担う。
このプロジェクトの「計算は `src/analytics.py`、判断は Claude」と同じ考え方。

**同梱スキル（重要）**：`finrobot_desktop/skills/` に 56 個。Anthropic の financial-services-plugins を自動変換したもの（MIT、内容は無変更と明記）。
分野：equity-research、financial-analysis、investment-banking、private-equity、wealth-management、partner-lseg、partner-spglobal。

`wealth-management/` の 6 つが個人の資産管理に直結する：
- **financial-plan**：収支・退職・教育資金・相続の計画
- **portfolio-rebalance**：配分のずれと、税・コストを考えたリバランス
- **tax-loss-harvesting**：含み損の売却で税を減らす手順
- **client-review**・**client-report**・**investment-proposal**：顧客向けの面談準備・報告・提案

すべて**米国の制度が前提**（IRA・401(k)、短期／長期の譲渡益、年 $3,000 の損失控除、wash sale ルール）。
日本に置き換えると、損出しは特定口座の損益通算と 3 年の繰越控除、NISA 内の損は通算できない、wash sale ルールはない、などが違う。

**必要なもの**：OpenAI と FMP（Financial Modeling Prep）の鍵。V2 はデスクトップアプリ。

**このプロジェクトで**：アプリ本体は使わない（米国株の個別銘柄調査が中心）。
**同梱スキルは、日本の制度（NISA・iDeCo・特定口座）に直して `.claude/skills/` の `/plan` `/review` に取り込む価値がある。**

---

## 4. TradingAgents

**何か**：実際の運用会社の役割分担を LLM で再現した、売買判断のフレームワーク（Tauric Research、Apache-2.0）。

**流れ（`tradingagents/graph/setup.py`、LangGraph）**
1. アナリスト 4 人が並行で調べる：市場（テクニカル）、ファンダメンタルズ、ニュース、センチメント
2. 強気と弱気のリサーチャーが討論 → リサーチマネージャーがまとめる
3. トレーダーが売買案を作る
4. リスク担当 3 人（積極・保守・中立）が討論 → ポートフォリオマネージャーが最終判断
5. 判断は 5 段階（Buy・Overweight・Hold・Underweight・Sell）。読み取れないときは Hold にせず `REVIEW` を返す（`agents/rating.py`）

**データ**（`dataflows/vendors/`）：Yahoo、Alpha Vantage、SEC EDGAR、FRED、Polymarket、Reddit、StockTwits。
v0.5.0 で「その日時点で手に入った情報だけを使う」（point-in-time）を全経路に入れた。

**Jev の使い方**（`agents/post_screen.py`）：`TYPESAFE_API_KEY` があると、StockTwits と Reddit の投稿 1 件ずつに 2 問聞く。
- `about`（noul 型）：この投稿は対象銘柄についてか → 0.3 未満なら捨てる
- `stance`（choice 型）：投稿者は株価の上昇・下落・どちらでもない、のどれを見ているか → 確度 0.5 以上だけ数える
- 16 並列、429／529 は最大 3 回まで待って再試行、失敗したら選別せずに残し「選別できなかった」と明記

**このプロジェクトとの差**：
- 既定のモデルが `jev-latest`。このプロジェクトは `jev-1.13.0` などに固定している（固定しないと、ある日バックテストの結果が変わる）
- 応答のキャッシュがない。このプロジェクトは (model, state, questions) のハッシュで SQLite に保存して再現性を担保している

**このプロジェクトで**：売買判断には使わない。Jev の質問の作り方（「関係ない投稿を落とす」「確度が低い答えは数えない」）は、
13F のニュースや銘柄メモを Jev で仕分けるときの手本になる。

---

## 5. ai-hedge-fund

**何か**：「AI がアナリストを務めるヘッジファンド」の教育用の実装（MIT）。実際の発注はしない。端末アプリ `aihf`。

**構成**（`hedge_fund/`）
- `signals/`：投資家を模したエージェント（buffett・graham・munger・lynch・druckenmiller）と、数式だけの PEAD（決算後のドリフト）
- 投資家エージェントの正体は**システムプロンプトだけ**（例：buffett.py は「堀・経営・財務・割安さ・10 年持てるか」のチェックリストを LLM に渡す）。データは決算書のその時点のスナップショット
- `portfolio/construction.py`：複数のシグナルを重みづけして目標配分に
- `risk/limits.py`：1 銘柄の上限（例 25%）と総エクスポージャー（1.0 ＝ 借りない）を強制
- `backtesting/`：過去の決算日で再生して損益を計算
- `fund/example.yaml`：ファンドの定義（戦略・スタッフ・リスク・資本・リバランス頻度・ベンチマーク）

**Jev の使い方**（`llm/contract.py`・`llm/client.py`）
- 1 回の要求に 3 問：`direction`（choice：強気・弱気・中立）、`bullish_strength` と `bearish_strength`（score：0〜4）
- 確信度 ＝ 選んだ方向の strength × 25（中立は 0）。Jev 自身の confidence では確信度を拡大・縮小しない
- 応答の形を厳密に検査（確率の合計、型、範囲）。崩れていたら失敗として扱う
- 429・5xx は 1 回だけ再試行（Retry-After を見る）。エラーメッセージや記録から API キーを伏せ字にする
- キャッシュキーは「提供元・エンドポイント・エージェント・要求・契約の版」のハッシュ

**このプロジェクトとの差**：
- 向こうにあって、こちらにないもの：応答の形の厳密な検査（確率の合計・型・範囲）、エラーや記録からの API キーの伏せ字処理
- 両方にあるもの：429・5xx での再試行と Retry-After の尊重（こちらは指数バックオフで複数回）
- こちらにあって、向こうにないもの：オフラインモード（`JEV_OFFLINE=1`）、料金の見積もり、モデル版の固定を前提にした SQLite キャッシュ

**このプロジェクトで**：売買判断には使わない。`src/jev.py` を本体に接続するとき（docs/REPORT.md の検証項目を回した後）に、応答の検査とキーの伏せ字を取り込む。

---

## 6. daily_stock_analysis

**何か**：自分のウォッチリストの銘柄を毎日 AI で分析し、「決定ダッシュボード」を WeChat・Feishu・Telegram・Discord・Slack・メールに送るシステム（MIT）。
A 株・香港・米国・**日本（例 `7203.T`）**・韓国・台湾に対応。

**中身**：`src/`（分析・通知・取得）、`data_provider/`（AkShare・Tushare・Baostock・YFinance・Longbridge など）、`strategies/`（15 種）、
`apps/`（Web とデスクトップ）、`bot/`、`.github/workflows/00-daily-analysis.yml`（**GitHub Actions で毎日自動実行**。サーバー不要）。
リポジトリに `SKILL.md`（`stock_analyzer`）と `CLAUDE.md` があり、Claude のスキルとしても呼べる。

**出力の形**：一言の結論・シグナル・ポジションの提案、テクニカル（トレンド・価格位置・出来高・需給）、ニュースとリスク、
「作戦」（狙う買値・売値、ポジションの取り方、リスク管理のチェックリスト）。

**戦略**（`strategies/*.yaml`）：移動平均のゴールデンクロス、出来高ブレイク、波動理論、纏論（チャン理論）、「龍頭」（テーマの主導株）、感情サイクル、ホットテーマなど。ほぼすべて短期の値動きの型。

**必要なもの**：LLM の鍵、ニュース検索の鍵（SerpAPI・Tavily など）。

**このプロジェクトで**：使わない。毎日「買い時・売り時」を知らせる仕組みは売買を増やす方向に働き、手取りを減らしやすい。
ただし「GitHub Actions で毎日動かして通知する」作りは、タブレットだけの環境でも定期処理を回す方法として参考になる（例：13F の更新確認）。

---

## 7. qlib

**何か**：Microsoft の定量投資の研究基盤（MIT）。データ・因子・予測モデル・バックテスト・強化学習の執行まで一式。

**中身**：`qlib/data`（独自形式の高速データ）、`qlib/contrib/model`（LightGBM・XGBoost・CatBoost・線形・LSTM・GRU・Transformer・TabNet・TCN・HIST・TRA など 30 以上）、
`qlib/backtest`、`qlib/rl`、`scripts/data_collector`（中国・米国・ブラジルの指数、Yahoo、投信、暗号資産）。日本向けの取得スクリプトはない。

**公開ベンチマーク**（`examples/benchmarks/README.md`）：中国 A 株 CSI300、学習 2008〜2014、検証 2015〜2016、**テスト 2017-01〜2020-08**、手数料 買 0.05%・売 0.15%、20 回の平均。

| モデル（Alpha158 の特徴量） | 指数に対する年超過リターン | 情報比 | 最大ドローダウン |
|---|---|---|---|
| DoubleEnsemble | 11.6% | 1.34 | −9.2% |
| LightGBM | 9.0% | 1.02 | −10.4% |
| 線形 | 6.9% | 0.92 | −15.1% |
| Transformer | 2.7% | 0.40 | −11.0% |

予測と実際のリターンの相関（IC）は 0.03〜0.05 程度。1 つの市場・1 つの期間の結果で、税は含まない。

**このプロジェクトで**：使わない。日本のデータがなく、毎日の銘柄入れ替えが前提で NISA と両立しない。

---

## 8. RD-Agent

**何か**：Microsoft の「研究開発を自動化する」AI エージェント（MIT）。仮説 → コード → 実験 → 評価 を LLM が回す。
金融では qlib の上で因子とモデルを自動で探す（`rdagent fin_factor`・`fin_model`・`fin_quant`、決算資料から因子を作る `fin_factor_report`）。

**中身**：`rdagent/scenarios/`（qlib・kaggle・data_science・finetune・rl）、`rdagent/app/`、`web/`（Vue の画面）。
MLE-bench（Kaggle 75 課題）で上位という実績がある。

**必要なもの**：**Docker が必須**（生成したコードを隔離して実行するため）、LLM の鍵（OpenAI・Azure・DeepSeek・LiteLLM）。
README 自身が「生成コードを実行するので、隔離した環境で、ホストの認証情報を渡さずに」と警告している。

**このプロジェクトで**：使わない。このクラウド環境では Docker を使えず、目的も短期予測の因子探し。

---

## 9. FinRL

**何か**：金融の深層強化学習の教育・研究用フレームワーク（MIT）。後継は FinRL-X（別リポジトリ）と README が案内している。

**中身**：`finrl/meta/`（環境：株式売買・ポートフォリオ配分・暗号資産、データ処理、ペーパートレード）、
`finrl/agents/`（stable-baselines3・ElegantRL・RLlib・FastTD3）、`finrl/applications/`（株式売買、ポートフォリオ配分、暗号資産、高頻度、模倣学習、NeurIPS 2018 の再現）。

**仕組み**：「データ → 環境 → エージェント学習 → 売買」の流れ。報酬はポートフォリオ価値の変化。

**このプロジェクトで**：使わない。学習に GPU と時間がかかり、過去に合わせすぎる危険が大きい。積立の判断に強化学習は要らない。

---

## 10. FinGPT

**何か**：金融向けに微調整したオープンな LLM 群（MIT）。感情分析（v1〜v3）、株価の方向予測（FinGPT-Forecaster、Llama 2 の LoRA）、
RAG、マルチエージェント RAG、ベンチマーク。

**取り込んで分かった変化**：リポジトリに `finogrid/` が加わっている。中身は**ステーブルコイン（USDT／USDC）での企業間送金と、AI エージェント同士の少額決済の基盤**で、
投資分析とは別物。

**必要なもの**：GPU、Hugging Face の鍵、Finnhub などのデータの鍵。

**このプロジェクトで**：使わない。モデルは 2023 年が中心で、汎用の LLM（Claude・Jev）より優れる根拠が見当たらない。

---

## 11. Vibe-Trading

**何か**：香港大学（HKUDS）の、対話で戦略を作り、バックテストし、実際に売買までする AI エージェント（MIT）。CLI・Web・デスクトップ・MCP。

**規模と活動**：2,643 ファイル。2026-09-29 に v0.1.16（前版から 492 コミット）。ほぼ毎日修正が入っている。

**機能**：462 個の「Alpha Zoo」因子、15 個以上の数理ライブラリの追加（Heston、階層的リスクパリティ、コピュラなど）、
**18 のブローカー接続**（Robinhood・IBKR・Alpaca・Longbridge・Zerodha・KIS・Toss など）、複数口座をまとめて見る読み取り専用のポートフォリオ、
「Shadow Account」、数字の出どころを検査してから回答する「grounding gate」。日本の証券会社への接続はない。評価できる通貨は USD・HKD・CNY。

**変更履歴から分かること**（このプロジェクトに効く教訓）
- pandas の `pct_change()` は既定で前の値を埋めるので、欠けた終値が「0% のリターン」になる。10 ファイル 24 か所で見つかった
- データ源が期間の**最後の** 500 本だけ返し、8 年分のつもりが 2 年分になっていた。エラーは出なかった
- テストは通るのに、実際のブローカーの応答形式と違っていて、実注文がすべて拒否されていた
- ブローカーの API キーを入れる設定ファイル 13 個が `.gitignore` から漏れていた

**このプロジェクトで**：使わない。実売買が中心で、方針と合わない。上の教訓は `src/marketdata.py` の点検項目にする。

---

## 12. vnpy

**何か**：中国の個人・機関向けの自動売買フレームワーク「VeighNa」（MIT）。4.0 で機械学習の `vnpy.alpha`（qlib の Alpha158 を移植、Lasso・LightGBM・MLP）を追加。

**中身**：`vnpy/trader/`（エンジン・ゲートウェイ・データベース・WeChat 通知）、`vnpy/event`、`vnpy/rpc`、`vnpy/chart`、`vnpy/alpha`。
接続は CTP（中国の先物）などのゲートウェイを別パッケージで足す。

**このプロジェクトで**：使わない。中国の先物・株式の執行が中心。

---

## 13. Lean

**何か**：QuantConnect の売買エンジン（Apache-2.0）。C# 製で、戦略は C# か Python で書ける。バックテストと実売買を同じコードで動かす。

**中身**：`Engine`、`Algorithm.*`、`Brokerages`、`Indicators`（多数）、`Optimizer`、`Report`、`Research`、Docker ファイル群。6,354 ファイルで 16 個の中で最大。

**必要なもの**：.NET か Docker（Lean CLI）。データは QuantConnect から（多くは有料）。

**このプロジェクトで**：使わない。プロ向けの重い基盤で、月次の積立の検証には backtrader で足りる。

---

## 14. nofx

**何か**：LLM を戦略そのものにした**暗号資産の自動売買端末**（Go＋TypeScript、AGPL-3.0）。

**初回の流れ**（README より）：アカウント登録 → 作られる「AI 手数料用ウォレット」に **$1 以上の USDC**（Base ネットワーク）を入れる →
Hyperliquid をつなぎ **$12 以上の USDC** を入金 → 「Autopilot」を開始すると、**AI が数分ごとに市場を見て自分で売買する**。

**中身**：`trader/`（取引所ごと。Hyperliquid・Binance・Bybit など 9 取引所）、`kernel/`、`provider/coinank`（データ）、`mcp/`、`store/`、`api/`、`web/`。
コード側の安全装置：ポジション数とレバレッジの上限、取引所側の損切り、ドローダウンでの自動決済、売買の頻度制限。README に取引所の紹介リンク（手数料割引）がある。

**このプロジェクトで**：使わない。実際のお金を AI に自動で動かさせる仕組みで、方針（実発注しない）と根本から合わない。

---

## 15. go-stock

**何か**：中国株向けのデスクトップ分析アプリ（Go＋Wails＋Vue、GPL-3.0）。A 株・香港・米国、場内 ETF。

**機能**：AI エージェント（3 方式）、150 以上のデータツール、MCP、スクリーンショットからの質問、K 線の作図（波動理論・神奇九転など）、
龍虎榜・資金の流れ・ストップ高の追跡、毎日の操作計画とアラート。README 自身が「娯楽目的」「主に Windows 10 以降で開発」と書いている。

**このプロジェクトで**：使わない。タブレットでは動かず、短期売買の道具。

---

## 16. FinceptTerminal

**何か**：C++20＋Qt6 のデスクトップの金融端末（AGPL-3.0）。Python 3.11 を内蔵。Bloomberg 端末の代替をうたう。

**中身**：`fincept-qt/`（CMake の C++ プロジェクト）、`docs/`、`setup.sh`。
README の冒頭は有料版の案内（Enterprise $15〜45／月、Quantcept $0〜8／30 日）。

**このプロジェクトで**：使わない。パソコン専用で、タブレットとクラウドでは動かない。

---

## このプロジェクトに取り込むこと（優先順）

1. **済**：backtrader で「続け方」の検証（`tools/backtest_habits.py` → `web/evidence.json` → 「道筋」タブ）
2. **次**：OpenBB を `src/marketdata.py` の予備の取得先にする（Yahoo の回数制限への対策）
3. **次**：FinRobot 同梱の wealth-management スキルを日本の制度に直して `/plan` `/review` に入れる（NISA・iDeCo・特定口座の損益通算）
4. **Jev 接続時**：ai-hedge-fund の応答検査・キーの伏せ字、TradingAgents の「関係ない投稿を落とす」質問の設計を `src/jev.py` に取り込む
5. **点検**：Vibe-Trading の変更履歴にある「欠損が 0% になる」「末尾だけ返る」型の不具合が `src/marketdata.py` にないか確かめる
