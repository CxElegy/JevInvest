---
description: 証券会社の取引履歴 CSV を取り込んで台帳に反映する（例: /import-csv ~/Downloads/tradehistory(JP).csv）
---

証券会社の CSV を取り込んでください。対象: $ARGUMENTS

1. `python -m src.cli import-csv <ファイル…>` を実行する（書式は列名で自動判定。
   判定できなければ `--broker sbi_jp|rakuten_jp|rakuten_us|rakuten_fund` を試す）。
   画面に出るのは件数と銘柄だけ。**CSV や出力ファイルの金額をユーザーに読み上げない。**
2. 警告をユーザーに伝える。黙って直さない。
   - 「買付・売付以外の行」（信用・再投資・入出庫など）は取り込んでいない。件数を伝える。
   - 「為替レートが空」の行は飛ばしている。約定時の為替を聞くか、証券会社の約定報告書を見てもらう。
   - 「資産クラスを既定値」「import_map に無い投信」は、`config/import_map.yaml`
     （無ければ `config/import_map.example.yaml` をコピー）に書いてから再実行する。
     資産クラスはユーザーに確認する。ETF を個別株扱いのまま進めない。
3. `config/transactions.imported.yaml` の中身を `config/transactions.yaml` の
   transactions に追記する（無ければ `config/transactions.example.yaml` の書式で作る）。
   二重取り込みは CLI 側で除いているが、追記前に件数が合うか確認する。
4. `/add-trade` の手順 3〜4 と同じく、`python -m src.cli transactions --no-quotes --write-derived`
   → `config/holdings.yaml` へ反映 → `python -m src.cli validate`。
5. 取り込んだ銘柄で `config/compliance.yaml` に記録が無いものがあれば、
   `python -m src.cli compliance` で知らせる（保有中の未確認銘柄は最優先で照会）。
6. これらのファイルは個人の金額データなのでコミットしない。
