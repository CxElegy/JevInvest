---
description: 売買・配当・入出金を記録して台帳に反映する（例: /add-trade トヨタを 100 株 2800 円で買った、NISA 口座）
---

取引を記録してください。内容: $ARGUMENTS

1. 内容から次を確定する。足りないものは推測せず、ユーザーに聞く。
   日付、種別（buy / sell / dividend / deposit / withdraw）、銘柄コード、口座、数量、単価、通貨、手数料。
   **外貨建ては約定時の為替（fx_rate）が必須。** 無ければ聞く（黙って 1 倍にしない）。
   銘柄コードは Yahoo Finance の表記（東証は 4 桁 + .T、投信は FUND- で始める）。
2. `config/transactions.yaml` に追記する（無ければ `config/transactions.example.yaml` の書式で作る）。
3. `python -m src.cli transactions --no-quotes --write-derived` を実行して警告を確認し、
   `config/holdings.derived.yaml` の該当銘柄（総平均法の数量・平均単価・平均為替）を、
   `config/holdings.yaml` の positions に反映する。同じ銘柄・同じ口座は 1 行にまとめる。
   現金残高（CASH-*）も、売買に合わせて増減させるかユーザーに確認する。
4. `python -m src.cli validate` で検証し、反映後の保有を短く報告する。
5. これらのファイルは個人の金額データなのでコミットしない。
