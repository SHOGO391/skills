---
name: org-usage-report
description: 組織全体で Claude のトークンを誰が・どのチームが・どの製品やモデルで使っているかを集計し、1 枚の HTML レポートと要約 JSON にする。Claude Enterprise の使用量・費用の分析 API、Claude Code の OpenTelemetry、各メンバーの PC の会話記録（合計だけ）の 3 つの集計元を混ぜて使える。「組織のトークン使用量」「誰がどれだけ使っているか」「チーム別の Claude の費用」「Enterprise の使用状況レポート」「会社全体の Claude Code の消費」「トークンの使いすぎを見つけて」で使う。1 人の PC の分だけなら公式プラグインの session-report で足りる。
---

# org-usage-report — 組織の Claude トークン使用量レポート

個人の PC の会話記録を読む公式の `session-report` は、その人の分しか見えない。
このスキルは組織の全員分を同じ形の行（JSONL）に集めて、人別・チーム別・製品別・モデル別・日別の表と、
気づいたことの一覧を 1 枚の HTML にまとめる。スクリプトは `scripts/org_usage.py` の 1 本だけ。

## 手順

### 1. 集計元を決める

組織が持っているものを先に聞き、上から順に使える物を選ぶ。混ぜてもよい（同じ人を二重に数えないよう、同じ期間に A と B を重ねない）。

| 集計元 | 必要なもの | 取れるもの | 取れないもの |
| --- | --- | --- | --- |
| A. 分析 API（`fetch`） | Claude Enterprise と、`read:analytics` 権限のキー（プライマリオーナーが claude.ai で発行） | 全製品（Chat・Claude Code・Cowork など）の人別トークンと費用、グループ別、スキル別 | プロジェクト別、サブエージェント別 |
| B. OpenTelemetry（`otel`） | 全員の Claude Code から Collector へ送る設定（[導入手順](references/setup.md)） | Claude Code の人別・チーム別トークンと費用 | Chat など Claude Code 以外 |
| C. 各自の会話記録（`collect-local`） | 各メンバーが自分の PC で 1 コマンド実行して結果を提出 | Claude Code のプロジェクト別・スキル別・サブエージェント別 | 費用、Claude Code 以外 |

A のキーは秘密情報。環境変数 `ANTHROPIC_API_KEY` に、組織の秘密管理（パスワード管理ツールや CI の secret）から入れる。
キーの値をファイル・コマンド・チャットに書かない。

### 2. 行を集める

```sh
# A. 分析 API（anthropic SDK が必要: pip install anthropic）
python scripts/org_usage.py fetch --since 30d --teams --skills --out rows-api.jsonl

# B. Collector が書いた JSON（複数可）
python scripts/org_usage.py otel metrics-*.json --team-attr department --out rows-otel.jsonl

# C. 各メンバーが自分の PC で実行し、できたファイルを集計担当へ渡す
python scripts/org_usage.py collect-local --since 30d --person 名前またはメール --team 営業 --out rows-名前.jsonl
```

`--since` は `7d`・`24h`・`2026-10-01` の形。A は 2026-01-01 より前と 365 日より前は取れない（API の制限）。
C は会話の本文を読み出さず、日・プロジェクト・モデルごとの合計と、スキル・サブエージェントの呼び出し回数だけを書く。
プロジェクト名（会話記録のフォルダ名）は既定で仮名にする。伏せずに出すときだけ `--show-projects` を付ける。

仮名は、組織で決めた合言葉（環境変数 `ORG_USAGE_SALT`）を鍵にして作る。全員が同じ合言葉を使えば、回をまたいでも同じ仮名になる。
未設定なら実行ごとに使い捨ての合言葉を作るので、仮名は逆算できないが、前回のレポートとは突き合わせられない。
合言葉は秘密情報として扱い、ファイルやチャットに書かない。

### 3. レポートを作る

```sh
python scripts/org_usage.py report rows-*.jsonl --out org-usage-report.html --summary org-usage-summary.json
```

### 4. 気づいたことを書く

`org-usage-summary.json` の `signals` は機械的に拾った候補（使用量の偏り・キャッシュ再利用率の低さ・モデルの偏り）。
`overall`・`by_person`・`by_team`・`by_product`・`by_model`・`skills` も読んで 3〜5 件に絞り、次の形で `notes.json` に書く。

```json
[{"level": "bad", "fig": "41%", "text": "営業部の 3 人で全体の 41%。うち 9 割が claude-opus-5-5"}]
```

- `level` は `bad`（無駄・異常）・`good`（健全）・`info`（事実）の 3 つ。
- `fig` は短い数字 1 つ（割合・人数・倍率）。数字は summary にある値だけを使い、推定しない。
- `text` は 1 文で、人・チーム・製品・モデルの名前を入れる。直し方の提案は「この人のこの使い方をこう変える」まで具体的に書く。

`--notes notes.json` を足して `report` をもう一度実行すると、HTML の先頭に入る。

### 5. 渡す

保存先のパスを伝える。経営層や社外に渡すときは `--anonymize` で人の名前を仮名（`u-` で始まる記号）に替えて作り直す。
仮名は `ORG_USAGE_SALT` を知らないと元に戻せない。本人の特定が要るときは仮名にしていない版を使う。
集めた行のファイル（`rows-*.jsonl`）にはメールアドレスが入るので、レポートと一緒に配らない。

使用量が多いことは、それだけでは問題ではない。気づいたことは使い方（キャッシュの再利用・モデルの選び方・同じ作業の繰り返し）について書き、
個人を責める材料として出さない。人別の表を誰に見せるかは、組織の担当者に先に確認する。

## 数字の読み方

- トークン = 入力 + 出力 + キャッシュ読み + キャッシュ書き。キャッシュ読みは安いので、トークンの多さと費用の多さは一致しない。
- キャッシュ再利用率 = キャッシュ読み ÷（入力 + キャッシュ読み + キャッシュ書き）。会話を続けて使う人ほど高い。85% 未満なら、会話を頻繁に作り直していないか・長い会話を圧縮し続けていないかを疑う。
- 費用は集計元が返した額だけを出す。C には費用が無いので「—」になる。単価を掛けた推定はしない（契約で単価が違うため）。
- グループ別（A の `--teams`）は、複数のグループに入っている人の分をそれぞれに数える。合計は全体を超えることがある。
- A のスキル別の人数は期間を分けて取ったうちの最大で、期間全体の延べ人数ではない。

## 確かめたこと

- A は anthropic SDK 2.3.0 の `client.beta.organization.analytics`（`usage`・`cost`・`skills`）を使う。費用は「セントの小数」で返るので 100 で割って USD にしている。
- B は Claude Code の `claude_code.token.usage`（属性 `type` = input・output・cacheRead・cacheCreation）と `claude_code.cost.usage` を読む。差分（delta）は合計し、累積（cumulative）は同じ系列の前の点からの増えた分だけを、その点の日に数える。
- C は同じ応答が複数行に分かれて記録されるので、`requestId` ごとに出力トークンが最大の行だけを数える（公式 `session-report` と同じ方法）。
