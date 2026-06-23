# ============================================================
# ISEO Chat Web — Dockerfile (Incremento 1)
# Immagine leggera: solo dipendenze core (niente torch/chromadb).
# Per i connettori (Incremento 2) aggiungere requirements-connectors.txt.
# ============================================================
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_DATA_DIR=/data

WORKDIR /app

# Dipendenze prima del codice: sfrutta la cache dei layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Codice applicativo
COPY app/ ./app/
COPY templates/ ./templates/
COPY static/ ./static/

# Utente non-root (principio di minimo privilegio)
RUN useradd -m -u 10001 appuser && mkdir -p /data && chown -R appuser:appuser /data /app
USER appuser

EXPOSE 8000

# Health check allineato a /healthz
HEALTHCHECK --interval=30s --timeout=4s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
