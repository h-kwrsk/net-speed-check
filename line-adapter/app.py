#!/usr/bin/env python3
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
USER_ID = os.environ["LINE_USER_ID"]
LINE_API = "https://api.line.me/v2/bot/message/push"


def send_line_message(text: str) -> None:
    resp = requests.post(
        LINE_API,
        headers={"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"},
        json={"to": USER_ID, "messages": [{"type": "text", "text": text}]},
        timeout=10,
    )
    resp.raise_for_status()


def format_message(alert: dict) -> str:
    status = "FIRING" if alert["status"] == "firing" else "RESOLVED"
    alertname = alert["labels"].get("alertname", "unknown")
    job_name = alert["labels"].get("job_name", "")
    description = alert.get("annotations", {}).get("description", "")

    lines = [f"[{status}] {alertname}"]
    if job_name:
        lines.append(f"Job: {job_name}")
    if description:
        lines.append(description)
    return "\n".join(lines)


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            for alert in body.get("alerts", []):
                text = format_message(alert)
                send_line_message(text)
                log.info("Sent LINE message: %s", text[:80])
            self.send_response(200)
        except Exception as e:
            log.error("Failed to process webhook: %s", e)
            self.send_response(500)
        self.end_headers()

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    log.info("Starting LINE webhook adapter on port %d", port)
    HTTPServer(("0.0.0.0", port), WebhookHandler).serve_forever()
