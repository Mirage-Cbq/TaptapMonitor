FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY requirements.lock ./
COPY taptap_monitor ./taptap_monitor
RUN pip install --no-cache-dir --require-hashes -r requirements.lock && useradd --uid 10001 --create-home monitor \
    && mkdir /app/data && chown monitor:monitor /app/data
COPY config.example.toml ./config.toml
COPY website/dist ./website/dist
USER monitor
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
CMD ["python", "-m", "taptap_monitor", "serve"]
