---
description: 定期実行用。直近の新着投稿だけを見て、持仓に触れるものがあれば その人の視点の分析を短く返す（例: /influencers-watch、/influencers-watch 3 = 直近 3 時間）
---

クラウドの定期実行（Routine）から毎回新しいセッションで呼ばれる前提。前回の取り込みは残っていないので、
**投稿時刻で窓を切って新着を判定する**（投稿 ID から時刻が出る）。窓の長さ（時間）: $ARGUMENTS（空なら 1.25）。
返答はユーザーの言語（中国語）で書く。

1. 準備: `pip install -q -r requirements.txt`。
2. 取り込む: `python tools/influencers.py fetch --hours <窓>`。
   - Serenity は X API / RSS が無ければ serenitystock.com（第三方の要約）から自動で取る。
   - **Jukan は X API / RSS が無いと取れない。** そのときは WebSearch で `Jukanlosreve` の最近の投稿を探し、
     `https://x.com/Jukanlosreve/status/<数字>` の URL と、検索結果に出ている本文がそろうものだけを、
     1 件ずつ「本文 + 空行 + URL」の形にして、ブロックの間を `---` の行で区切り
     `python tools/influencers.py import - --who jukan --hours <窓>` に標準入力で渡す。
     本文が分からないもの・別人の投稿は入れない。時刻は URL の ID から自動で出る。
3. `python tools/influencers.py report --hours <窓>` を実行し、`reports/influencers.md` を読む。
4. 窓の中で持仓に触れる投稿が **0 件なら**、「新着なし（取れなかったものがあればその理由）」と 1〜2 行だけ返して終わる。
   分析は書かない（通知を増やさない）。
5. 1 件以上あれば、短く返す（スマホの通知で読める長さ）:
   - 誰が何を言ったか（1 行ずつ、URL つき。serenitystock の要約なら「第三方の要約」と書く）
   - その人の視点で、触れた持ち株（比率・どのファンド経由か）をどう見そうか。投稿に無い意見を作らない。推測は推測と書く
   - 行動: ほとんどの場合「何もしない」。持仓は 3 本のインデックスファンドで、1 つの投稿は比率を変える理由になりにくい。
     個別株の後追いは根拠が弱く、独立性の確認（/compliance）が先だと添える
6. コミット・プッシュ・PR はしない（data/ と reports/ は .gitignore 済み）。
