# 集計元の導入

## A. Claude Enterprise の分析 API

1. 組織のプライマリオーナーが claude.ai の管理画面で、`read:analytics` 権限だけを付けたキーを発行する。
2. キーは組織の秘密管理に保存し、集計する人の環境変数 `ANTHROPIC_API_KEY` へそこから読み込む。
3. `pip install anthropic` のあと `fetch` を実行する。

Claude Console（API）の組織で Admin API キーを持っている場合も、同じ `fetch` で取れることがある（費用と使用量の分析は Console では早期提供）。
取れない組織では 403 になるので、そのときは B か C を使う。

## B. Claude Code の OpenTelemetry

全員の Claude Code に、管理者のマネージド設定（`managed-settings.json` の `env`）で次を配る。

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.example.internal:4318",
    "OTEL_RESOURCE_ATTRIBUTES": "department=sales"
  }
}
```

`OTEL_RESOURCE_ATTRIBUTES` の `department` は部署ごとに変える。別の名前にしたら `otel --team-attr` にその名前を渡す。

Collector は OpenTelemetry Collector の contrib 版を使い、届いた指標をファイルに書かせる。

```yaml
receivers:
  otlp:
    protocols:
      http: {}
      grpc: {}
exporters:
  file:
    path: ./claude-metrics.json
service:
  pipelines:
    metrics:
      receivers: [otlp]
      exporters: [file]
```

すでに Prometheus や Datadog などへ送っている組織は、そちらの画面で見ればよく、このスキルの B は要らない。

## C. 各メンバーの会話記録

各自が自分の PC で `collect-local` を実行し、できた `rows-名前.jsonl` を集計担当に渡す。
会話の本文は入らない。プロジェクト名（フォルダ名）は既定で仮名になる。全員に同じ `ORG_USAGE_SALT` を配っておくと、回をまたいで同じ仮名になる。
Claude Code は既定で 30 日より古い会話記録を消すので、それより前の分は集計できない。
