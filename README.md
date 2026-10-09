# SHOGO391 Skills

[![Validate skills](https://github.com/SHOGO391/skills/actions/workflows/validate.yml/badge.svg)](https://github.com/SHOGO391/skills/actions/workflows/validate.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**参考LPのローカル再現と、既存LPのセクション差し替え。スキル・プロンプト・MCPの3通りで使えます。**

HTML・CSS・画像・フォントを整理して再現する手順と、今あるLPの文言や配色を保って一部分のデザインだけを変える手順をまとめています。

## 収録内容

| スキル | 用途 | 入力 |
| --- | --- | --- |
| [lp-copy](skills/lp-copy/SKILL.md) | HTML・CSS・画像・フォント・必要なJSを取得してLPをローカル再現 | 参照URL、保存先 |
| [lp-section-replace](skills/lp-section-replace/SKILL.md) | 既存LPの指定セクションだけデザイン変更 | 元HTML、対象セクション、参照URL |

差し替えは **Header / Hero / Features / Testimonials / Pricing / CTA / FAQ / Footer** の8種類に対応。
`lp-copy` の「copy」はページの再現を意味します。

## MCPとして使う

ローカルのstdio MCPサーバーを同梱しています。[導入・設定手順](docs/MCP.md) を参照してください。

| ツール | 機能 |
| --- | --- |
| `get_lp_copy_workflow` | LPの取得・ローカル化の手順を取得 |
| `get_section_replace_workflow` | セクション差し替えの手順を取得 |
| `replace_lp_section` | 一意に一致するHTML範囲だけを置換し、完全なHTMLを返す |
| `verify_section_replacement` | 指定範囲だけの置換と完全一致するか検証 |

MCPは手順の提供とHTML文字列の置換・検証を担当します。**取得・デザイン編集・ファイル保存・ブラウザでの画面確認は、接続先AIのツールで実行します。**
MCP単体の自動クローラーや画面再現エンジンではありません。APIキーや公開サーバーは不要です。

## スキルとして使う

### インストール

以下はmacOS / Linux向け。Windowsでは同じフォルダを利用するAIのスキル保存先へコピーできます。

```sh
git clone https://github.com/SHOGO391/skills.git shogo391-skills
cd shogo391-skills
```

Codexの個人用スキルへ追加する例。既存の同名スキルは上書きしません。

```sh
mkdir -p "$HOME/.agents/skills"
for skill in lp-copy lp-section-replace; do
  if [ -e "$HOME/.agents/skills/$skill" ] || [ -L "$HOME/.agents/skills/$skill" ]; then
    echo "$skill は存在します。更新前に内容を比較してください。"
  else
    cp -R "skills/$skill" "$HOME/.agents/skills/$skill"
  fi
done
```

Claude Codeではコピー先を `~/.claude/skills/` にします。
案件内だけで使う場合は、Codexなら `.agents/skills/`、Claude Codeなら `.claude/skills/` へ配置してください。
Codexで反映されない場合は再起動します。
詳細は [Codex公式](https://learn.chatgpt.com/docs/build-skills)・[Claude Code公式](https://code.claude.com/docs/en/skills) を参照してください。

### 使用例

`lp-copy` には、ブラウザが使った素材を優先取得する共通CLIを同梱しています。**取得・パス変換を毎回実装せず、PC/SPで使う素材を保存してローカル表示できます。**
通常の `quick`、CSSの未使用フォント等も補完する `complete`、8並列取得、キャッシュ再利用、同じ操作でのPC/SP検証に対応します。

```sh
cd skills/lp-copy/scripts
npm ci
npx playwright install chromium
node capture.mjs capture --url 'https://example.com/' --out ./reference-copy
node capture.mjs serve --out ./reference-copy --port 8766
```

Node.js 22以上が必要です。メニュー・タブ操作の指定、再取得、検証は [共通CLIの使い方](skills/lp-copy/references/fast-capture.md) を参照してください。初回の依存導入後は同じインストールを再利用できます。MCPサーバーの依存にNode.jsを追加するものではありません。
取得量・所要時間と比較条件は [実測結果](docs/CAPTURE-BENCHMARK.md) にまとめています。

Codexの場合：

```text
$lp-copy
このURLのLPを、新しいフォルダにローカル再現して。
HTML・CSS・画像・フォントを取得し、ローカルパスへ修正して。
元サイトへのフォーム送信・計測は動かさず、PC/SPを確認して。
参照URL：［URL］
```

```text
$lp-section-replace
今のLPのHeroだけを、参考URLのHeroデザインに差し替えて。
文言・リンク・配色・フォントは今のLPのものを使って。
対象外の既存コードはそのまま残し、完成HTMLを省略なしで出して。
今のLP：［HTML全文、またはファイルパス］
参考URL：［URL］
```

Claude Codeでは先頭を `/lp-copy` または `/lp-section-replace` にします。

## プロンプトとして使う

スキルをインストールしなくても、[8種類の差し替えプロンプト](prompts/section-replace.md) をコピーして使えます。
HTML全部と参考URLを貼って送信してください。
[素材の取得ガイド](skills/lp-copy/references/download-guide.md) には、HTML・CSS・画像・フォント・JS取得からパス修正までの8段階を記載しています。

## 設計と制約

- 相対URL、CSS import、srcset、日本語フォントのサブセット、同名assetの衝突を確認します。
- セクション差し替えは元の文言・URL・価格・人数・項目数と、対象外の既存コードを保持します。
- 専用CSS/JSをhead/body末尾に追加する場合は追加分を明記します。範囲外への追加も禁止された依頼では、その制約を優先します。
- prefixと対象rootへの限定、PC/SP、変更前後のdiffでCSS/JSの影響を確認します。
- 元サイトの文言・会社情報・リンクの新規混入を確認します。必要なライセンス表示まで削除するものではありません。

取得にはネットワークとシェル、画面確認にはブラウザ機能を持つAI環境が必要です。
ログイン後の画面やバックエンド機能、利用できない素材は対象外または代替が必要です。
AI・ツール・対象サイトによって結果が変わるため、実際の表示と内容を確認して利用してください。

## 構成

```text
skills/lp-copy/              # 取得・ローカル再現スキルと補足資料
skills/lp-section-replace/   # 部分差し替えスキルと8種の確認項目
prompts/section-replace.md   # 8種類のコピペ用プロンプト
mcp_server.py               # stdio MCPサーバー
docs/MCP.md                 # MCP導入方法
tests/                      # HTML保持・MCP接続テスト
scripts/validate_skills.py   # スキル構造・相対リンク等の検証
```

## 開発・検証

Python 3.10以上、[uv](https://docs.astral.sh/uv/getting-started/installation/)、Gitを使います。

```sh
uv sync --locked
uv run --locked python scripts/validate_skills.py
uv run --locked python -m unittest discover -s tests -v
git diff --check
```

CIではメタデータ・ローカル参照・基本的な公開ファイル確認と、実stdio接続によるMCPテストを行います。
任意サイトの視覚的な再現品質や、モデルが手順を守ることまで保証する検証ではありません。

## ライセンス・報告

スキル・プロンプト・コードは [MIT License](LICENSE) です。
参考サイトのHTML・画像・フォント・ロゴ等へ、このMITが適用されるわけではありません。
自分が利用できる素材を使い、素材ごとの利用条件と必要な著作権表示を保持してください。

改善提案は [Issues](https://github.com/SHOGO391/skills/issues) へ。
秘密情報や顧客の非公開HTMLは投稿せず、セキュリティ上の問題は [SECURITY.md](SECURITY.md) を参照してください。
