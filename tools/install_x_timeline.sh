#!/usr/bin/env bash
# x-timeline-archive（X の Web 版の検索を、ログイン cookie で呼ぶ bird-search を同梱）を vendor/ に入れる。
# 中身を確認したコミットに固定している。更新するときは新しいコミットの差分を読んでから COMMIT を変える。
#   通信先は x.com / twitter.com だけ（2026-10 に確認）。投稿・画像アップロードの処理は使っていない。
# 使うには環境変数 X_AUTH_TOKEN と X_CT0（x.com の cookie の auth_token と ct0）が要る。
# cookie はパスワードと同じ扱い。捨てアカウントのものを使い、コード・チャット・コミットに書かない。
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=https://github.com/Xiao-yun-Hu/x-timeline-archive
COMMIT=c7eda1a2b8cce9fdc6b2e4b075d75bad89a34a13
DEST=vendor/x-timeline-archive
if [ -d "$DEST/.git" ] && [ "$(git -C "$DEST" rev-parse HEAD)" = "$COMMIT" ]; then
  echo "インストール済み: $DEST"; exit 0
fi
rm -rf "$DEST"
mkdir -p vendor
git clone -q --filter=blob:none "$REPO" "$DEST"
git -C "$DEST" checkout -q "$COMMIT"
echo "→ $DEST ($COMMIT)"
