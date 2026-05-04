FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    speedtest-cli==2.1.3 \
    prometheus-client==0.25.0

COPY run.py .

USER nobody

CMD ["python", "run.py"]
