FROM python:3.12-slim

ENV TZ=Asia/Tokyo
# nobody ユーザーのホームディレクトリとして /tmp を使う（Ookla CLI が設定ファイルを書き込むため）
ENV HOME=/tmp

WORKDIR /app

# Ookla 公式 speedtest CLI のインストール
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl gnupg && \
    curl -fsSL https://packagecloud.io/ookla/speedtest-cli/gpgkey | gpg --dearmor -o /usr/share/keyrings/ookla-speedtest.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/ookla-speedtest.gpg] https://packagecloud.io/ookla/speedtest-cli/debian/ bookworm main" > /etc/apt/sources.list.d/ookla-speedtest.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends speedtest && \
    apt-get purge -y curl gnupg && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir prometheus-client==0.25.0

COPY run.py .

USER nobody

CMD ["python", "run.py"]
