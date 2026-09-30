---
description: 独立性（independence）の確認状況を見て、次にやることを整理する（例: /compliance、/compliance VT は DESC で問題なし）
---

独立性の確認状況を整理してください。ユーザーからの報告: $ARGUMENTS

1. `python -m src.cli compliance` を実行し、状態ごとの一覧と「次にやること」を伝える。
2. ユーザーが確認結果を報告した場合だけ、`config/compliance.yaml` を更新する
   （無ければ `config/compliance.example.yaml` の書式で作る）。
   - 書くのは、ユーザーが実際に DESC で確認・承認を受けたと言った事実のみ。日付も本人の申告どおりに書く。
   - **推測で `desc_result: clear` や `preclearance: approved` を書かない。**
   - 制限対象だった（`restricted`）と報告された場合は、保有中なら
     Independence & Conflicts Network への連絡が先だと必ず伝える。
3. このツールは制限対象かどうかを判定しない。判定の正は DESC と Independence & Conflicts Network で、
   ユーザーの代わりに「この銘柄は買って大丈夫」と言わない。
4. 更新後にもう一度 `compliance` を実行して結果を示す。ファイルはコミットしない。
