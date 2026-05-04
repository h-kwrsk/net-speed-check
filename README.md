# net-speed-check

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Raspberry Pi の Kubernetes クラスタ上でインターネット速度を定期計測し、Grafana で可視化するシステムです。

## アーキテクチャ

```text
[CronJob (30分毎)]
       |
   run.py
 (speedtest-cli)
       |
  push metrics
       |
[Prometheus Pushgateway]
       |
  ServiceMonitor
       |
  [Prometheus]
       |
 [Grafana Dashboard]
```

## ファイル構成

```text
.
├── run.py                        # 速度計測 & Pushgateway 送信スクリプト
├── test_run.py                   # 単体テスト
├── Dockerfile                    # ARM64 対応コンテナイメージ
└── k8s/
    ├── pushgateway.yaml          # Pushgateway Deployment + Service + ServiceMonitor
    ├── cronjob.yaml              # 30分毎に計測を実行する CronJob
    └── grafana-dashboard.yaml    # Grafana ダッシュボード (ConfigMap)
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

docker buildx build \
  --platform linux/arm64 \
  -t <DOCKERHUB_USERNAME>/net-speed-check:latest \
  --push .
```

### 2. `cronjob.yaml` のイメージ名を更新

[k8s/cronjob.yaml](k8s/cronjob.yaml) の `image` フィールドを実際の Docker Hub ユーザー名に変更します。

### 3. クラスタへデプロイ

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
2026-05-04 17:18:58,330 INFO Initializing speedtest client... (attempt 1/3)
2026-05-04 17:18:59,340 INFO Selecting best server...
2026-05-04 17:19:00,232 INFO Running download test...
2026-05-04 17:19:10,074 INFO Running upload test...
2026-05-04 17:19:14,032 INFO Results: download=332.79 Mbps, upload=349.51 Mbps, ping=18.66 ms, server=Tokyo (Japan)
2026-05-04 17:19:14,033 INFO Pushing metrics to http://pushgateway.monitoring.svc.cluster.local:9091 ...
2026-05-04 17:19:14,061 INFO Metrics pushed successfully.
```

### Grafana ダッシュボード

`kubectl apply` 後、Grafana の sidecar が ConfigMap を検出して自動的にインポートします（約 30 秒）。

**Dashboards → Browse → "Internet Speed Test"** で確認できます。

## 開発

### テストの実行

```bash
pip install speedtest-cli prometheus-client pytest
python -m pytest test_run.py -v
```

### 設定できる環境変数

| 変数名 | デフォルト値 | 説明 |
| --- | --- | --- |
| `PUSHGATEWAY_URL` | `http://pushgateway:9091` | Prometheus Pushgateway のエンドポイント |
| `SPEEDTEST_INSTANCE` | `raspi-cluster` | Prometheus の `instance` ラベルに使う識別子 |

## 収集するメトリクス

| メトリクス名 | 説明 |
| --- | --- |
| `speedtest_download_bits_per_second` | ダウンロード速度（bps） |
| `speedtest_upload_bits_per_second` | アップロード速度（bps） |
| `speedtest_ping_latency_milliseconds` | ping レイテンシ（ms） |
| `speedtest_server_info` | 計測に使用したサーバー情報 |

## ライセンス

[MIT License](LICENSE)
