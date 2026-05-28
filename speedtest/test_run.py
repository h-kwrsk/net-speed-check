"""run.py の単体テスト"""
import json
import unittest
from unittest.mock import MagicMock, patch

import run

# Ookla CLI の JSON 出力サンプル（bandwidth は bytes/s）
SAMPLE_CLI_OUTPUT = json.dumps({
    "type": "result",
    "ping": {"latency": 20.0, "jitter": 0.5},
    "download": {"bandwidth": 25_000_000},
    "upload": {"bandwidth": 12_500_000},
    "server": {
        "name": "Tokyo",
        "host": "speedtest.example.com",
        "port": 8080,
        "country": "Japan",
    },
})

# bytes/s × 8 → bits/s に変換した期待値
SAMPLE_RESULTS = {
    "download": 200_000_000.0,
    "upload": 100_000_000.0,
    "ping": 20.0,
    "server": {
        "name": "Tokyo",
        "host": "speedtest.example.com:8080",
        "country": "Japan",
        "sponsor": "",
    },
}


def make_proc(stdout=SAMPLE_CLI_OUTPUT, returncode=0, stderr=""):
    """subprocess.CompletedProcess のモックを生成する"""
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


class TestParseResult(unittest.TestCase):

    def test_extracts_result_from_multiline_output(self):
        """正常系: type=result 行を複数行出力から正しく抽出する"""
        ping_line = json.dumps({"type": "ping", "ping": {"latency": 10.0}})
        result_line = SAMPLE_CLI_OUTPUT
        output = f"{ping_line}\n{result_line}\n"

        obj = run._parse_result(output)

        self.assertEqual(obj["type"], "result")
        self.assertEqual(obj["ping"]["latency"], 20.0)

    def test_raises_when_no_result_found(self):
        """異常系: type=result 行がない場合に RuntimeError が送出される"""
        output = json.dumps({"type": "ping", "ping": {"latency": 10.0}})

        with self.assertRaises(RuntimeError):
            run._parse_result(output)


class TestRunSpeedtest(unittest.TestCase):

    @patch("run.subprocess.run")
    def test_success_on_first_attempt(self, mock_run):
        """正常系: 1 回目で成功した場合に結果が返る"""
        mock_run.return_value = make_proc()

        results = run.run_speedtest()

        self.assertEqual(results["download"], SAMPLE_RESULTS["download"])
        self.assertEqual(results["upload"], SAMPLE_RESULTS["upload"])
        self.assertEqual(results["ping"], SAMPLE_RESULTS["ping"])
        self.assertEqual(results["server"]["host"], "speedtest.example.com:8080")
        self.assertEqual(results["server"]["sponsor"], "")
        mock_run.assert_called_once()

    @patch("run.SERVER_ID", "48463")
    @patch("run.subprocess.run")
    def test_server_id_passed_when_set(self, mock_run):
        """正常系: SERVER_ID が設定されている場合に --server-id が渡される"""
        mock_run.return_value = make_proc()

        run.run_speedtest()

        args = mock_run.call_args[0][0]
        self.assertIn("--server-id", args)
        self.assertIn("48463", args)

    @patch("run.SERVER_ID", "")
    @patch("run.subprocess.run")
    def test_server_id_not_passed_when_empty(self, mock_run):
        """正常系: SERVER_ID が未設定の場合に --server-id が渡されない"""
        mock_run.return_value = make_proc()

        run.run_speedtest()

        args = mock_run.call_args[0][0]
        self.assertNotIn("--server-id", args)

    @patch("run.time.sleep")
    @patch("run.subprocess.run")
    def test_retry_on_failure_then_success(self, mock_run, mock_sleep):
        """準正常系: 1 回目が失敗し、2 回目で成功する場合にリトライされる"""
        mock_run.side_effect = [
            make_proc(returncode=1, stderr="connection error"),
            make_proc(),
        ]

        results = run.run_speedtest()

        self.assertEqual(results["download"], SAMPLE_RESULTS["download"])
        self.assertEqual(mock_run.call_count, 2)
        mock_sleep.assert_called_once_with(run.RETRY_DELAY)

    @patch("run.time.sleep")
    @patch("run.subprocess.run")
    def test_retries_on_zero_download(self, mock_run, mock_sleep):
        """準正常系: download が 0 の場合にリトライし、次回成功する"""
        zero_output = json.dumps({
            "type": "result",
            "ping": {"latency": 20.0},
            "download": {"bandwidth": 0},
            "upload": {"bandwidth": 12_500_000},
            "server": {"name": "Tokyo", "host": "speedtest.example.com", "country": "Japan"},
        })
        mock_run.side_effect = [make_proc(stdout=zero_output), make_proc()]

        results = run.run_speedtest()

        self.assertEqual(results["download"], SAMPLE_RESULTS["download"])
        self.assertEqual(mock_run.call_count, 2)
        mock_sleep.assert_called_once_with(run.RETRY_DELAY)

    @patch("run.time.sleep")
    @patch("run.subprocess.run")
    def test_retries_on_zero_upload(self, mock_run, mock_sleep):
        """準正常系: upload が 0 の場合にリトライし、次回成功する"""
        zero_output = json.dumps({
            "type": "result",
            "ping": {"latency": 20.0},
            "download": {"bandwidth": 25_000_000},
            "upload": {"bandwidth": 0},
            "server": {"name": "Tokyo", "host": "speedtest.example.com", "country": "Japan"},
        })
        mock_run.side_effect = [make_proc(stdout=zero_output), make_proc()]

        results = run.run_speedtest()

        self.assertEqual(results["upload"], SAMPLE_RESULTS["upload"])
        self.assertEqual(mock_run.call_count, 2)
        mock_sleep.assert_called_once_with(run.RETRY_DELAY)

    @patch("run.time.sleep")
    @patch("run.subprocess.run")
    def test_raises_after_max_retries(self, mock_run, mock_sleep):
        """異常系: MAX_RETRIES 回すべて失敗した場合に例外が送出される"""
        mock_run.return_value = make_proc(returncode=1, stderr="connection refused")

        with self.assertRaises(RuntimeError) as ctx:
            run.run_speedtest()

        self.assertIn("connection refused", str(ctx.exception))
        self.assertEqual(mock_run.call_count, run.MAX_RETRIES)
        self.assertEqual(mock_sleep.call_count, run.MAX_RETRIES - 1)

    @patch("run.time.sleep")
    @patch("run.subprocess.run")
    def test_retries_on_low_download(self, mock_run, mock_sleep):
        """準正常系: download が MIN_SPEED_MBPS 未満の場合にリトライし、次回成功する"""
        low_output = json.dumps({
            "type": "result",
            "ping": {"latency": 20.0},
            "download": {"bandwidth": int(run.MIN_SPEED_MBPS * 1e6 / 8) - 1},  # bytes/s
            "upload": {"bandwidth": 12_500_000},
            "server": {"name": "Tokyo", "host": "speedtest.example.com", "country": "Japan"},
        })
        mock_run.side_effect = [make_proc(stdout=low_output), make_proc()]

        results = run.run_speedtest()

        self.assertEqual(results["download"], SAMPLE_RESULTS["download"])
        self.assertEqual(mock_run.call_count, 2)
        mock_sleep.assert_called_once_with(run.RETRY_DELAY)

    @patch("run.time.sleep")
    @patch("run.subprocess.run")
    def test_retries_on_low_upload(self, mock_run, mock_sleep):
        """準正常系: upload が MIN_SPEED_MBPS 未満の場合にリトライし、次回成功する"""
        low_output = json.dumps({
            "type": "result",
            "ping": {"latency": 20.0},
            "download": {"bandwidth": 25_000_000},
            "upload": {"bandwidth": int(run.MIN_SPEED_MBPS * 1e6 / 8) - 1},  # bytes/s
            "server": {"name": "Tokyo", "host": "speedtest.example.com", "country": "Japan"},
        })
        mock_run.side_effect = [make_proc(stdout=low_output), make_proc()]

        results = run.run_speedtest()

        self.assertEqual(results["upload"], SAMPLE_RESULTS["upload"])
        self.assertEqual(mock_run.call_count, 2)
        mock_sleep.assert_called_once_with(run.RETRY_DELAY)

    @patch("run.subprocess.run")
    def test_host_without_port(self, mock_run):
        """正常系: server に port がない場合は host のみを使う"""
        output = json.dumps({
            "type": "result",
            "ping": {"latency": 10.0},
            "download": {"bandwidth": 25_000_000},
            "upload": {"bandwidth": 12_500_000},
            "server": {"name": "Osaka", "host": "osaka.example.com", "country": "Japan"},
        })
        mock_run.return_value = make_proc(stdout=output)

        results = run.run_speedtest()

        self.assertEqual(results["server"]["host"], "osaka.example.com")


class TestPushMetrics(unittest.TestCase):

    @patch("run.push_to_gateway")
    def test_pushes_correct_values(self, mock_push):
        """正常系: 計測結果が正しいゲージ値として Pushgateway へ送信される"""
        run.push_metrics(SAMPLE_RESULTS)

        mock_push.assert_called_once()
        _, kwargs = mock_push.call_args
        self.assertEqual(kwargs["job"], "speedtest")
        self.assertEqual(kwargs["grouping_key"], {"instance": run.INSTANCE})

        registry = kwargs["registry"]
        metrics = {m.name: m for m in registry.collect()}
        self.assertAlmostEqual(
            list(metrics["speedtest_download_bits_per_second"].samples)[0].value,
            SAMPLE_RESULTS["download"],
        )
        self.assertAlmostEqual(
            list(metrics["speedtest_upload_bits_per_second"].samples)[0].value,
            SAMPLE_RESULTS["upload"],
        )
        self.assertAlmostEqual(
            list(metrics["speedtest_ping_latency_milliseconds"].samples)[0].value,
            SAMPLE_RESULTS["ping"],
        )

    @patch("run.push_to_gateway", side_effect=Exception("connection refused"))
    def test_raises_on_pushgateway_error(self, _mock_push):
        """異常系: Pushgateway への送信が失敗した場合に例外が送出される"""
        with self.assertRaises(Exception):
            run.push_metrics(SAMPLE_RESULTS)


class TestMain(unittest.TestCase):

    @patch("run.push_metrics")
    @patch("run.run_speedtest", return_value=SAMPLE_RESULTS)
    def test_main_success(self, mock_run, mock_push):
        """正常系: main() が正常終了する"""
        run.main()
        mock_run.assert_called_once()
        mock_push.assert_called_once_with(SAMPLE_RESULTS)

    @patch("run.run_speedtest", side_effect=Exception("speedtest error"))
    def test_main_exits_on_speedtest_failure(self, _mock_run):
        """異常系: 計測失敗時に sys.exit(1) が呼ばれる"""
        with self.assertRaises(SystemExit) as ctx:
            run.main()
        self.assertEqual(ctx.exception.code, 1)

    @patch("run.push_metrics", side_effect=Exception("push error"))
    @patch("run.run_speedtest", return_value=SAMPLE_RESULTS)
    def test_main_exits_on_push_failure(self, _mock_run, _mock_push):
        """異常系: 送信失敗時に sys.exit(1) が呼ばれる"""
        with self.assertRaises(SystemExit) as ctx:
            run.main()
        self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
