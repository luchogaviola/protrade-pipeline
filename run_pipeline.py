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
    log(f"Descargando PDFs del Drive {DRIVE_FOLDER}...")
    subprocess.run(
        ["python", "-m", "gdown", "--folder",
         f"https://drive.google.com/drive/folders/{DRIVE_FOLDER}",
         "-O", str(PDF_DIR)],
        check=False, timeout=600,
    )
    pdfs = list(PDF_DIR.rglob("*.pdf"))
    log(f"  {len(pdfs)} PDFs descargados")
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
        log("Sin PDFs. Abortando.")
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

    log(f"Total productos parseados: {len(all_products)}")

    # Guardar all-products para los otros scripts
    (OUT_DIR / "all-products.json").write_text(
        json.dumps({"dolar_blue_venta": blue, "total_productos": len(all_products),
                    "products": all_products}, ensure_ascii=False), encoding="utf-8")

    # 4. Subir imágenes proveedor (idempotente)
    upload_proveedor_images(all_products, OUT_DIR / "images")

    # 5. Generar catalogo.json + subir a Supabase
    subprocess.run(["python", "build_sheet_data.py"], cwd=str(Path(__file__).parent),
                   check=False, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    catalog = OUT_DIR / "sheet-data.json"
    if catalog.exists():
        url = f"{SUPA_URL}/storage/v1/object/{BUCKET}/data/catalogo.json"
        requests.post(url, headers={"Authorization": f"Bearer {SUPA_KEY}",
                                    "Content-Type": "application/json", "x-upsert": "true"},
                      data=catalog.read_bytes(), timeout=60)
        log("  catalogo.json subido a Supabase")

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
