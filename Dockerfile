FROM python:3.12-slim

WORKDIR /app

ARG APP_VERSION=dev
ARG BUILD_DATE=
ENV APP_VERSION=$APP_VERSION
ENV BUILD_DATE=$BUILD_DATE

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates/ templates/

# subscriptions.local.json es opcional y gitignored
# se monta como volumen en local si existe

RUN adduser --disabled-password --gecos "" appuser && \
    chown -R appuser /app
USER appuser

EXPOSE 8000

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--timeout", "120", "--workers", "2", "--reload", "app:app"]