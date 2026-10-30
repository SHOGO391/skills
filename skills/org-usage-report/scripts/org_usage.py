#!/usr/bin/env python3
"""Collect Claude token usage for a whole organization and render one report.

Sources (each writes the same JSONL rows, so they can be mixed):
  fetch          Claude Enterprise Usage and Cost Analytics API (needs the anthropic SDK)
  otel           OTLP/JSON files written by an OpenTelemetry Collector from Claude Code
  collect-local  one member's ~/.claude/projects transcripts, totals only (no prompt text)
Then:
  report         merge rows into report.html and summary.json
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import hashlib
import hmac
import html
import json
import os
from pathlib import Path
import secrets
import sys

TOKEN_KEYS = ("input", "output", "cache_read", "cache_write")
API_EARLIEST = date(2026, 1, 1)  # the analytics endpoints reject earlier dates
API_WINDOW_DAYS = 31  # one request may span at most 31 days


def usage_row(**fields):
    row = {"kind": "usage", "source": "", "day": None, "person": None, "team": None,
           "product": None, "model": None, "project": None,
           "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
           "cost_usd": None, "requests": None}
    row.update(fields)
    return row


def total_tokens(row):
    return sum(int(row.get(key) or 0) for key in TOKEN_KEYS)


def parse_since(text, today=None):
    """'7d' / '24h' / 'YYYY-MM-DD' / ISO 8601 -> aware datetime in UTC."""
    now = today or datetime.now(timezone.utc)
    if text.endswith("d") and text[:-1].isdigit():
        return now - timedelta(days=int(text[:-1]))
    if text.endswith("h") and text[:-1].isdigit():
        return now - timedelta(hours=int(text[:-1]))
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def keyed_hash(prefix, text, salt):
    """Pseudonym that cannot be reversed by hashing guesses without the salt."""
    return prefix + hmac.new(salt.encode(), text.encode(), hashlib.sha256).hexdigest()[:10]


def pseudonym_salt():
    salt = os.environ.get("ORG_USAGE_SALT")
    if not salt:
        print("ORG_USAGE_SALT が未設定なので、今回だけの仮名にします（回をまたいで同じ仮名にはなりません）", file=sys.stderr)
        salt = secrets.token_hex(16)
    return salt


def write_jsonl(rows, out):
    stream = open(out, "w", encoding="utf-8", newline="\n") if out != "-" else sys.stdout
    try:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    finally:
        if stream is not sys.stdout:
            stream.close()


# ---------------------------------------------------------------- analytics API

def actor_label(actor):
    if not actor:
        return None
    for key in ("email_address", "name", "api_key_name", "service_account_name", "user_id", "id"):
        if actor.get(key):
            return str(actor[key])
    return actor.get("type")


def api_windows(start, end, today=None):
    """Split [start, end) into whole-day windows the API accepts."""
    oldest = (today or datetime.now(timezone.utc)).date() - timedelta(days=364)
    start = max(start.date(), API_EARLIEST, oldest)
    end_day = end.date()
    while start < end_day:
        stop = min(start + timedelta(days=API_WINDOW_DAYS), end_day)
        yield start, stop
        start = stop


def to_dict(item):
    if hasattr(item, "to_dict"):
        return item.to_dict()
    return item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)


def rows_from_usage_items(items, cost_items=()):
    """Join usage and cost rows that share (day, actor, product, model)."""
    def key(item):
        group = item.get("group") or {}
        return (str(item.get("starting_at"))[:10], actor_label(group.get("actor")),
                group.get("product"), group.get("model"))

    costs = defaultdict(float)
    for item in cost_items:
        costs[key(item)] += float((item.get("cost") or {}).get("amount") or 0) / 100  # cents -> USD
    rows = []
    for item in items:
        usage = item.get("usage") or {}
        cache_write = usage.get("cache_creation") or {}
        day, person, product, model = key(item)
        cost = costs.pop((day, person, product, model), 0.0)  # attach once even if a key repeats
        rows.append(usage_row(
            source="analytics_api", day=day, person=person, product=product, model=model,
            input=usage.get("uncached_input_tokens") or 0,
            output=usage.get("output_tokens") or 0,
            cache_read=usage.get("cache_read_input_tokens") or 0,
            cache_write=sum(int(v or 0) for v in cache_write.values()),
            cost_usd=round(cost, 6) if cost_items else None,
            requests=usage.get("requests")))
    return rows


def rows_from_team_items(items):
    rows = []
    for item in items:
        usage = item.get("usage") or {}
        group = item.get("group") or {}
        rows.append({"kind": "team", "source": "analytics_api",
                     "team": group.get("rbac_group_id") or "(no group)",
                     "tokens": int(usage.get("total_tokens") or 0)})
    return rows


def rows_from_skill_items(items):
    return [{"kind": "skill", "source": "analytics_api", "skill": item.get("skill_name"),
             "invocations": item.get("invocation_count") or 0,
             "users": item.get("distinct_user_count") or 0,
             "list_price_usd": item.get("attributed_list_price")}
            for item in items]


def cmd_fetch(args):
    try:
        import anthropic
    except ImportError:
        sys.exit("anthropic SDK が必要です: pip install anthropic")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY に read:analytics 権限のキーを設定してください（値は表示しません）")
    client = anthropic.Anthropic()
    analytics = client.beta.organization.analytics
    start = parse_since(args.since)
    end = parse_since(args.until) if args.until else datetime.now(timezone.utc)
    windows = list(api_windows(start, end))
    if not windows:
        sys.exit("取れる期間がありません。API は日単位で、2026-01-01 以降・365 日以内・今日より前の日だけ返します")
    rows = []
    for lo, hi in windows:
        span = {"starting_at": f"{lo}T00:00:00Z", "ending_at": f"{hi}T00:00:00Z",
                "time_bucket_width": "1d", "limit": 1000}
        dims = ["actor", "product", "model"]
        usage = [to_dict(x) for x in analytics.usage.list(group_by=dims, **span)]
        cost = [to_dict(x) for x in analytics.cost.list(group_by=dims, **span)] if not args.no_cost else ()
        rows += rows_from_usage_items(usage, cost)
        if args.teams:
            whole = {"starting_at": span["starting_at"], "ending_at": span["ending_at"], "limit": 1000}
            rows += rows_from_team_items(to_dict(x) for x in analytics.usage.list(group_by=["rbac_group_id"], **whole))
        if args.skills:
            rows += rows_from_skill_items(
                to_dict(x) for x in analytics.skills.list(starting_date=str(lo), ending_date=str(hi), limit=1000))
        print(f"{lo}..{hi}: {len(rows)} rows so far", file=sys.stderr)
    write_jsonl(rows, args.out)


# ---------------------------------------------------------------- OpenTelemetry

OTEL_TYPES = {"input": "input", "output": "output", "cacheRead": "cache_read", "cacheCreation": "cache_write"}


def otel_attrs(items):
    out = {}
    for item in items or []:
        value = item.get("value") or {}
        for kind in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if kind in value:
                out[item.get("key")] = value[kind]
    return out


def otel_number(point):
    return float(point.get("asDouble", point.get("asInt", 0)) or 0)


def rows_from_otel(payloads, team_attr):
    """Claude Code metrics: claude_code.token.usage (tokens) and claude_code.cost.usage (USD).

    Delta points are summed. Cumulative points are running totals, so each point
    contributes only its increase over the previous point of the same series,
    on the day that point was taken.
    """
    delta = defaultdict(float)
    series_points = defaultdict(dict)
    for payload in payloads:
        for resource in payload.get("resourceMetrics", []):
            base = otel_attrs((resource.get("resource") or {}).get("attributes"))
            for scope in resource.get("scopeMetrics", []):
                for metric in scope.get("metrics", []):
                    name = metric.get("name")
                    if name not in ("claude_code.token.usage", "claude_code.cost.usage"):
                        continue
                    data = metric.get("sum") or metric.get("gauge") or {}
                    cumulative = data.get("aggregationTemporality") in (2, "AGGREGATION_TEMPORALITY_CUMULATIVE")
                    for point in data.get("dataPoints", []):
                        attrs = {**base, **otel_attrs(point.get("attributes"))}
                        field = "cost_usd" if name == "claude_code.cost.usage" else OTEL_TYPES.get(attrs.get("type"))
                        if not field:
                            continue
                        stamp = int(point.get("timeUnixNano") or 0)
                        day = datetime.fromtimestamp(stamp / 1e9, timezone.utc).date().isoformat() if stamp else None
                        who = (attrs.get("user.email") or attrs.get("user.account_uuid"),
                               attrs.get(team_attr), attrs.get("model"), field)
                        if cumulative:
                            series = (who, attrs.get("session.id"), point.get("startTimeUnixNano"))
                            series_points[series][stamp] = (day, otel_number(point))
                        else:
                            delta[(day, *who)] += otel_number(point)
    for (who, _, _), points in series_points.items():
        previous = 0.0
        for stamp in sorted(points):
            day, value = points[stamp]
            delta[(day, *who)] += value - previous if value >= previous else value  # a drop means a reset
            previous = value
    merged = {}
    for (day, person, team, model, field), value in delta.items():
        row = merged.setdefault((day, person, team, model), usage_row(
            source="otel", day=day, person=person, team=team, product="claude_code", model=model))
        if field == "cost_usd":
            row["cost_usd"] = round((row["cost_usd"] or 0) + value, 6)
        else:
            row[field] += int(value)
    return list(merged.values())


def read_otel_files(paths):
    for path in paths:
        text = Path(path).read_text(encoding="utf-8")
        try:
            yield json.loads(text)
        except json.JSONDecodeError:  # the Collector file exporter writes one request per line
            for line in text.splitlines():
                if line.strip():
                    yield json.loads(line)


def cmd_otel(args):
    write_jsonl(rows_from_otel(read_otel_files(args.files), args.team_attr), args.out)


# ---------------------------------------------------------------- local transcripts

def project_label(path, root, salt):
    name = path.relative_to(root).parts[0]
    return keyed_hash("p-", name, salt) if salt else name


def rows_from_transcripts(root, since, person, team, salt):
    """salt=None keeps folder names; otherwise they become keyed pseudonyms."""
    """Totals per (day, project, model) plus skill and subagent call counts.

    A response is written as several lines that share one requestId; only the
    line with the largest output_tokens is counted. Prompt text is never read out.
    """
    best = {}
    counts = defaultdict(int)
    seen_tools = set()
    for path in sorted(Path(root).rglob("*.jsonl")):
        if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < since:
            continue
        project = project_label(path, Path(root), salt)
        with open(path, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = entry.get("message") if isinstance(entry, dict) else None
                if entry.get("type") != "assistant" or not isinstance(message, dict):
                    continue
                stamp = entry.get("timestamp")
                try:
                    if datetime.fromisoformat(stamp.replace("Z", "+00:00")) < since:
                        continue
                except (AttributeError, TypeError, ValueError):
                    continue
                for block in message.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id") not in seen_tools:
                        seen_tools.add(block.get("id"))
                        tool_input = block.get("input") or {}
                        if block.get("name") == "Skill" and tool_input.get("skill"):
                            counts[("skill", str(tool_input["skill"]))] += 1
                        elif block.get("name") in ("Agent", "Task"):
                            counts[("subagent", str(tool_input.get("subagent_type") or "general-purpose"))] += 1
                usage = message.get("usage")
                key = entry.get("requestId") or message.get("id")
                if not usage or not key:
                    continue
                if key not in best or (usage.get("output_tokens") or 0) >= (best[key][3].get("output_tokens") or 0):
                    best[key] = (stamp[:10], project, message.get("model"), usage)
    merged = {}
    for day, project, model, usage in best.values():
        if model == "<synthetic>":
            continue
        row = merged.setdefault((day, project, model), usage_row(
            source="local", day=day, person=person, team=team, product="claude_code",
            model=model, project=project, requests=0))
        row["input"] += usage.get("input_tokens") or 0
        row["output"] += usage.get("output_tokens") or 0
        row["cache_read"] += usage.get("cache_read_input_tokens") or 0
        row["cache_write"] += usage.get("cache_creation_input_tokens") or 0
        row["requests"] += 1
    rows = list(merged.values())
    rows += [{"kind": kind, "source": "local", "person": person, "team": team,
              "skill" if kind == "skill" else "subagent": name, "invocations": n}
             for (kind, name), n in sorted(counts.items())]
    return rows


def cmd_collect_local(args):
    root = Path(args.dir).expanduser()
    if not root.is_dir():
        sys.exit(f"会話記録のフォルダがありません: {root}")
    salt = None if args.show_projects else pseudonym_salt()
    rows = rows_from_transcripts(root, parse_since(args.since), args.person, args.team, salt)
    write_jsonl(rows, args.out)


# ---------------------------------------------------------------- report

def read_rows(paths):
    for path in paths:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield json.loads(line)


def summarize(rows, salt=None, top=15):
    """salt=None keeps names; otherwise people become keyed pseudonyms."""
    usage = [r for r in rows if r.get("kind") == "usage"]
    if salt:
        for row in rows:
            if row.get("person"):
                row["person"] = keyed_hash("u-", row["person"], salt)
    grand = sum(total_tokens(r) for r in usage)

    def table(field):
        acc = defaultdict(lambda: {"tokens": 0, "cost_usd": None, "cache_read": 0, "input_side": 0, "requests": 0})
        for row in usage:
            slot = acc[row.get(field) or "(不明)"]
            slot["tokens"] += total_tokens(row)
            slot["cache_read"] += row["cache_read"]
            slot["input_side"] += row["input"] + row["cache_read"] + row["cache_write"]
            slot["requests"] += row.get("requests") or 0
            if row.get("cost_usd") is not None:
                slot["cost_usd"] = (slot["cost_usd"] or 0) + row["cost_usd"]
        out = []
        for name, slot in sorted(acc.items(), key=lambda kv: -kv[1]["tokens"]):
            out.append({"name": name, "tokens": slot["tokens"],
                        "share": slot["tokens"] / grand if grand else 0,
                        "cost_usd": round(slot["cost_usd"], 2) if slot["cost_usd"] is not None else None,
                        "cache_hit": slot["cache_read"] / slot["input_side"] if slot["input_side"] else None,
                        "requests": slot["requests"] or None})
        return out

    def counts(kind):
        # Local rows name the person; API rows carry a per-window user count,
        # which cannot be de-duplicated across windows, so the largest is shown.
        acc = defaultdict(lambda: {"invocations": 0, "people": set(), "api_users": 0})
        for row in rows:
            if row.get("kind") == kind:
                slot = acc[row.get(kind)]
                slot["invocations"] += row.get("invocations") or 0
                if row.get("person"):
                    slot["people"].add(row["person"])
                slot["api_users"] = max(slot["api_users"], row.get("users") or 0)
        return [{"name": k, "invocations": v["invocations"], "people": max(len(v["people"]), v["api_users"])}
                for k, v in sorted(acc.items(), key=lambda kv: -kv[1]["invocations"])]

    teams = defaultdict(int)
    for row in rows:
        if row.get("kind") == "team":
            teams[row["team"]] += row["tokens"]
    days = sorted({r["day"] for r in usage if r.get("day")})
    costs = [r["cost_usd"] for r in usage if r.get("cost_usd") is not None]
    input_side = sum(r["input"] + r["cache_read"] + r["cache_write"] for r in usage)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "period": {"from": days[0] if days else None, "to": days[-1] if days else None},
        "sources": sorted({r.get("source") for r in rows}),
        "overall": {"tokens": grand, **{k: sum(r[k] for r in usage) for k in TOKEN_KEYS},
                    "cost_usd": round(sum(costs), 2) if costs else None,
                    "cache_hit": sum(r["cache_read"] for r in usage) / input_side if input_side else None,
                    "people": len({r.get("person") for r in usage} - {None})},
        "by_person": table("person")[:top],
        "by_team": table("team")[:top] if any(r.get("team") for r in usage) else [],
        "by_rbac_group": [{"name": k, "tokens": v} for k, v in sorted(teams.items(), key=lambda kv: -kv[1])],
        "by_product": table("product"),
        "by_model": table("model"),
        "by_project": table("project")[:top] if any(r.get("project") for r in usage) else [],
        "by_day": sorted(table("day"), key=lambda r: r["name"]),
        "skills": counts("skill")[:top],
        "subagents": counts("subagent")[:top],
    }
    summary["signals"] = signals(summary)
    return summary


def signals(summary):
    """Rule-based candidates for the findings; the agent picks and words them."""
    out = []
    people = summary["by_person"]
    # Heavy use is not misuse, so a person is never flagged "bad" for volume alone.
    if len(people) >= 3 and people[0]["share"] >= 0.25:
        out.append({"level": "info", "fig": f"{people[0]['share']:.0%}",
                    "text": f"{people[0]['name']} の分が全体の {people[0]['share']:.0%}"})
    for row in people[:5]:
        if row["cache_hit"] is not None and row["cache_hit"] < 0.7 and row["share"] >= 0.05:
            out.append({"level": "bad", "fig": f"{row['cache_hit']:.0%}",
                        "text": f"{row['name']} の使い方はキャッシュの再利用が少ない（{row['cache_hit']:.0%}）"})
    for row in summary["by_model"][:3]:
        if row["share"] >= 0.5:
            out.append({"level": "info", "fig": f"{row['share']:.0%}", "text": f"{row['name']} が全体の {row['share']:.0%}"})
    hit = summary["overall"]["cache_hit"]
    if hit is not None:
        out.append({"level": "good" if hit >= 0.85 else "bad", "fig": f"{hit:.0%}",
                    "text": f"組織全体のキャッシュ再利用率 {hit:.0%}"})
    return out


CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a65;--line:#e4e1d8;--bar:#3d6fb6;--bad:#b3412f;--good:#2f7d4f;--info:#3d6fb6;--card:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#18181a;--fg:#ecebe6;--muted:#a3a29c;--line:#34343a;--bar:#7aa5e6;--bad:#e88472;--good:#79c495;--info:#7aa5e6;--card:#212125}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 system-ui,-apple-system,"Hiragino Sans","Yu Gothic UI",sans-serif}
main{max-width:1040px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:32px 0 8px}
.meta{color:var(--muted)}.hero{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:20px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}.tile b{display:block;font-size:20px}
.take{display:flex;gap:12px;align-items:baseline;padding:6px 0;border-bottom:1px solid var(--line)}.fig{min-width:64px;font-weight:700}
.bad .fig{color:var(--bad)}.good .fig{color:var(--good)}.info .fig{color:var(--info)}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;background:var(--card)}th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}th{color:var(--muted);font-weight:600}.bar{display:inline-block;height:8px;background:var(--bar);border-radius:2px;vertical-align:middle}
.note{color:var(--muted);font-size:12px}
"""


def fmt_tokens(n):
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return str(n)


def render_table(rows, cols):
    if not rows:
        return '<p class="note">データなし</p>'
    head = "".join(f"<th>{html.escape(label)}</th>" for label, _ in cols)
    body = []
    for row in rows:
        body.append("<tr>" + "".join(f"<td>{cell(row)}</td>" for _, cell in cols) + "</tr>")
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def usage_cols(label):
    pct = lambda v: "—" if v is None else f"{v:.0%}"
    return [(label, lambda r: html.escape(str(r["name"]))),
            ("トークン", lambda r: fmt_tokens(r["tokens"])),
            ("割合", lambda r: f'<span class="bar" style="width:{max(2, int(r["share"] * 120))}px"></span> {r["share"]:.1%}'),
            ("費用 (USD)", lambda r: "—" if r["cost_usd"] is None else f'{r["cost_usd"]:,.2f}'),
            ("キャッシュ再利用", lambda r: pct(r["cache_hit"]))]


def render_html(summary, title, notes):
    o = summary["overall"]
    tiles = [("合計トークン", fmt_tokens(o["tokens"])), ("人数", str(o["people"])),
             ("費用 (USD)", "—" if o["cost_usd"] is None else f'{o["cost_usd"]:,.2f}'),
             ("キャッシュ再利用", "—" if o["cache_hit"] is None else f'{o["cache_hit"]:.0%}')]
    takes = notes if notes is not None else summary["signals"]
    take_html = "".join(
        f'<div class="take {html.escape(t.get("level", "info"))}"><div class="fig">{html.escape(t.get("fig", ""))}</div>'
        f'<div>{html.escape(t.get("text", ""))}</div></div>' for t in takes)
    count_cols = [("名前", lambda r: html.escape(str(r["name"]))), ("呼び出し", lambda r: f'{r["invocations"]:,}'),
                  ("人数", lambda r: str(r["people"]))]
    sections = [("気づいたこと", take_html or '<p class="note">なし</p>'),
                ("人別", render_table(summary["by_person"], usage_cols("人"))),
                ("チーム別（集計元の属性）", render_table(summary["by_team"], usage_cols("チーム"))),
                ("グループ別（Enterprise のグループ）",
                 render_table(summary["by_rbac_group"], [("グループ", lambda r: html.escape(str(r["name"]))),
                                                         ("トークン", lambda r: fmt_tokens(r["tokens"]))])
                 + '<p class="note">複数のグループに入っている人の分はそれぞれに数えるので、合計は全体を超えることがあります。</p>'),
                ("製品別", render_table(summary["by_product"], usage_cols("製品"))),
                ("モデル別", render_table(summary["by_model"], usage_cols("モデル"))),
                ("プロジェクト別", render_table(summary["by_project"], usage_cols("プロジェクト"))),
                ("日別", render_table(summary["by_day"], usage_cols("日付"))),
                ("スキル", render_table(summary["skills"], count_cols)),
                ("サブエージェント", render_table(summary["subagents"], count_cols))]
    body = "".join(f"<h2>{html.escape(h)}</h2>{content}" for h, content in sections
                   if "データなし" not in content or h in ("人別", "製品別"))
    period = summary["period"]
    return (f'<!doctype html><html lang="ja"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>'
            f"<style>{CSS}</style></head><body><main><h1>{html.escape(title)}</h1>"
            f'<p class="meta">{html.escape(str(period["from"]))} 〜 {html.escape(str(period["to"]))}（UTC）'
            f' ・ 集計元: {html.escape(", ".join(summary["sources"]))}</p>'
            f'<div class="hero">{"".join(f"<div class=tile>{html.escape(k)}<b>{html.escape(v)}</b></div>" for k, v in tiles)}</div>'
            f"{body}<p class=\"note\">トークン = 入力 + 出力 + キャッシュ読み + キャッシュ書き。費用は集計元が返した額だけを表示し、推定はしません。</p>"
            f"</main></body></html>")


def cmd_report(args):
    rows = list(read_rows(args.rows))
    summary = summarize(rows, salt=pseudonym_salt() if args.anonymize else None, top=args.top)
    notes = json.loads(Path(args.notes).read_text(encoding="utf-8")) if args.notes else None
    if notes is not None and not (isinstance(notes, list) and all(isinstance(n, dict) for n in notes)):
        sys.exit("--notes は [{\"level\": ..., \"fig\": ..., \"text\": ...}] の形の JSON にしてください")
    Path(args.out).write_text(render_html(summary, args.title, notes), encoding="utf-8")
    if args.summary:
        Path(args.summary).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{args.out}: {summary['overall']['people']} 人・{fmt_tokens(summary['overall']['tokens'])} トークン")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("fetch", help="Enterprise の分析 API から取る")
    p.add_argument("--since", default="7d")
    p.add_argument("--until")
    p.add_argument("--teams", action="store_true", help="Enterprise のグループ別も取る")
    p.add_argument("--skills", action="store_true", help="スキル別の利用も取る")
    p.add_argument("--no-cost", action="store_true")
    p.add_argument("--out", default="rows-api.jsonl")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("otel", help="OpenTelemetry Collector の JSON を読む")
    p.add_argument("files", nargs="+")
    p.add_argument("--team-attr", default="department", help="チームとして使う属性名")
    p.add_argument("--out", default="rows-otel.jsonl")
    p.set_defaults(func=cmd_otel)

    p = sub.add_parser("collect-local", help="自分の PC の会話記録を合計だけにする")
    p.add_argument("--dir", default="~/.claude/projects")
    p.add_argument("--since", default="7d")
    p.add_argument("--person", required=True, help="報告に出す名前（メールでも可）")
    p.add_argument("--team")
    p.add_argument("--show-projects", action="store_true", help="プロジェクト名を伏せずに出す（既定は仮名）")
    p.add_argument("--out", default="rows-local.jsonl")
    p.set_defaults(func=cmd_collect_local)

    p = sub.add_parser("report", help="行をまとめて HTML にする")
    p.add_argument("rows", nargs="+")
    p.add_argument("--out", default="org-usage-report.html")
    p.add_argument("--summary", default="org-usage-summary.json")
    p.add_argument("--notes", help="気づいたことの JSON（[{level, fig, text}]）")
    p.add_argument("--title", default="Claude 使用量レポート")
    p.add_argument("--anonymize", action="store_true", help="人の名前を仮名にする")
    p.add_argument("--top", type=int, default=15)
    p.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
