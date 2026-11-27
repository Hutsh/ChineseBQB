from pathlib import Path
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import analyze_stickers_cloud as cloud


class StickerCloudLimitTests(unittest.TestCase):
    def test_style_backfill_preserves_prior_analysis_and_makes_progress(self):
        before = {"id": "a", "analysis_status": "visually_reviewed", "analysis": {"keep": True},
                  "selection_profile": {"occasion": "general"}, "evidence": {"source": "original"}}
        item = {"id": "a", "selection_profile": {"expression_styles": ["stoic"], "cuteness": 0}}
        accepted, issue = cloud.parse_response(json.dumps({"items": [item]}), {"a"}, style_only=True)
        self.assertFalse(issue)
        options = SimpleNamespace(style_only=True)
        self.assertFalse(cloud.reviewed_for_run(before, options))
        cloud.publish_style(before, accepted["a"])
        self.assertTrue(cloud.reviewed_for_run(before, options))
        self.assertEqual(before["analysis"], {"keep": True})
        self.assertEqual(before["evidence"], {"source": "original"})
        self.assertEqual(before["selection_profile"], {"occasion": "general", **item["selection_profile"]})

    def test_usage_limit_carries_provider_reset_time(self):
        with patch.object(cloud.time, "time", return_value=1_800_000_000):
            with self.assertRaises(cloud.CloudLimit) as caught:
                cloud.summarize_cli_error(
                    b'ERROR: {"type":"usage_limit_reached","resets_at":1800000360,"resets_in_seconds":120}'
                )
        self.assertTrue(caught.exception.quota)
        self.assertEqual(caught.exception.retry_at, 1_800_000_360)

    def test_http_rate_limit_carries_retry_after(self):
        with patch.object(cloud.time, "time", return_value=1_800_000_000):
            with self.assertRaises(cloud.CloudLimit) as caught:
                cloud.summarize_cli_error(b"HTTP status 429\nRetry-After: 90")
        self.assertFalse(caught.exception.quota)
        self.assertEqual(caught.exception.retry_at, 1_800_000_090)

    def test_account_and_billing_errors_are_not_automatically_retried(self):
        for diagnostic in (b"ERROR: 403 Forbidden", b"HTTP 429 ERROR: insufficient_quota"):
            with self.subTest(diagnostic=diagnostic):
                with self.assertRaises(cloud.CloudStop) as caught:
                    cloud.summarize_cli_error(diagnostic)
                self.assertNotIsInstance(caught.exception, cloud.CloudLimit)

    def test_usage_limit_without_reset_retries_one_image_without_repeating_done_work(self):
        rows = [{"id": name, "analysis_status": "pending"} for name in ("a", "b", "c")]
        groups = [[row] for row in rows]
        calls = []

        def run(_executable, _options, _rows, current):
            calls.append([group[0]["id"] for group in current])
            if len(calls) == 1:
                rows[0]["analysis_status"] = "visually_reviewed"
                raise cloud.CloudLimit(quota=True)
            if len(calls) == 2:
                raise cloud.CloudLimit(quota=True)
            for group in current:
                group[0]["analysis_status"] = "visually_reviewed"

        with patch.object(cloud, "run", side_effect=run), patch.object(cloud, "wait_until") as wait:
            with patch.object(cloud.time, "time", return_value=1_800_000_000):
                cloud.run_with_limit_wait(None, SimpleNamespace(wait_for_limit=True), rows, groups)

        self.assertEqual(calls, [["a", "b", "c"], ["b"], ["b"], ["c"]])
        self.assertEqual([call.args[0] for call in wait.call_args_list], [1_800_001_800, 1_800_003_600])

    def test_default_behavior_stops_on_limit(self):
        row = {"id": "a", "analysis_status": "pending"}
        with patch.object(cloud, "run", side_effect=cloud.CloudLimit()), patch.object(cloud, "wait_until") as wait:
            with self.assertRaises(cloud.CloudLimit):
                cloud.run_with_limit_wait(None, SimpleNamespace(wait_for_limit=False), [row], [[row]])
        wait.assert_not_called()


if __name__ == "__main__":
    unittest.main()
