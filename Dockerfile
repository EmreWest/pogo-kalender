FROM python:3.13-alpine

# Tesseract für die lokale Texterkennung der Infografiken (Deutsch + Englisch)
RUN apk add --no-cache tesseract-ocr tesseract-ocr-data-deu tesseract-ocr-data-eng

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY *.py /app/
COPY landing.html /app/landing.html
COPY status.html /app/status.html
COPY admin.html /app/admin.html
COPY favicon.svg /app/favicon.svg
COPY favicon.ico /app/favicon.ico
COPY favicon-32x32.png /app/favicon-32x32.png
COPY apple-touch-icon.png /app/apple-touch-icon.png
COPY icon-192.png /app/icon-192.png
COPY icon-512.png /app/icon-512.png
COPY site.webmanifest /app/site.webmanifest
COPY pokemon_names-de.json /app/pokemon_names-de.json
COPY pokemon_species-de.json /app/pokemon_species-de.json
COPY plz-de.json /app/plz-de.json

RUN addgroup -S app && adduser -S -G app app \
    && mkdir -p /data \
    && chown -R app:app /app /data

USER app
EXPOSE 8080

# Healthcheck des Webservers. Der Discord-Worker überschreibt ihn in der
# docker-compose.yml mit einer eigenen Prüfung.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/health >/dev/null || exit 1

CMD ["python", "/app/app.py"]
