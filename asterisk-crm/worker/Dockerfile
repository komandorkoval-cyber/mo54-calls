FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        libsndfile1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# GigaChat uses a certificate chain rooted at the Russian Trusted Root CA.
# Keep TLS verification enabled and install the official root + issuing CA into
# Debian's trust store rather than bypassing certificate verification.
COPY certs/russian_trusted_root_ca_pem.crt /usr/local/share/ca-certificates/russian-trusted-root-ca.crt
COPY certs/russian_trusted_sub_ca_pem.crt /usr/local/share/ca-certificates/russian-trusted-sub-ca.crt
RUN update-ca-certificates

# Скачиваем модель GigaAM при сборке образа (кешируется в volume model-cache)
# Если нет доступа к интернету при сборке — закомментировать; модель скачается при первом запуске
ENV ONNX_ASR_CACHE=/root/.cache/onnx_asr
# Model files are downloaded on first use into the persistent model-cache volume.

COPY *.py .

CMD ["python", "-u", "main.py"]
