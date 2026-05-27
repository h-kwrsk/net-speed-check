FROM python:3.12-slim
ENV TZ=Asia/Tokyo
WORKDIR /app
RUN pip install --no-cache-dir requests==2.32.3
COPY app.py .
USER nobody
CMD ["python", "app.py"]
