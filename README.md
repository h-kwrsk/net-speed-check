# net-speed-check

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Raspberry Pi の Kubernetes クラスタ上でインターネット速度を定期計測し、Grafana で可視化するシステムです。
計測が失敗した際は LINE に通知します。

## アーキテクチャ

```text
[CronJob (15分毎)]
       |
   run.py
 (Ookla speedtest CLI)
       |
  push metrics
       |
[Prometheus Pushgateway]
       |
  ServiceMonitor
       |
  [Prometheus] ---------> [PrometheusRule]
       |                        |
 [Grafana Dashboard]     [Alertmanager]
                                |
                        [line-adapter]
                                |
                        [LINE Messaging API]
```

## ファイル構成

```text
.
├── speedtest/
│   ├── run.py                    # 速度計測 & Pushgateway 送信スクリプト
│   ├── test_run.py               # 単体テスト
│   ├── Dockerfile                # ARM64 対応コンテナイメージ
│   └── requirements.txt          # Python 依存パッケージ
├── line-adapter/
│   ├── app.py                    # Alertmanager webhook → LINE 転送サービス
│   ├── test_app.py               # 単体テスト
│   ├── Dockerfile                # ARM64 対応コンテナイメージ
│   └── requirements.txt          # Python 依存パッケージ
└── k8s/
    ├── pushgateway.yaml          # Pushgateway Deployment + Service + ServiceMonitor
    ├── cronjob.yaml              # 15分毎に計測を実行する CronJob
    ├── grafana-dashboard.yaml    # Grafana ダッシュボード (ConfigMap)
    ├── line-adapter.yaml         # line-adapter Deployment + Service
    ├── speedtest-alerts.yaml     # PrometheusRule（Job 失敗検知）
    └── alertmanager-config.yaml  # AlertmanagerConfig（LINE へルーティング）
```

## 前提条件

- Kubernetes クラスタ（ARM64）に [`kube-prometheus-stack`](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack) が導入済みであること
- Docker Desktop（`docker buildx` が使えること）
- Docker Hub アカウント

## デプロイ手順

### 1. イメージのビルド & プッシュ

```bash
# buildx ビルダーがなければ作成
docker buildx create --name rpibuilder --use

# 速度計測スクリプト
docker buildx build \
  --platform linux/arm64 \
  -t <DOCKERHUB_USERNAME>/net-speed-check:latest \
  --push speedtest/

# LINE 通知アダプター
docker buildx build \
  --platform linux/arm64 \
  -t <DOCKERHUB_USERNAME>/line-adapter:latest \
  --push line-adapter/
```

### 2. `cronjob.yaml` / `line-adapter.yaml` のイメージ名を更新

各 YAML の `image` フィールドを実際の Docker Hub ユーザー名に変更します。

### 3. LINE 認証情報を Secret として登録

```bash
kubectl create secret generic line-credentials \
  -n monitoring \
  --from-literal=channel-access-token="<Channel Access Token>" \
  --from-literal=user-id="<Your LINE User ID>"
```

- **Channel Access Token**: LINE Developers コンソール → Messaging API → Channel access token
- **User ID**: LINE Developers コンソール → Messaging API → Your user ID（`Uxxxxxxxx` 形式）

### 4. クラスタへデプロイ

```bash
scp -r k8s/ raspi-master.local:~/net-speed-check/
ssh raspi-master.local "kubectl apply -f ~/net-speed-check/k8s/"
```

## 動作確認

### 手動実行

```bash
ssh raspi-master.local \
  "kubectl create job --from=cronjob/speedtest speedtest-manual -n monitoring"
```

### ログ確認

```bash
ssh raspi-master.local "kubectl logs -n monitoring job/speedtest-manual"
```

正常時の出力例:

```text
2026-05-27 16:12:23,940 INFO Running speedtest... (attempt 1/3)
2026-05-27 16:12:54,212 INFO Results: download=419.19 Mbps, upload=408.79 Mbps, ping=12.34 ms, server=Allied Telesis Capital Corporation (Japan)
2026-05-27 16:12:54,213 INFO Pushing metrics to http://pushgateway.monitoring.svc.cluster.local:9091 ...
2026-05-27 16:12:54,306 INFO Metrics pushed successfully.
```

### Grafana ダッシュボード

`kubectl apply` 後、Grafana の sidecar が ConfigMap を検出して自動的にインポートします（約 30 秒）。

**Dashboards → Browse → "Internet Speed Test"** で確認できます。

### LINE 通知の確認

line-adapter の Pod が起動していることを確認します。

```bash
ssh raspi-master.local "kubectl get pods -n monitoring -l app=line-adapter"
```

テスト通知を送信するには、クラスタ内から以下を実行します。

```bash
kubectl run curl-test --image=curlimages/curl:latest --rm --restart=Never -n monitoring -- \
  curl -s -X POST http://line-adapter.monitoring.svc.cluster.local:8080/webhook \
  -H 'Content-Type: application/json' \
  -d '{"alerts":[{"status":"firing","labels":{"alertname":"SpeedtestJobFailed","job_name":"test"},"annotations":{"description":"テスト通知"}}]}'
```

## 開発

### テストの実行

```bash
pip install -r speedtest/requirements.txt
python -m pytest speedtest/test_run.py -v
python -m pytest line-adapter/test_app.py -v
```

### 設定できる環境変数

| 変数名 | デフォルト値 | 説明 |
| --- | --- | --- |
| `PUSHGATEWAY_URL` | `http://pushgateway:9091` | Prometheus Pushgateway のエンドポイント |
| `SPEEDTEST_INSTANCE` | `raspi-cluster` | Prometheus の `instance` ラベルに使う識別子 |
| `SPEEDTEST_MAX_RETRIES` | `3` | 計測失敗時の最大リトライ回数 |
| `SPEEDTEST_RETRY_DELAY` | `30` | リトライ間隔（秒） |

## 収集するメトリクス

| メトリクス名 | 説明 |
| --- | --- |
| `speedtest_download_bits_per_second` | ダウンロード速度（bps） |
| `speedtest_upload_bits_per_second` | アップロード速度（bps） |
| `speedtest_ping_latency_milliseconds` | ping レイテンシ（ms） |
| `speedtest_server_info` | 計測に使用したサーバー情報 |

## ライセンス

[MIT License](LICENSE)
