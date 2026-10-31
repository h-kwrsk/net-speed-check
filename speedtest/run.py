#!/usr/bin/env python3
"""
インターネット速度を計測し、結果を Prometheus Pushgateway へ送信するスクリプト。
Kubernetes CronJob として 30 分ごとに実行されることを想定している。
"""
import json
import logging
import os
import subprocess
import sys
import time

from prometheus_client import CollectorRegistry, Gauge, push_to_gateway

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# 環境変数で上書き可能にすることで、同じイメージを異なる環境でも使い回せる
PUSHGATEWAY_URL = os.environ.get("PUSHGATEWAY_URL", "http://pushgateway:9091")
INSTANCE = os.environ.get("SPEEDTEST_INSTANCE", "raspi-cluster")

# speedtest.net の API は稀に一時的な接続エラーを返すため、リトライで吸収する
MAX_RETRIES = int(os.environ.get("SPEEDTEST_MAX_RETRIES", "3"))
RETRY_DELAY = int(os.environ.get("SPEEDTEST_RETRY_DELAY", "30"))  # seconds

# 計測サーバーを固定することで、サーバー起因のブレを排除して一貫した計測を行う
# 未設定時は Ookla CLI がレイテンシ最小のサーバーを自動選択する
SERVER_ID = os.environ.get("SPEEDTEST_SERVER_ID", "")

# Ookla CLI はリクエスト過多で "Limit reached" になると exit code 173 を返す。
# 終了コードは非公開仕様のため、出力中のメッセージでも判定する
RATE_LIMIT_EXIT_CODE = 173
RATE_LIMIT_MESSAGE = "Limit reached"


class RateLimitError(RuntimeError):
    """Ookla のレート制限に達したことを表す。
    制限中にリトライすると制限期間を長引かせるため、即座に失敗させる。
    """


def _parse_result(stdout: str) -> dict[str, object]:
    """speedtest CLI の JSON 出力から type=result のオブジェクトを抽出する。
    CLI は計測段階ごとに複数行の JSON を出力するため、最終結果行を探す。
    """
    for line in stdout.strip().split("\n"):
        try:
            obj = json.loads(line)
            if obj.get("type") == "result":
                return obj
        except json.JSONDecodeError:
            continue
    raise RuntimeError(f"No result found in speedtest output: {stdout[:200]}")


def run_speedtest() -> dict[str, object]:
    """速度計測を実行して結果の dict を返す。失敗した場合は MAX_RETRIES 回リトライする。

    SERVER_ID が設定されている場合、固定サーバーで MAX_RETRIES 回試みた後、
    すべて失敗したら自動選択で 1 回フォールバックする。
    レート制限（RateLimitError）の場合はリトライ・フォールバックせず即座に送出する。
    リトライのたびに Ookla へのリクエストが増えるため、低速な計測結果はリトライせずそのまま返す。
    """
    last_error = None
    # SERVER_ID が設定されている場合: [固定 × MAX_RETRIES, 自動選択 × 1]
    # 未設定の場合:                  [自動選択 × MAX_RETRIES]
    server_ids: list[str] = [SERVER_ID] * MAX_RETRIES + ([""] if SERVER_ID else [])
    total = len(server_ids)

    for attempt, server_id in enumerate(server_ids, 1):
        is_fallback = SERVER_ID and not server_id
        try:
            if is_fallback:
                log.info("Running speedtest... (attempt %d/%d, auto-select fallback)", attempt, total)
            else:
                log.info("Running speedtest... (attempt %d/%d)", attempt, total)
            cmd = ["speedtest", "--format=json", "--accept-license", "--accept-gdpr"]
            if server_id:
                cmd += ["--server-id", server_id]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
            # --format=json 指定時はエラーが stdout に JSON で出る場合もあるため両方を確認する
            output = f"{proc.stderr.strip()} {proc.stdout.strip()}".strip()
            if proc.returncode == RATE_LIMIT_EXIT_CODE or (
                proc.returncode != 0 and RATE_LIMIT_MESSAGE in output
            ):
                raise RateLimitError(
                    f"speedtest rate limited (exit code {proc.returncode}): {output[:300]}"
                )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"speedtest exited with code {proc.returncode}: {proc.stderr.strip()}"
                )

            data = _parse_result(proc.stdout)

            # Ookla CLI の bandwidth は bytes/s のため bits/s へ変換する
            download = float(data["download"]["bandwidth"]) * 8
            upload = float(data["upload"]["bandwidth"]) * 8
            ping = float(data["ping"]["latency"])
            server = data["server"]
            port = server.get("port", "")
            host = f"{server['host']}:{port}" if port else server["host"]

            results = {
                "download": download,
                "upload": upload,
                "ping": ping,
                "server": {
                    "name": server["name"],
                    "host": host,
                    "country": server["country"],
                    "sponsor": "",
                },
            }

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
        except RateLimitError:
            # 下の except Exception でリトライされないよう、そのまま送出する
            raise
        except Exception as e:
            last_error = e
            log.warning("Attempt %d/%d failed: %s", attempt, total, e)
            if attempt < total:
                log.info("Retrying in %d seconds...", RETRY_DELAY)
                time.sleep(RETRY_DELAY)

    raise last_error


def push_metrics(results: dict[str, object]) -> None:
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
        server_sponsor=server["sponsor"],
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


def main() -> None:
    try:
        results = run_speedtest()
    except RateLimitError as e:
        # レート制限は想定内の状態のため Job を成功扱いにし、SpeedtestJobFailed を発火させない。
        # 制限が長引いた場合は SpeedtestDataStale で検知する
        log.warning("Speed test skipped due to rate limit: %s", e)
        sys.exit(0)
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
