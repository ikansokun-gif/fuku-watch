# fuku-watch

服セールウォッチの裏方です。Claude の毎朝の巡回（7:51）が `want.json` に「写真が必要な商品」を書き込むと、GitHub Actions がその写真を小さくして `thumbs/` に保存します。巡回はその写真を服セールウォッチのページに取り込みます（毎朝 6:17 にも念のため実行します）。

- `targets.json` … 監視先の一覧（ページの「監視先」タブから自動で書き写されます。手で直す必要はありません）
- `scripts/fetch.py` … 写真を取ってくるスクリプト（GitHub Actions で動きます）
- `engine/fw.py` … Claude の毎朝の巡回が使う差分エンジン（新着・値下げの判定、価格履歴、写真の取り込み）
- `want.json` … 写真を取ってくる商品の一覧（Claude の毎朝の巡回が書き込みます）
- `data/` … 監視先ごとの商品データ（お店が許可している場合のみ。いまは使っていません）
- `thumbs/` … 在庫ありの商品の写真（幅 360px）

いまは Shopify で作られたお店（URL に `/collections/` を含む一覧ページ）に対応しています。それ以外のお店は、Claude が一覧ページを直接読み取ります。

手動で今すぐ取りに行きたいときは、GitHub の「Actions」タブ →「fetch shop data and photos」→「Run workflow」を押してください。
