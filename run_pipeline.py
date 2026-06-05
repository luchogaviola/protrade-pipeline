"""
Orquestador del pipeline diario ProTrade.

Flujo:
1. Descarga PDFs del Drive del proveedor (gdown)
2. Compara hash/fecha con el estado anterior → detecta qué cambió
3. Parsea los PDFs nuevos/modificados (pdfplumber + PyMuPDF)
4. Sube imágenes del proveedor a Supabase Storage (solo SKUs sin imagen IA)
5. Genera catalogo.json (con override de imágenes IA) y lo sube a Supabase
6. Dispara webhook a n8n para que escriba al Google Sheet + mande Telegram

Pensado para correr en Docker (easypanel) con cron diario.
Config por variables de entorno (ver .env.example).
"""
from __future__ import annotations

import gc
import hashlib
import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import requests

# ---------- Config ----------
DRIVE_FOLDER = os.environ.get(
    "PROVEEDOR_DRIVE_FOLDER",
    "1vRh7MP5ng5YymTJF-XTD6nva1mP5Uuqs",
)
SUPA_URL = os.environ.get("SUPABASE_URL", "https://arjvysbtvznibqjexcek.supabase.co")
SUPA_KEY = os.environ["SUPABASE_SERVICE_KEY"]  # required
BUCKET = os.environ.get("SUPABASE_BUCKET", "protrade-productos")
N8N_WEBHOOK = os.environ.get("N8N_SHEET_WEBHOOK", "")  # opcional
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

WORK = Path(os.environ.get("PIPELINE_WORK_DIR", "/data"))
PDF_DIR = WORK / "pdfs"
STATE_FILE = WORK / "state.json"
OUT_DIR = WORK / "output"

# ---------- Robustez ----------
# Descarga: los PDFs del proveedor son grandes (27-55MB c/u, ~200MB total).
# En easypanel (server 86% RAM, red lenta) 600s no alcanza -> 1800s + reintentos.
GDOWN_TIMEOUT = int(os.environ.get("GDOWN_TIMEOUT", "1800"))  # 30 min
GDOWN_RETRIES = int(os.environ.get("GDOWN_RETRIES", "3"))
# Gate de sanidad: NUNCA pisar el catálogo bueno con uno roto.
# El catálogo histórico ronda 1900 productos; sin categorias.json todo cae a "Otros".
MIN_PRODUCTOS = int(os.environ.get("MIN_PRODUCTOS", "1000"))
MAX_OTROS = int(os.environ.get("MAX_OTROS", "100"))


def log(msg: str):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def file_hash(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"pdfs": {}}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def download_pdfs() -> list[Path]:
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    # Resiliente: reintenta, tolera timeouts/parciales y NUNCA tira excepción.
    # Devuelve lo que haya en disco, incluido el cache del run anterior (volumen /data).
    for intento in range(1, GDOWN_RETRIES + 1):
        log(f"Descargando PDFs del Drive {DRIVE_FOLDER} (intento {intento}/{GDOWN_RETRIES})...")
        try:
            subprocess.run(
                ["python", "-m", "gdown", "--folder",
                 f"https://drive.google.com/drive/folders/{DRIVE_FOLDER}",
                 "-O", str(PDF_DIR)],
                check=False, timeout=GDOWN_TIMEOUT,
            )
            break  # gdown terminó (aunque sea parcial); salimos del loop de reintentos
        except subprocess.TimeoutExpired:
            log(f"  gdown TIMEOUT a los {GDOWN_TIMEOUT}s (intento {intento})")
        except Exception as e:  # noqa: BLE001 - no queremos que el cron muera nunca acá
            log(f"  gdown error (intento {intento}): {e}")
    # Limpiar descargas a medio bajar (.part) para no parsear basura.
    for part in PDF_DIR.rglob("*.part"):
        try:
            part.unlink()
        except OSError:
            pass
    pdfs = list(PDF_DIR.rglob("*.pdf"))
    log(f"  {len(pdfs)} PDFs disponibles en disco")
    return pdfs


def detect_changes(pdfs: list[Path], state: dict) -> list[Path]:
    """Devuelve los PDFs nuevos o modificados (hash distinto)."""
    changed = []
    new_state = {}
    for pdf in pdfs:
        h = file_hash(pdf)
        new_state[pdf.name] = h
        if state["pdfs"].get(pdf.name) != h:
            changed.append(pdf)
    state["pdfs"] = new_state
    log(f"  {len(changed)} PDFs nuevos/modificados de {len(pdfs)}")
    return changed


def notify_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
    except Exception as e:
        log(f"  telegram error: {e}")


def trigger_n8n(payload: dict):
    if not N8N_WEBHOOK:
        return
    try:
        requests.post(N8N_WEBHOOK, json=payload, timeout=30)
        log("  webhook n8n disparado")
    except Exception as e:
        log(f"  n8n webhook error: {e}")


def main():
    log("=== PIPELINE PROTRADE — inicio ===")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()

    # 1-2. Descargar + detectar cambios
    pdfs = download_pdfs()
    if not pdfs:
        msg = "ABORTADO: no se bajó ningún PDF ni hay cache en disco. Catálogo en prod INTACTO."
        log(msg)
        notify_telegram(f"⚠️ *Pipeline ProTrade* — {msg}")
        return
    changed = detect_changes(pdfs, state)

    # 3-5. Procesar TODO (el parser es rápido; reprocesa full para mantener consistencia)
    # Importa los módulos del pipeline
    sys.path.insert(0, str(Path(__file__).parent))
    from parse_pdf import parse_pdf, fetch_dolar_blue_venta, slugify
    from upload_supabase import upload_proveedor_images
    from build_sheet_data import scan_user_skus  # reusa scan IA

    blue = fetch_dolar_blue_venta()
    log(f"Dólar blue venta: {blue}")

    all_products = []
    for pdf in pdfs:
        try:
            prods = parse_pdf(pdf, OUT_DIR)
            for p in prods:
                from dataclasses import asdict
                d = asdict(p)
                d["costo_ars"] = round(p.costo_usd * blue, 0)
                d["precio_30"] = round(p.costo_usd * blue * 1.1, 0)  # margen 10% (legacy name kept)
                d["pdf_source"] = pdf.name
                d["imagen_local"] = Path(p.imagen_local).name if p.imagen_local else None
                all_products.append(d)
        except Exception as e:
            log(f"  ERROR parse {pdf.name}: {e}")
        gc.collect()  # liberar RAM entre PDFs (server al límite de memoria -> evita OOM/Killed)

    log(f"Total productos parseados: {len(all_products)}")

    # Guardar all-products para los otros scripts
    (OUT_DIR / "all-products.json").write_text(
        json.dumps({"dolar_blue_venta": blue, "total_productos": len(all_products),
                    "products": all_products}, ensure_ascii=False), encoding="utf-8")

    # 4. Subir imágenes proveedor (idempotente)
    upload_proveedor_images(all_products, OUT_DIR / "images")

    # 5. Generar catalogo.json + subir a Supabase (con GATE de sanidad)
    subprocess.run(["python", "build_sheet_data.py"], cwd=str(Path(__file__).parent),
                   check=False, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    catalog = OUT_DIR / "sheet-data.json"
    if not catalog.exists():
        msg = "ABORTADO: build_sheet_data.py no generó sheet-data.json. Catálogo en prod INTACTO."
        log(f"  {msg}")
        notify_telegram(f"⚠️ *Pipeline ProTrade* — {msg}")
        return

    nuevo = json.loads(catalog.read_text(encoding="utf-8"))
    total_nuevo = nuevo.get("total", 0)
    otros_nuevo = (nuevo.get("categorias_count") or {}).get("Otros", 0)

    # No pisar el catálogo bueno con uno roto: pocos productos (descarga parcial)
    # o "Otros" desbordado (faltan categorias.json/clasificacion_ia.json).
    if total_nuevo < MIN_PRODUCTOS or otros_nuevo > MAX_OTROS:
        msg = (f"RECHAZADO (no subido): total={total_nuevo} (mín {MIN_PRODUCTOS}), "
               f"Otros={otros_nuevo} (máx {MAX_OTROS}). Catálogo en prod QUEDA INTACTO.")
        log(f"  {msg}")
        notify_telegram(f"🔴 *Pipeline ProTrade* — {msg}")
        return

    url = f"{SUPA_URL}/storage/v1/object/{BUCKET}/data/catalogo.json"
    resp = requests.post(url, headers={"Authorization": f"Bearer {SUPA_KEY}",
                                "Content-Type": "application/json", "x-upsert": "true"},
                  data=catalog.read_bytes(), timeout=60)
    if resp.status_code >= 300:
        msg = f"Upload a Supabase FALLÓ ({resp.status_code}). Catálogo previo intacto."
        log(f"  {msg}: {resp.text[:200]}")
        notify_telegram(f"🔴 *Pipeline ProTrade* — {msg}")
        return
    log(f"  catalogo.json subido a Supabase (total={total_nuevo}, Otros={otros_nuevo})")

    save_state(state)

    # 6. Notificar n8n + Telegram
    data = json.loads(catalog.read_text(encoding="utf-8")) if catalog.exists() else {}
    resumen = (f"*Pipeline ProTrade {date.today().isoformat()}*\n"
               f"PDFs cambiados: {len(changed)}\n"
               f"Productos: {data.get('total', '?')}\n"
               f"Con imagen IA: {data.get('con_imagen_ia', '?')}\n"
               f"Dólar blue: ${blue}")
    trigger_n8n({"catalogo_url": f"{SUPA_URL}/storage/v1/object/public/{BUCKET}/data/catalogo.json",
                 "resumen": resumen, "changed_pdfs": [p.name for p in changed]})
    notify_telegram(resumen)
    log("=== PIPELINE — fin ===")


if __name__ == "__main__":
    main()
