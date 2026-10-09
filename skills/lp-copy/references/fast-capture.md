# 共通CLIで取得を速くする

このCLIは公開ページの素材を取得し、元のパスを保ったローカルHTTP表示を提供する。初期設定済みの取得・変換・検証を再利用し、AIは取得範囲、主要操作、利用条件、表示差分の判断に集中する。

## 初回だけ

Node.js 22以上を使う。以下の `scripts` はこのスキル内のフォルダ。利用環境に既存の適切な依存がなければ1回だけ導入する。

```sh
cd /path/to/lp-copy/scripts
npm ci
npx playwright install chromium
```

LinuxでブラウザのOS依存がない場合はPlaywrightの公式手順に従う。以後は同じインストールを再利用し、案件ごとにnpm installやブラウザのダウンロードを繰り返さない。

## 通常の取得

```sh
node /path/to/lp-copy/scripts/capture.mjs capture \
  --url 'https://example.com/' --out ./reference-copy \
  --actions ./actions.json
node /path/to/lp-copy/scripts/capture.mjs serve --out ./reference-copy --port 8766
```

CLIが出力したloopback URLで開く。保存先は新規フォルダ。HTMLをfile://で開いたり、フォルダを一般の静的サーバーで丸ごと配信したりしない。原本を公開せず、クエリ付き素材や外部CDNの参照を正しく解決するため、同梱のserveを使用する。

`actions.json` は取得対象ページを読んで作る。無関係な操作を総当たりしない。省略時はPC/SPの表示と最大60ステップのスクロールだけ。例:

```json
[
  {"action":"click","role":"button","name":"メニューを開く"},
  {"action":"press","key":"Escape"},
  {"action":"click","role":"tab","name":"料金","expect":{"attribute":"aria-selected","value":"true"}}
]
```

role/nameは完全一致。必要なら `selector` でCSSセレクターを指定できる。`viewport: "mobile"` / `"desktop"` で幅を限定する。実行対象は新しい一時ブラウザで、既存ログイン状態を読み込まない。POST等の送信・任意のfetch/XHR・WebSocket・別ページ/iframeへの移動を止める。通常の静的素材GETと、明示した `--data-url URL` のGETだけを対象にする。決済や問い合わせ操作をactionsへ入れない。

## 追加取得とキャッシュ

```sh
# 1時間以内の正常な保存済みデータを再利用。既存のcapture URLにだけ使える。
node /path/to/lp-copy/scripts/capture.mjs capture \
  --url 'https://example.com/' --out ./reference-copy --resume --actions ./actions.json

# 外部CSS・HTML内のCSS参照（未使用フォント分割・背景画像等）も補完
node /path/to/lp-copy/scripts/capture.mjs capture \
  --url 'https://example.com/' --out ./reference-copy --resume --mode complete --actions ./actions.json
```

- `--include URL`：未操作時の画像等を静的確認で見つけた場合に追加。複数指定可。
- `--exclude 部分文字列`：取得不要/同梱できない素材URLを除外。該当URLを参照する外部CSSのfont-faceも除き、既存フォールバックを使う。他の素材やインライン定義を除外した場合は必要な代替を行う。いずれも表示差分を確認・報告する。
- `--refresh`：TTL内でも更新を確認。ETag/Last-Modifiedがあれば条件付きリクエスト。
- `--cache-age 秒`：TTL（既定3600）。0は毎回更新確認。
- `--concurrency 数`：取得の同時数（既定8、最大16）。通常は増やす必要はない。
- `--allow-private`：自分のローカル検証サイト用。公開サイトの通常取得には付けない。

600 URL、1素材32MiB、合計256MiB、5リダイレクトで制限する。失敗・取得除外・CSSの未取得参照を `report.json` に記録し、際限なく再試行しない。キャッシュはこの保存先内だけで共有し、ログインCookie/Authorization/Set-Cookieは保存・送信しない。

resumeでactions/include/exclude/data-urlを省略すると前回の指定を引き継ぐ。操作と除外条件が同じで全キャッシュがTTL内・ハッシュ一致なら、原本のブラウザ確認も再利用する（`inspectionReused: true`）。これは最新の元サイトを再確認したという意味ではない。操作変更・破損・期限切れ・refresh時は原本を再確認する。

途中の操作で失敗した場合も保存先を捨てず、actionsを直して同じフォルダへ `--resume` する。開始時の `capture-progress.json` とURLごとのキャッシュ記録から取得済みデータを再利用する。正常に最後まで取得できるまでは未完成として扱う。

## 最後に確認

```sh
node /path/to/lp-copy/scripts/capture.mjs verify --out ./reference-copy
```

同じactionsを使ってPC1440×1000/SP390×844を確認し、外部通信を遮断する。見出し、画像、横はみ出し、HTTP/JS/Consoleエラーを検査し、失敗は非0終了。スクリーンショットと `verification.json` を保存する。これに加えて原本とローカルの画像を目視比較する。

`report.json` の所要秒数・取得数・キャッシュヒットを比較する。`networkBytes` はHTTPで取得した本文の展開後サイズで、圧縮込みの実回線量ではない。キャッシュの有無、同じ操作範囲かを区別し、未計測の短縮率は書かない。

## 保存内容と限界

- `original.html` / `raw/`：原本。`capture.json`：URLと保存先・HTTP型・ハッシュ・キャッシュ情報。
- `index.html` / `objects/`：変換済み。`screenshots/`：原本/ローカルのPC/SP画像。
- `report.json`：計測・未取得項目。`verification.json`：検証結果。

HTML/CSSを構文解析し、JSでは取得済み素材の絶対URL文字列だけを変換する。同一サイト内の動的パスとES moduleの相対importは専用サーバーが元パスで配信する。外部URLの動的な組立、Worker、認証、バックエンド、未訪問ルートの完全複製は保証しない。取得漏れや差分があれば必要な部分だけ手動補完する。取得失敗があっても調査用成果物は保存するため、captureの終了だけで完成扱いにせずverifyと目視確認まで実施する。

quickで後回しにしたフォント分割は、文言を変更すると必要になることがある。文字変更・幅の追加・未確認操作まで使う場合はcompleteで補完し、追加条件で検証する。completeでも未訪問ページやJSで生成される全素材まで取得する意味ではない。

出典と素材の利用条件、代替フォント、未収録機能は `THIRD-PARTY.md` 等へ別途記録する。原本・キャッシュ・対応表には取得URLが含まれるので公開スキルのリポジトリへ追加しない。
