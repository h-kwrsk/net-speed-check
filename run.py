#!/usr/bin/env python3
"""
インターネット速度を計測し、結果を Prometheus Pushgateway へ送信するスクリプト。
Kubernetes CronJob として 30 分ごとに実行されることを想定している。
"""
import logging
import os
import sys
import time

import speedtest
from prometheus_client import CollectorRegistry, Gauge, push_to_gateway

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# 環境変数で上書き可能にすることで、同じイメージを異なる環境でも使い回せる
PUSHGATEWAY_URL = os.environ.get("PUSHGATEWAY_URL", "http://pushgateway:9091")
INSTANCE = os.environ.get("SPEEDTEST_INSTANCE", "raspi-cluster")

# speedtest.net の API は稀に一時的な接続エラーを返すため、リトライで吸収する
MAX_RETRIES = int(os.environ.get("SPEEDTEST_MAX_RETRIES", "3"))
RETRY_DELAY = int(os.environ.get("SPEEDTEST_RETRY_DELAY", "30"))  # seconds


def run_speedtest():
    """速度計測を実行して結果の dict を返す。失敗した場合は MAX_RETRIES 回リトライする。"""
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log.info("Initializing speedtest client... (attempt %d/%d)", attempt, MAX_RETRIES)
            st = speedtest.Speedtest(secure=True)

            log.info("Selecting best server...")
            st.get_best_server()

            log.info("Running download test...")
            st.download()

            log.info("Running upload test...")
            # threads=1 で安定して計測できるケースがある（Raspberry Pi 環境での upload=0 対策）
            st.upload(threads=1)

            results = st.results.dict()

            # 0 Mbps は計測失敗とみなしてリトライする
            if results["download"] == 0 or results["upload"] == 0:
                raise ValueError(
                    f"Invalid result: download={results['download'] / 1e6:.2f} Mbps, "
                    f"upload={results['upload'] / 1e6:.2f} Mbps"
                )

            log.info(
                "Results: download=%.2f Mbps, upload=%.2f Mbps, ping=%.2f ms, server=%s (%s)",
                results["download"] / 1e6,
                results["upload"] / 1e6,
                results["ping"],
                results["server"]["name"],
                results["server"]["country"],
            )
            return results
        except Exception as e:
            last_error = e
            log.warning("Attempt %d/%d failed: %s", attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                log.info("Retrying in %d seconds...", RETRY_DELAY)
                time.sleep(RETRY_DELAY)

    raise last_error


def push_metrics(results):
    """計測結果を Prometheus メトリクスに変換して Pushgateway へ送信する。

    push_to_gateway() を使うことで、同一 job+instance の古いメトリクスを
    上書きし、常に最新の計測値だけが Prometheus に残る。
    """
    registry = CollectorRegistry()
    server = results["server"]

    download_gauge = Gauge(
        "speedtest_download_bits_per_second",
        "Internet download speed in bits per second",
        registry=registry,
    )
    upload_gauge = Gauge(
        "speedtest_upload_bits_per_second",
        "Internet upload speed in bits per second",
        registry=registry,
    )
    ping_gauge = Gauge(
        "speedtest_ping_latency_milliseconds",
        "Internet ping latency in milliseconds",
        registry=registry,
    )
    # サーバー情報はラベルで持ち、値は常に 1 とする Prometheus の info メトリクスパターン
    info_gauge = Gauge(
        "speedtest_server_info",
        "Speedtest server information (value is always 1)",
        ["server_name", "server_host", "server_country", "server_sponsor"],
        registry=registry,
    )

    download_gauge.set(results["download"])
    upload_gauge.set(results["upload"])
    ping_gauge.set(results["ping"])
    info_gauge.labels(
        server_name=server["name"],
        server_host=server["host"],
        server_country=server["country"],
        server_sponsor=server.get("sponsor", ""),
    ).set(1)

    log.info("Pushing metrics to %s ...", PUSHGATEWAY_URL)
    push_to_gateway(
        PUSHGATEWAY_URL,
        job="speedtest",
        registry=registry,
        # grouping_key で job/instance ラベルを明示する。
        # ServiceMonitor の honorLabels: true と組み合わせることで
        # Prometheus がこれらのラベルを上書きしなくなる。
        grouping_key={"instance": INSTANCE},
    )
    log.info("Metrics pushed successfully.")


def main():
    try:
        results = run_speedtest()
    except Exception as e:
        log.error("Speed test failed: %s", e)
        sys.exit(1)

    try:
        push_metrics(results)
    except Exception as e:
        log.error("Failed to push metrics to Pushgateway: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
