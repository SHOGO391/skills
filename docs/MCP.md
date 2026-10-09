# MCPの使い方

このサーバーはローカルの **stdio** で動きます。公開URL・APIキーは不要です。
stdio MCPに対応するクライアントで使います。HTTP URLだけを受け付けるリモート接続先へ、そのまま登録することはできません。

## 準備

Python 3.10以上と [uv](https://docs.astral.sh/uv/getting-started/installation/) を用意します。
リポジトリをまだ取得していなければクローンしてください。

```sh
git clone https://github.com/SHOGO391/skills.git shogo391-skills
cd shogo391-skills
uv sync --locked
```

初回の依存取得にはネットワークを使います。以後のサーバー処理はネットワークを使用しません。
リポジトリ全体を保持してください。`mcp_server.py` だけを移動すると同梱スキルを読めません。

## Codex

リポジトリのフォルダ内で次を実行します（macOS / Linux）。既存の `lp-skills` 設定がある場合は先に比較してください。

```sh
codex mcp add lp-skills -- "$PWD/.venv/bin/python" "$PWD/mcp_server.py"
codex mcp list
```

Codexを再起動し、`/mcp` で接続を確認します。[CodexのMCP設定](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) を参照してください。

TOMLで設定する場合の例（絶対パスを実際の保存場所へ置き換えます）：

```toml
[mcp_servers.lp-skills]
command = "/absolute/path/shogo391-skills/.venv/bin/python"
args = ["/absolute/path/shogo391-skills/mcp_server.py"]
```

## Claude Code

同じフォルダ内で、利用者用の設定へ追加する例：

```sh
claude mcp add --transport stdio --scope user lp-skills -- "$PWD/.venv/bin/python" "$PWD/mcp_server.py"
claude mcp list
```

[Claude CodeのMCP設定](https://code.claude.com/docs/en/mcp) も参照してください。

## JSON設定を使うクライアント

`mcpServers` 形式の設定に対応するクライアント向けの例です。保存場所は各クライアントの設定画面で確認してください。

```json
{
  "mcpServers": {
    "lp-skills": {
      "command": "/absolute/path/shogo391-skills/.venv/bin/python",
      "args": ["/absolute/path/shogo391-skills/mcp_server.py"]
    }
  }
}
```

Windowsではcommandを `C:/path/shogo391-skills/.venv/Scripts/python.exe`、argsを同じ保存先の `mcp_server.py` へ変更します。
既存の設定へこのサーバーだけを追加し、他のサーバー設定は保持してください。

## AIへの依頼例

```text
lp-skillsのget_lp_copy_workflowを読んで、次のURLのLPをローカルに再現して。
取得とPC/SPの確認には、使えるシェルとブラウザを使って。
URL：［参照URL］
保存先：［新しいフォルダ］
```

```text
get_section_replace_workflowをsection="Hero"で読んで、今のLPのHeroだけを変更して。
デザインは参考URLに合わせ、文章・リンク・色・フォントは今のLPを使って。
対象外のコードを保持して、最後にverify_section_replacementでも確認して。
今のLP：［HTML全文またはファイルパス］
参考URL：［参照URL］
```

ツール取得後、AIが必要なURL・HTMLを読み、デザインを作成します。MCP自身はサイトをダウンロードしたり、HTMLのデザインを生成したりしません。

## ツール・リソース・プロンプト

| 種類 | 名前 | 内容 |
| --- | --- | --- |
| Tool | `get_lp_copy_workflow` | 取得の8段階と補足資料 |
| Tool | `get_section_replace_workflow` | `section` に8種類のいずれかを指定 |
| Tool | `replace_lp_section` | 元HTML内の一意な文字列範囲だけを置換し全文を返す |
| Tool | `verify_section_replacement` | 変更結果がその置換と完全一致するか検証 |
| Resource | `lp-skills://lp-copy` | 取得・再現の手順 |
| Resource | `lp-skills://lp-section-replace` | 部分差し替えの手順 |
| Prompt | `lp-copy` | `url` を指定して依頼を組み立てる |
| Prompt | `lp-section-replace` | `section` と `url` を指定。元HTMLは会話で渡す |

Prompt/Resourceに未対応のクライアントでも、Toolから同じ手順を取得できます。

### HTML置換の例

`replace_lp_section` の引数：

```json
{
  "original_html": "<!DOCTYPE html><html><body><header>元</header><main><section id=\"hero\">見出し</section></main><footer>保持</footer></body></html>",
  "original_section": "<section id=\"hero\">見出し</section>",
  "replacement_section": "<section id=\"hero\" class=\"lpsr-hero-root\"><h1>見出し</h1></section>"
}
```

一意に完全一致する範囲が必要です。空文字・複数一致・未一致はエラーにします。
返される `html` が完全版、`updated_sha256` がUTF-8でのSHA-256です。クライアントが長い結果を省略していないか確認してください。
各入力と結果は最大2,000,000 UTF-8 bytes。ファイルへの保存はクライアントが行います。

`verify_section_replacement` には同じ3引数に加えて `updated_html` を渡します。
`matches_expected_replacement: true` は、指定範囲だけの置換と完全一致したことを意味します。
不一致時の `outside_unchanged` は `null` です。範囲外の変更か置換内容の違いかを、その値だけでは断定しません。

head/body末尾へのCSS/JS追加は、厳密な範囲置換には含まれません。
追加分を別途diffで確認し、その追加分だけを除いた比較用HTMLを検証へ渡してください。検証のために元ファイルを書き換えないでください。
範囲の選び方、DOMの妥当性、文言の保持、CSS/JSの視覚的・機能的影響は別途確認が必要です。

## 検証

```sh
uv run --locked python -m unittest discover -s tests -v
```

実際のstdioサブプロセスを起動し、接続、tools/list・call、resources/list・read、prompts/list・getを確認します。
特定のクライアント画面や、実サイトを使ったAIの再現品質まで検証するものではありません。
実装は [公式MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) を使用しています。
