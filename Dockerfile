FROM python:3.12-slim

WORKDIR /app

# Install openssl for cert generation
RUN apt-get update && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Generate self-signed cert at build time
RUN mkdir -p ./certs && \
    openssl req -x509 -newkey rsa:4096 \
      -keyout ./certs/key.pem -out ./certs/cert.pem \
      -days 3650 -nodes -subj "/CN=soundstitch"

EXPOSE 8443

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8443", \
     "--ssl-keyfile", "./certs/key.pem", "--ssl-certfile", "./certs/cert.pem"]
