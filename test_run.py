"""run.py の単体テスト"""
import unittest
from unittest.mock import MagicMock, patch

import run


# テスト用の計測結果サンプル
SAMPLE_RESULTS = {
    "download": 200_000_000.0,  # 200 Mbps in bps
    "upload": 100_000_000.0,    # 100 Mbps in bps
    "ping": 20.0,
    "server": {
        "name": "Tokyo",
        "host": "speedtest.example.com:8080",
        "country": "Japan",
        "sponsor": "Example ISP",
    },
}


def make_mock_speedtest(results=SAMPLE_RESULTS):
    """speedtest.Speedtest のモックを生成する"""
    st = MagicMock()
    st.results.dict.return_value = results
    return st


class TestRunSpeedtest(unittest.TestCase):

    @patch("run.speedtest.Speedtest")
    def test_success_on_first_attempt(self, mock_cls):
        """正常系: 1 回目で成功した場合に結果が返る"""
        mock_cls.return_value = make_mock_speedtest()

        results = run.run_speedtest()

        self.assertEqual(results["download"], SAMPLE_RESULTS["download"])
        self.assertEqual(results["upload"], SAMPLE_RESULTS["upload"])
        self.assertEqual(results["ping"], SAMPLE_RESULTS["ping"])
        # Speedtest のインスタンスが 1 回だけ生成されていること
        mock_cls.assert_called_once_with(secure=True)

    @patch("run.time.sleep")
    @patch("run.speedtest.Speedtest")
    def test_retry_on_failure_then_success(self, mock_cls, mock_sleep):
        """準正常系: 1 回目が失敗し、2 回目で成功する場合にリトライされる"""
        mock_cls.side_effect = [
            Exception("Unable to connect to servers to test latency."),
            make_mock_speedtest(),
        ]

        results = run.run_speedtest()

        self.assertEqual(results["download"], SAMPLE_RESULTS["download"])
        # 2 回インスタンスが生成されていること
        self.assertEqual(mock_cls.call_count, 2)
        # RETRY_DELAY 秒スリープしていること
        mock_sleep.assert_called_once_with(run.RETRY_DELAY)

    @patch("run.time.sleep")
    @patch("run.speedtest.Speedtest")
    def test_retries_on_zero_download(self, mock_cls, mock_sleep):
        """準正常系: download が 0 の場合にリトライし、次回成功する"""
        zero_result = {**SAMPLE_RESULTS, "download": 0.0}
        mock_cls.side_effect = [
            make_mock_speedtest(zero_result),
            make_mock_speedtest(),
        ]

        results = run.run_speedtest()

        self.assertEqual(results["download"], SAMPLE_RESULTS["download"])
        self.assertEqual(mock_cls.call_count, 2)
        mock_sleep.assert_called_once_with(run.RETRY_DELAY)

    @patch("run.time.sleep")
    @patch("run.speedtest.Speedtest")
    def test_retries_on_zero_upload(self, mock_cls, mock_sleep):
        """準正常系: upload が 0 の場合にリトライし、次回成功する"""
        zero_result = {**SAMPLE_RESULTS, "upload": 0.0}
        mock_cls.side_effect = [
            make_mock_speedtest(zero_result),
            make_mock_speedtest(),
        ]

        results = run.run_speedtest()

        self.assertEqual(results["upload"], SAMPLE_RESULTS["upload"])
        self.assertEqual(mock_cls.call_count, 2)
        mock_sleep.assert_called_once_with(run.RETRY_DELAY)

    @patch("run.time.sleep")
    @patch("run.speedtest.Speedtest")
    def test_raises_after_max_retries(self, mock_cls, mock_sleep):
        """異常系: MAX_RETRIES 回すべて失敗した場合に例外が送出される"""
        error = Exception("Unable to connect to servers to test latency.")
        mock_cls.side_effect = error

        with self.assertRaises(Exception) as ctx:
            run.run_speedtest()

        self.assertEqual(str(ctx.exception), str(error))
        # MAX_RETRIES 回試みていること
        self.assertEqual(mock_cls.call_count, run.MAX_RETRIES)
        # 最後の試行ではスリープしないこと
        self.assertEqual(mock_sleep.call_count, run.MAX_RETRIES - 1)


class TestPushMetrics(unittest.TestCase):

    @patch("run.push_to_gateway")
    def test_pushes_correct_values(self, mock_push):
        """正常系: 計測結果が正しいゲージ値として Pushgateway へ送信される"""
        run.push_metrics(SAMPLE_RESULTS)

        mock_push.assert_called_once()
        _, kwargs = mock_push.call_args
        self.assertEqual(kwargs["job"], "speedtest")
        self.assertEqual(kwargs["grouping_key"], {"instance": run.INSTANCE})

        # registry 内のメトリクス値を検証する
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

    @patch("run.push_to_gateway")
    def test_sponsor_defaults_to_empty_string(self, mock_push):
        """正常系: server に sponsor キーがなくても空文字列で補完される"""
        results = {**SAMPLE_RESULTS, "server": {**SAMPLE_RESULTS["server"]}}
        del results["server"]["sponsor"]

        # 例外が発生しないこと
        run.push_metrics(results)
        mock_push.assert_called_once()

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
