"""line-adapter/app.py の単体テスト"""
import io
import json
import os
import unittest
from unittest.mock import MagicMock, patch

# app はモジュールレベルで os.environ を参照するため、インポート前に設定する
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "test-token")
os.environ.setdefault("LINE_USER_ID", "Utest000")

from app import (  # noqa: E402
    MAX_BODY_SIZE,
    MAX_MESSAGE_LENGTH,
    WebhookHandler,
    format_message,
    format_notification,
    send_line_message,
)

SAMPLE_ALERT = {
    "status": "firing",
    "labels": {"alertname": "SpeedtestJobFailed", "job_name": "speedtest-123"},
    "annotations": {
        "summary": "速度計測ジョブが失敗しました",
        "description": "speedtest-123 が計測エラーまたはデータ送信エラーで失敗しました。",
        "action": "kubectl logs -n monitoring job/speedtest-123 でログを確認してください。",
    },
    # Alertmanager は UTC・ナノ秒精度で送ってくる
    "startsAt": "2026-10-30T08:30:00.123456789Z",
    "endsAt": "0001-01-01T00:00:00Z",
}

RESOLVED_ALERT = {
    **SAMPLE_ALERT,
    "status": "resolved",
    "endsAt": "2026-10-30T09:35:00Z",
}


def make_handler(body_dict: dict | None = None, *, raw_body: bytes | None = None) -> WebhookHandler:
    """WebhookHandler をモックリクエストで生成する。raw_body を指定すると生バイト列を使う。"""
    body = raw_body if raw_body is not None else json.dumps(body_dict).encode()
    handler = WebhookHandler.__new__(WebhookHandler)
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)
    handler.send_response = MagicMock()
    handler.end_headers = MagicMock()
    return handler


def make_get_handler(path: str) -> WebhookHandler:
    """GET リクエスト用の WebhookHandler をモックで生成する"""
    handler = WebhookHandler.__new__(WebhookHandler)
    handler.path = path
    handler.send_response = MagicMock()
    handler.end_headers = MagicMock()
    return handler


class TestFormatMessage(unittest.TestCase):

    def test_firing_message(self):
        """正常系: 発生通知は見出し・内容・JST の発生時刻・対処を含む"""
        result = format_message(SAMPLE_ALERT)
        self.assertEqual(
            result,
            "🔴【発生】速度計測ジョブが失敗しました\n"
            "speedtest-123 が計測エラーまたはデータ送信エラーで失敗しました。\n"
            "発生時刻: 10/30 17:30\n"
            "対処: kubectl logs -n monitoring job/speedtest-123 でログを確認してください。",
        )

    def test_resolved_message(self):
        """正常系: 解消通知は見出しと発生〜解消の期間のみを含む"""
        result = format_message(RESOLVED_ALERT)
        self.assertEqual(
            result,
            "✅【解消】速度計測ジョブが失敗しました\n"
            "期間: 10/30 17:30 〜 18:35（1時間5分）",
        )

    def test_resolved_duration_under_one_hour(self):
        """正常系: 1 時間未満の期間は分のみで表示する"""
        alert = {**RESOLVED_ALERT, "endsAt": "2026-10-30T09:00:00Z"}
        self.assertIn("（30分）", format_message(alert))

    def test_falls_back_to_alertname_without_summary(self):
        """正常系: summary がない場合は alertname を見出しに使う"""
        alert = {**SAMPLE_ALERT, "annotations": {}}
        result = format_message(alert)
        self.assertEqual(result, "🔴【発生】SpeedtestJobFailed\n発生時刻: 10/30 17:30")

    def test_invalid_starts_at_is_omitted(self):
        """正常系: startsAt が不正な場合は発生時刻を表示しない"""
        alert = {**SAMPLE_ALERT, "startsAt": "invalid"}
        self.assertNotIn("発生時刻", format_message(alert))

    def test_empty_alert(self):
        """正常系: labels・annotations・時刻がなくても例外が発生しない"""
        result = format_message({"status": "firing"})
        self.assertEqual(result, "🔴【発生】不明なアラート")


class TestFormatNotification(unittest.TestCase):

    def test_joins_alerts_with_firing_first(self):
        """正常系: 複数アラートを 1 通にまとめ、発生中を先に表示する"""
        result = format_notification([RESOLVED_ALERT, SAMPLE_ALERT])
        self.assertLess(result.index("【発生】"), result.index("【解消】"))
        self.assertIn("\n\n", result)

    def test_truncates_long_message(self):
        """異常系: LINE の上限文字数を超える場合は省略する"""
        result = format_notification([SAMPLE_ALERT] * 100)
        self.assertEqual(len(result), MAX_MESSAGE_LENGTH)
        self.assertTrue(result.endswith("…（以下省略）"))


class TestSendLineMessage(unittest.TestCase):

    @patch("app.requests.post")
    def test_sends_to_correct_endpoint(self, mock_post):
        """正常系: LINE API の正しいエンドポイントへ送信する"""
        mock_post.return_value.raise_for_status = MagicMock()
        send_line_message("テストメッセージ")
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "https://api.line.me/v2/bot/message/push")
        self.assertEqual(kwargs["json"]["messages"][0]["text"], "テストメッセージ")

    @patch("app.requests.post")
    def test_raises_on_http_error(self, mock_post):
        """異常系: HTTP エラー時に例外が送出される"""
        mock_post.return_value.raise_for_status.side_effect = Exception("403 Forbidden")
        with self.assertRaises(Exception):
            send_line_message("テスト")


class TestWebhookHandler(unittest.TestCase):

    @patch("app.send_line_message")
    def test_successful_webhook_returns_200(self, mock_send):
        """正常系: アラートを受信して 200 を返す"""
        handler = make_handler({"alerts": [SAMPLE_ALERT]})
        handler.do_POST()
        mock_send.assert_called_once()
        handler.send_response.assert_called_once_with(200)

    @patch("app.send_line_message")
    def test_empty_alerts_returns_200(self, mock_send):
        """正常系: alerts が空でも 200 を返し LINE 送信は行わない"""
        handler = make_handler({"alerts": []})
        handler.do_POST()
        mock_send.assert_not_called()
        handler.send_response.assert_called_once_with(200)

    @patch("app.send_line_message")
    def test_multiple_alerts_sends_once(self, mock_send):
        """正常系: 複数アラートは 1 通にまとめて LINE 送信する"""
        handler = make_handler({"alerts": [SAMPLE_ALERT, RESOLVED_ALERT]})
        handler.do_POST()
        mock_send.assert_called_once()

    @patch("app.send_line_message", side_effect=Exception("LINE error"))
    def test_line_error_returns_500(self, _mock_send):
        """異常系: LINE 送信失敗時に 500 を返す"""
        handler = make_handler({"alerts": [SAMPLE_ALERT]})
        handler.do_POST()
        handler.send_response.assert_called_once_with(500)

    @patch("app.send_line_message")
    def test_oversized_body_returns_413(self, mock_send):
        """異常系: MAX_BODY_SIZE を超えるリクエストは 413 を返し LINE 送信は行わない"""
        handler = WebhookHandler.__new__(WebhookHandler)
        handler.headers = {"Content-Length": str(MAX_BODY_SIZE + 1)}
        handler.rfile = io.BytesIO(b"")
        handler.send_response = MagicMock()
        handler.end_headers = MagicMock()
        handler.do_POST()
        mock_send.assert_not_called()
        handler.send_response.assert_called_once_with(413)

    @patch("app.send_line_message")
    def test_empty_body_returns_200(self, mock_send):
        """正常系: Content-Length が 0 のリクエストでも 200 を返し LINE 送信は行わない"""
        handler = make_handler(raw_body=b"")
        handler.do_POST()
        mock_send.assert_not_called()
        handler.send_response.assert_called_once_with(200)


class TestHealthzHandler(unittest.TestCase):

    def test_healthz_returns_200(self):
        """/healthz は 200 を返す"""
        handler = make_get_handler("/healthz")
        handler.do_GET()
        handler.send_response.assert_called_once_with(200)

    def test_unknown_path_returns_404(self):
        """未知のパスは 404 を返す"""
        handler = make_get_handler("/unknown")
        handler.do_GET()
        handler.send_response.assert_called_once_with(404)


if __name__ == "__main__":
    unittest.main()
