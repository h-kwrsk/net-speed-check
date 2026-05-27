"""line-adapter/app.py の単体テスト"""
import io
import json
import os
import unittest
from unittest.mock import MagicMock, patch

# app はモジュールレベルで os.environ を参照するため、インポート前に設定する
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "test-token")
os.environ.setdefault("LINE_USER_ID", "Utest000")

from app import WebhookHandler, format_message, send_line_message  # noqa: E402

SAMPLE_ALERT = {
    "status": "firing",
    "labels": {"alertname": "SpeedtestJobFailed", "job_name": "speedtest-123"},
    "annotations": {"description": "Speedtest CronJob で計測エラーが発生しました。\nJob: speedtest-123"},
}


def make_handler(body_dict: dict) -> WebhookHandler:
    """WebhookHandler をモックリクエストで生成する"""
    body = json.dumps(body_dict).encode()
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

    def test_firing_includes_all_fields(self):
        """正常系: firing アラートはすべてのフィールドを含む"""
        result = format_message(SAMPLE_ALERT)
        self.assertIn("[FIRING]", result)
        self.assertIn("SpeedtestJobFailed", result)
        self.assertIn("speedtest-123", result)
        self.assertIn("Speedtest CronJob で計測エラーが発生しました", result)

    def test_resolved_status(self):
        """正常系: resolved アラートは [RESOLVED] を含む"""
        alert = {**SAMPLE_ALERT, "status": "resolved"}
        result = format_message(alert)
        self.assertIn("[RESOLVED]", result)
        self.assertNotIn("[FIRING]", result)

    def test_missing_job_name(self):
        """正常系: job_name がなくても例外が発生しない"""
        alert = {
            "status": "firing",
            "labels": {"alertname": "SpeedtestJobFailed"},
            "annotations": {"description": "計測エラーが発生しました"},
        }
        result = format_message(alert)
        self.assertIn("SpeedtestJobFailed", result)
        self.assertNotIn("Job:", result)

    def test_missing_description(self):
        """正常系: description がなくても例外が発生しない"""
        alert = {**SAMPLE_ALERT, "annotations": {}}
        result = format_message(alert)
        self.assertIn("[FIRING]", result)

    def test_empty_labels_and_annotations(self):
        """正常系: labels・annotations が空でも例外が発生しない"""
        alert = {"status": "firing", "labels": {}, "annotations": {}}
        result = format_message(alert)
        self.assertIn("[FIRING]", result)


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
    def test_multiple_alerts_sends_each(self, mock_send):
        """正常系: 複数アラートをそれぞれ LINE 送信する"""
        handler = make_handler({"alerts": [SAMPLE_ALERT, SAMPLE_ALERT]})
        handler.do_POST()
        self.assertEqual(mock_send.call_count, 2)

    @patch("app.send_line_message", side_effect=Exception("LINE error"))
    def test_line_error_returns_500(self, _mock_send):
        """異常系: LINE 送信失敗時に 500 を返す"""
        handler = make_handler({"alerts": [SAMPLE_ALERT]})
        handler.do_POST()
        handler.send_response.assert_called_once_with(500)

    @patch("app.send_line_message")
    def test_empty_body_returns_200(self, mock_send):
        """正常系: Content-Length が 0 のリクエストでも 200 を返し LINE 送信は行わない"""
        handler = WebhookHandler.__new__(WebhookHandler)
        handler.headers = {"Content-Length": "0"}
        handler.rfile = io.BytesIO(b"")
        handler.send_response = MagicMock()
        handler.end_headers = MagicMock()
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
