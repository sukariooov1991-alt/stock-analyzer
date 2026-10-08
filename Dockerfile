FROM python:3.13-trixie-slim

WORKDIR /app

# متطلبات النظام لـ psycopg2-binary
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render يحقن PORT — نقرأه ديناميكياً
ENV PORT=10000
EXPOSE ${PORT}

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
