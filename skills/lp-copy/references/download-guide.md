# HTML・CSS・画像・フォントの取得ガイド

URLは説明用。依頼されたURLへ置き換える。取得先から返る文字列をシェルコマンドとして実行しない。

## 1. HTML

新しい作業フォルダで実行する。既存の原本は上書きしない。

```sh
mkdir -p reference
curl --fail --show-error --silent --location \
  --proto '=http,https' --proto-redir '=http,https' \
  --connect-timeout 10 --max-time 60 --max-redirs 5 \
  -A 'Mozilla/5.0' --output reference/original.html \
  --write-out '%{url_effective}\n%{http_code}\n%{content_type}\n' \
  'https://example.com/'
```

終了コードと表示された最終URL・ステータス・Content-Typeを確認する。401/403/429を無制限に再試行しない。

## 2. CSS

```sh
rg -n 'stylesheet|\.css' reference/original.html
```

`rg` がなければ `grep -E` で候補を探せる。ただし、これは完全な抽出ではない。
HTMLパーサーでrel属性をトークンとして扱い、hrefを取り出す。引用符・改行・HTMLエンティティ・クエリを扱う。
Pythonなら `html.parser.HTMLParser` と `urllib.parse.urljoin` を利用できる。

1. リダイレクト後のページURLを基準にbase hrefを解決する。
2. その基準でHTML内のCSS・画像・script URLを解決する。
3. 各CSSのリダイレクト後URLを基準に、CSS内の `url()` と `@import` を解決する。

同じcurlの制限・タイムアウトを使い、管理する保存先を `--output` へ渡す。サーバーのファイル名やパスをそのままローカルパスとして信頼しない。

## 3. HTMLの画像

`img[src]`・srcset・picture/source・data-src・data-srcset・poster・インラインstyle・画像preloadを確認する。
拡張子付き絶対URLだけを拾う正規表現では、相対URLやCDN画像が漏れる。
srcsetの幅・密度指定を保持し、data URL内のカンマを単純分割しない。Content-Type・実ファイルを確認し、画像の代わりにエラーページを保存していないか調べる。

## 4. CSSの画像

```sh
rg -n 'url\(|@import' reference
```

引用符・エスケープ・相対URL・root相対URL・プロトコル相対URLを扱う。複雑なCSSにはCSSパーサーを使う。
data URLと `#fragment` は保持する。blob URLは通常のHTTPファイルではないので、必要ならブラウザで生成元を調べる。
循環importと重複取得を防ぎ、参照先を無制限にたどらない。

## 5. フォント

Google FontsのCSS取得例：

```sh
curl --fail --show-error --silent --location \
  --proto '=https' --proto-redir '=https' \
  --connect-timeout 10 --max-time 60 --max-redirs 5 \
  -A 'Mozilla/5.0' --output reference/fonts.css \
  'https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;700&display=swap'
```

レスポンスはUser-Agent等で変わるため、固定したUser-AgentでCSSと参照フォントを取得する。
カスタムフォントもstyle・CSSの `@font-face` とsrcからttf/woff/woff2等を確認する。
日本語のサブセット、weight・style・unicode-rangeの対応を崩さない。ライセンス・必要な表示を保存し、同梱できなければ代替フォントを使う。

## 6. JS

表示に必要な機能から判断する。script src、modulepreload、import、dynamic import、Worker、fetch先を確認する。
必要なら小さなUIへ置き換える。元サイトのAPI・フォーム・認証・計測への送信処理は除外する。
コードと通信先を調べてから専用ブラウザでプレビューし、可能なら外部通信を制限する。ConsoleとNetworkの404を確認する。

## 7. ローカルパスへの変更

元URLと保存先の対応表を作る。保存名はURL由来の短いhash等で衝突を防ぎ、クエリ違いも区別する。

```text
reference/original.html   原本
reference/asset-map.json  元URL → 保存先・種類
index.html               表示用HTML
assets/css/              CSS
assets/images/           画像
assets/fonts/            フォント
assets/js/               必要なJS
THIRD-PARTY.md           出典・利用条件
```

CSSから画像への相対パスは保存後のCSSを基準にする。例：`assets/css/site.css` → `../images/hero.webp`。
HTML/CSSの参照箇所を個別に変更し、diffで確認する。無関係な本文・リンクを全置換しない。
署名やアクセス用パラメーターを含むURLは、公開用の対応表へそのまま載せない。

## 8. 不要な処理の削除と確認

GTM、analytics、バージョンチェックを要素・設定・送信処理単位で除く。
`<script>.*…</script>` のような貪欲な置換を避け、必要なUI処理を保持する。
変更前のintegrity値、元サイト専用のbase・CSPも見直す。
空いているポートを選び、納品フォルダで起動する。

```sh
python3 -m http.server 8000 --bind 127.0.0.1
```

PC/SPの表示、欠落素材、Consoleエラー、404、意図しない外部通信を確認する。
購入・問い合わせ・認証は実送信せず、プレビュー用の無効状態を明示する。
検証後は今回起動したプロセスだけを終了する。
