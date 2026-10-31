#!/usr/bin/env python3
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
USER_ID = os.environ["LINE_USER_ID"]
LINE_API = "https://api.line.me/v2/bot/message/push"
MAX_BODY_SIZE = 1024 * 1024  # 1MB: Alertmanager のペイロードは通常数 KB 以内
MAX_MESSAGE_LENGTH = 5000  # LINE テキストメッセージの最大文字数
JST = timezone(timedelta(hours=9))


def send_line_message(text: str) -> None:
    resp = requests.post(
        LINE_API,
        headers={"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"},
        json={"to": USER_ID, "messages": [{"type": "text", "text": text}]},
        timeout=10,
    )
    resp.raise_for_status()


def _parse_time(value: str) -> datetime | None:
    """Alertmanager の RFC3339 時刻を JST の datetime に変換する。
    未設定を表す 0001-01-01 や不正な値は None を返す。
    """
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.year <= 1:
        return None
    # 表示は分単位のため、秒以下を切り捨てて表示時刻と期間の計算を一致させる
    return dt.astimezone(JST).replace(second=0, microsecond=0)


def _format_duration(start: datetime, end: datetime) -> str:
    minutes = max(int((end - start).total_seconds() // 60), 0)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}時間{minutes}分" if minutes else f"{hours}時間"
    return f"{minutes}分"


def format_message(alert: dict[str, object]) -> str:
    """1 件のアラートを日本語の通知文に整形する。
    本文は PrometheusRule の annotations（summary / description / action）を使う。
    """
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    title = annotations.get("summary") or labels.get("alertname", "不明なアラート")
    starts_at = _parse_time(alert.get("startsAt", ""))

    if alert.get("status") == "resolved":
        lines = [f"✅【解消】{title}"]
        ends_at = _parse_time(alert.get("endsAt", ""))
        # 解消時の description は発生時点の値のため表示せず、期間だけを示す
        if starts_at and ends_at:
            # 日付をまたぐ場合は終了側にも日付を付け、終了が開始より前に見えないようにする
            end_format = "%H:%M" if ends_at.date() == starts_at.date() else "%m/%d %H:%M"
            lines.append(
                f"期間: {starts_at:%m/%d %H:%M} 〜 {ends_at.strftime(end_format)}"
                f"（{_format_duration(starts_at, ends_at)}）"
            )
        return "\n".join(lines)

    # Alertmanager はグループ内で通知済みの発生中アラートも毎回含めて送るため、
    # 新規発生と誤解されないよう「発生中」と表記する
    lines = [f"🔴【発生中】{title}"]
    if annotations.get("description"):
        lines.append(annotations["description"])
    if starts_at:
        lines.append(f"発生時刻: {starts_at:%m/%d %H:%M}")
    if annotations.get("action"):
        lines.append(f"対処: {annotations['action']}")
    return "\n".join(lines)


def format_notification(alerts: list[dict[str, object]]) -> str:
    """webhook 1 回分のアラートを 1 通のメッセージにまとめる。
    LINE の無料枠（月 200 通）を節約するため、アラートごとには送信しない。
    """
    # 解消を先に表示する。発生中のアラートは通知済みのものが再送されている場合があり、
    # 今回の変化である解消を先頭に置く（sorted は安定ソートのため同じ状態内の順序は保たれる）
    ordered = sorted(alerts, key=lambda a: a.get("status") != "resolved")
    return _truncate("\n\n".join(format_message(a) for a in ordered))


def _truncate(text: str) -> str:
    """LINE の上限文字数に収める。
    絵文字（🔴 など）は UTF-16 で 2 単位になるため、安全側に UTF-16 単位で数える。
    """
    if len(text.encode("utf-16-le")) // 2 <= MAX_MESSAGE_LENGTH:
        return text
    suffix = "\n…（以下省略）"
    limit_bytes = (MAX_MESSAGE_LENGTH - len(suffix)) * 2
    # サロゲートペアの途中で切れた場合は errors="ignore" で不完全な文字を捨てる
    head = text.encode("utf-16-le")[:limit_bytes].decode("utf-16-le", errors="ignore")
    return head + suffix


class WebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
        else:
            self.send_response(404)
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_BODY_SIZE:
                self.send_response(413)
                self.end_headers()
                return
            body = json.loads(self.rfile.read(length)) if length > 0 else {}
            alerts = body.get("alerts", [])
            if alerts:
                text = format_notification(alerts)
                send_line_message(text)
                log.info("Sent LINE message (%d alerts): %s", len(alerts), text[:80])
            self.send_response(200)
        except Exception as e:
            log.error("Failed to process webhook: %s", e)
            self.send_response(500)
        self.end_headers()

    def log_message(self, format, *args):
        # BaseHTTPRequestHandler のデフォルト実装は HTTP アクセスログを stderr に出力するため抑制する
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    log.info("Starting LINE webhook adapter on port %d", port)
    HTTPServer(("0.0.0.0", port), WebhookHandler).serve_forever()
