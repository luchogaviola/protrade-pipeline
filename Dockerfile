FROM python:3.12-slim

# Dependencias del sistema (cron + ffmpeg para posters de reels si se usa)
RUN apt-get update && apt-get install -y --no-install-recommends \
    cron ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY parse_pdf.py upload_supabase.py build_sheet_data.py run_pipeline.py ./

# Cron: corre el pipeline todos los días a las 8:00 AM (hora del contenedor)
# Cron diario 8 AM. Formato /etc/cron.d/ requiere el usuario (root) en la línea.
RUN echo "0 8 * * * root cd /app && PYTHONIOENCODING=utf-8 python run_pipeline.py >> /data/pipeline.log 2>&1" > /etc/cron.d/protrade \
    && chmod 0644 /etc/cron.d/protrade

VOLUME ["/data"]

# entrypoint: corre una vez al arrancar + deja cron en foreground
CMD ["sh", "-c", "cd /app && PYTHONIOENCODING=utf-8 python run_pipeline.py >> /data/pipeline.log 2>&1; cron -f"]
