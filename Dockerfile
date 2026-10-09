FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libopus0 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 sentra

WORKDIR /app

COPY requirements.txt /app/
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

COPY . /app
RUN mkdir -p /data && chown -R sentra:sentra /data

USER sentra
ENV DATABASE_URL=sqlite+aiosqlite:////data/sentra.db

CMD ["python", "main.py"]
