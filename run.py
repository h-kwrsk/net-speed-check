#!/usr/bin/env python3
import logging
import os
import sys
import time

import speedtest
from prometheus_client import CollectorRegistry, Gauge, push_to_gateway

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

PUSHGATEWAY_URL = os.environ.get("PUSHGATEWAY_URL", "http://pushgateway:9091")
INSTANCE = os.environ.get("SPEEDTEST_INSTANCE", "raspi-cluster")
MAX_RETRIES = 3
RETRY_DELAY = 30  # seconds


def run_speedtest():
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
            st.upload()

            results = st.results.dict()
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
