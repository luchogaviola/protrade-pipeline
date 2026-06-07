FROM python:3.12-slim

# Solo cron (la sync es liviana: HTTP a la API del proveedor, sin PDFs ni imágenes).
RUN apt-get update && apt-get install -y --no-install-recommends \
    cron \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Único script: trae el catálogo de la API del proveedor (Lambo Tech / HodERP) -> Supabase.
COPY sync_hodmall.py ./

# Cron: sincroniza el catálogo CADA HORA (stock + dólar del día).
# Formato /etc/cron.d/ requiere el usuario (root) en la línea.
RUN echo "0 * * * * root cd /app && PYTHONIOENCODING=utf-8 python sync_hodmall.py >> /data/pipeline.log 2>&1" > /etc/cron.d/protrade \
    && chmod 0644 /etc/cron.d/protrade

VOLUME ["/data"]

# entrypoint: corre una vez al arrancar + deja cron en foreground
CMD ["sh", "-c", "cd /app && PYTHONIOENCODING=utf-8 python sync_hodmall.py >> /data/pipeline.log 2>&1; cron -f"]
