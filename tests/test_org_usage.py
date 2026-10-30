import json
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "org-usage-report" / "scripts"))
import org_usage  # noqa: E402


def api_item(day, email, product, model, uncached, output, read, write_5m):
    return {"starting_at": f"{day}T00:00:00Z", "ending_at": f"{day}T00:00:00Z",
            "group": {"actor": {"type": "user_actor", "email_address": email, "user_id": "user_1", "deleted": False},
                      "product": product, "model": model},
            "usage": {"uncached_input_tokens": uncached, "output_tokens": output,
                      "cache_read_input_tokens": read, "total_tokens": uncached + output + read + write_5m,
                      "cache_creation": {"ephemeral_5m_input_tokens": write_5m, "ephemeral_1h_input_tokens": 0},
                      "requests": 3}}


def otel_payload(temporality, points):
    return {"resourceMetrics": [{
        "resource": {"attributes": [{"key": "department", "value": {"stringValue": "sales"}}]},
        "scopeMetrics": [{"metrics": [{"name": "claude_code.token.usage", "sum": {
            "aggregationTemporality": temporality,
            "dataPoints": [{"asDouble": value, "timeUnixNano": str(stamp), "startTimeUnixNano": "1",
                            "attributes": [{"key": "type", "value": {"stringValue": kind}},
                                           {"key": "model", "value": {"stringValue": "claude-opus-5-5"}},
                                           {"key": "user.email", "value": {"stringValue": "a@example.com"}},
                                           {"key": "session.id", "value": {"stringValue": "s1"}}]}
                           for kind, value, stamp in points]}}]}]}]}


DAY_NS = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp() * 1e9)


class AnalyticsApiTests(unittest.TestCase):
    def test_usage_and_cost_join(self):
        usage = [api_item("2026-10-01", "a@example.com", "claude_code", "claude-opus-5-5", 10, 20, 300, 40)]
        cost = [{"starting_at": "2026-10-01T00:00:00Z", "group": usage[0]["group"],
                 "cost": {"amount": "1234.5", "currency": "USD", "list_amount": "1234.5"}}]
        [row] = org_usage.rows_from_usage_items(usage, cost)
        self.assertEqual((row["person"], row["input"], row["output"], row["cache_read"], row["cache_write"]),
                         ("a@example.com", 10, 20, 300, 40))
        self.assertAlmostEqual(row["cost_usd"], 12.345)  # fractional cents -> USD
        self.assertEqual(org_usage.total_tokens(row), 370)

    def test_windows_respect_api_limits(self):
        start = datetime(2025, 12, 1, tzinfo=timezone.utc)
        end = datetime(2026, 3, 15, tzinfo=timezone.utc)
        windows = list(org_usage.api_windows(start, end))
        self.assertEqual(windows[0][0].isoformat(), "2026-01-01")
        self.assertEqual(windows[-1][1].isoformat(), "2026-03-15")
        self.assertTrue(all((hi - lo).days <= 31 for lo, hi in windows))

    def test_windows_stop_at_365_days_and_short_ranges(self):
        today = datetime(2027, 6, 1, tzinfo=timezone.utc)
        windows = list(org_usage.api_windows(datetime(2026, 1, 1, tzinfo=timezone.utc), today, today=today))
        self.assertEqual(windows[0][0].isoformat(), "2026-06-02")
        same_day = datetime(2026, 10, 30, 9, tzinfo=timezone.utc)
        self.assertEqual(list(org_usage.api_windows(same_day, same_day + timedelta(hours=3))), [])

    def test_parse_since_keeps_offset(self):
        self.assertEqual(org_usage.parse_since("2026-10-01T09:00:00+09:00"),
                         datetime(2026, 10, 1, 0, tzinfo=timezone.utc))


class OtelTests(unittest.TestCase):
    def test_delta_points_are_summed(self):
        rows = org_usage.rows_from_otel([otel_payload(1, [("input", 5, DAY_NS), ("input", 7, DAY_NS + 1),
                                                          ("cacheRead", 100, DAY_NS)])], "department")
        [row] = rows
        self.assertEqual((row["input"], row["cache_read"], row["team"]), (12, 100, "sales"))

    def test_cumulative_points_take_latest(self):
        rows = org_usage.rows_from_otel([otel_payload(2, [("output", 5, DAY_NS), ("output", 9, DAY_NS + 10)])],
                                        "department")
        self.assertEqual(rows[0]["output"], 9)

    def test_cumulative_across_days_counts_increase_once(self):
        day = 86400 * 10**9
        rows = org_usage.rows_from_otel([otel_payload(2, [("output", 100, DAY_NS), ("output", 150, DAY_NS + day)])],
                                        "department")
        by_day = {r["day"]: r["output"] for r in rows}
        self.assertEqual(by_day, {"2026-10-01": 100, "2026-10-02": 50})


class LocalTranscriptTests(unittest.TestCase):
    def test_dedupes_streamed_lines_and_counts_tools(self):
        lines = [
            {"type": "user", "timestamp": "2026-10-01T00:00:00Z", "message": {"content": "秘密のプロンプト"}},
            {"type": "assistant", "timestamp": "2026-10-01T00:00:01Z", "requestId": "r1",
             "message": {"id": "m1", "model": "claude-opus-5-5",
                         "content": [{"type": "tool_use", "id": "t1", "name": "Skill", "input": {"skill": "yomiyasu"}}],
                         "usage": {"input_tokens": 3, "output_tokens": 1, "cache_read_input_tokens": 50,
                                   "cache_creation_input_tokens": 10}}},
            {"type": "assistant", "timestamp": "2026-10-01T00:00:02Z", "requestId": "r1",
             "message": {"id": "m1", "model": "claude-opus-5-5",
                         "content": [{"type": "tool_use", "id": "t2", "name": "Agent",
                                      "input": {"subagent_type": "Explore"}}],
                         "usage": {"input_tokens": 3, "output_tokens": 8, "cache_read_input_tokens": 50,
                                   "cache_creation_input_tokens": 10}}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "C--work-secret-project" / "session.jsonl"
            path.parent.mkdir()
            path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines), encoding="utf-8")
            since = datetime(2026, 1, 1, tzinfo=timezone.utc)
            rows = org_usage.rows_from_transcripts(tmp, since, "me", "dev", salt="org-secret")
        usage = [r for r in rows if r["kind"] == "usage"]
        self.assertEqual(len(usage), 1)
        self.assertEqual((usage[0]["input"], usage[0]["output"], usage[0]["requests"]), (3, 8, 1))
        self.assertTrue(usage[0]["project"].startswith("p-"))
        self.assertNotIn("秘密", json.dumps(rows, ensure_ascii=False))
        self.assertNotIn("secret", json.dumps(rows, ensure_ascii=False))
        self.assertIn({"kind": "skill", "source": "local", "person": "me", "team": "dev",
                       "skill": "yomiyasu", "invocations": 1}, rows)
        self.assertEqual([r["subagent"] for r in rows if r["kind"] == "subagent"], ["Explore"])


class ReportTests(unittest.TestCase):
    def test_report_escapes_and_anonymizes(self):
        rows = org_usage.rows_from_usage_items([
            api_item("2026-10-01", "<b>a@example.com</b>", "claude_code", "claude-opus-5-5", 10, 20, 900, 40),
            api_item("2026-10-01", "b@example.com", "chat", "claude-sonnet-5-5", 10, 20, 30, 40),
        ])
        summary = org_usage.summarize(rows, salt="org-secret")
        self.assertEqual(summary["overall"]["people"], 2)
        self.assertTrue(all(r["name"].startswith("u-") for r in summary["by_person"]))
        page = org_usage.render_html(summary, "<script>x</script>", None)
        self.assertNotIn("<script>x", page)
        self.assertNotIn("example.com", page)

    def test_signal_flags_heavy_user(self):
        rows = org_usage.rows_from_usage_items([
            api_item("2026-10-01", "heavy@example.com", "claude_code", "m", 1000, 0, 0, 0),
            api_item("2026-10-01", "b@example.com", "claude_code", "m", 10, 0, 0, 0),
            api_item("2026-10-01", "c@example.com", "claude_code", "m", 10, 0, 0, 0),
        ])
        signals = org_usage.summarize(rows)["signals"]
        # Volume alone is reported as a fact, never as a fault.
        self.assertTrue(any("heavy@example.com" in s["text"] and s["level"] == "info" for s in signals))

    def test_pseudonym_needs_the_salt(self):
        import hashlib
        rows = org_usage.rows_from_usage_items([api_item("2026-10-01", "a@example.com", "chat", "m", 1, 1, 1, 1)])
        name = org_usage.summarize(rows, salt="org-secret")["by_person"][0]["name"]
        self.assertNotIn(hashlib.sha256(b"a@example.com").hexdigest()[:8], name)
        self.assertEqual(name, org_usage.keyed_hash("u-", "a@example.com", "org-secret"))


if __name__ == "__main__":
    unittest.main()
