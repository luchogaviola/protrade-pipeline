"""
Sube imágenes a Supabase Storage en 2 carpetas:
- proveedor/{sku}.png  → imagen original del PDF (siempre)
- manual/{sku}/portada.png + contexto.png  → imágenes pre-generadas del user (si existen)

Idempotente: si ya existe, hace upsert (sobrescribe).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
import requests


SUPA_URL = os.environ.get("SUPABASE_URL", "https://arjvysbtvznibqjexcek.supabase.co")
SUPA_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
BUCKET = os.environ.get("SUPABASE_BUCKET", "protrade-productos")

import os
os.environ["PYTHONIOENCODING"] = "utf-8"


def safe_sku(sku: str) -> str:
    return re.sub(r"[^\w\-.]", "_", sku)


def upload_one(path: Path, storage_key: str, content_type: str = "image/png") -> tuple[bool, str]:
    """POST + sets upsert=true header to replace if exists."""
    url = f"{SUPA_URL}/storage/v1/object/{BUCKET}/{storage_key}"
    headers = {
        "Authorization": f"Bearer {SUPA_KEY}",
        "Content-Type": content_type,
        "x-upsert": "true",
    }
    try:
        with open(path, "rb") as f:
            r = requests.post(url, headers=headers, data=f.read(), timeout=60)
        if r.status_code in (200, 201):
            return True, storage_key
        return False, f"{storage_key} | HTTP {r.status_code} {r.text[:120]}"
    except Exception as e:
        return False, f"{storage_key} | {type(e).__name__}: {e}"


def upload_proveedor_images(products: list[dict], img_root: Path, max_workers: int = 8) -> dict:
    """Sube todas las imágenes locales de productos a proveedor/{sku}.png"""
    tasks = []
    for p in products:
        if not p.get("imagen_local"):
            continue
        # img_root contiene subcarpetas por PDF: images/{slug}/page0_imgN.png
        pdf_slug = re.sub(r"[^\w\-]", "-", Path(p["pdf_source"]).stem.lower())
        # slugify ya lo hizo el parser; reconstruimos igual
        from parse_pdf import slugify
        slug = slugify(Path(p["pdf_source"]).stem)
        local = img_root / slug / p["imagen_local"]
        if not local.exists():
            continue
        sku_s = safe_sku(p["sku"])
        key = f"proveedor/{sku_s}.png"
        tasks.append((local, key))

    print(f"Subiendo {len(tasks)} imágenes del proveedor a Supabase...")
    ok = 0
    fail = 0
    failures = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(upload_one, path, key) for path, key in tasks]
        for i, fut in enumerate(as_completed(futures), 1):
            success, msg = fut.result()
            if success:
                ok += 1
            else:
                fail += 1
                failures.append(msg)
            if i % 100 == 0:
                print(f"  Progreso: {i}/{len(tasks)} (ok={ok} fail={fail})")
    print(f"\nProveedor: OK={ok} FAIL={fail}")
    return {"ok": ok, "fail": fail, "failures": failures[:20]}


def upload_user_images(user_dir: Path, max_workers: int = 8) -> dict:
    """
    Escanea recursivamente user_dir buscando carpetas con formato
    '{SKU} - {descripcion}' que contengan portada.png y/o contexto.png.
    """
    tasks = []
    found = 0
    for folder in user_dir.rglob("*"):
        if not folder.is_dir():
            continue
        name = folder.name
        # match SKU al inicio: alphanumerics + posibles guiones, antes de ' - '
        m = re.match(r"^([A-Z0-9][\w\-./]+?)\s*-\s+", name)
        if not m:
            continue
        sku = m.group(1).strip()
        sku_s = safe_sku(sku)
        for label in ("portada.png", "contexto.png"):
            f = folder / label
            if f.exists():
                key = f"manual/{sku_s}/{label}"
                tasks.append((f, key, sku))
        found += 1

    print(f"\nEncontrados {found} folders de productos pre-generados ({len(tasks)} archivos)")
    if not tasks:
        return {"ok": 0, "fail": 0, "skus": []}

    ok = 0
    fail = 0
    skus_set = set()
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(upload_one, path, key) for path, key, _ in tasks]
        for i, fut in enumerate(as_completed(futures), 1):
            success, msg = fut.result()
            if success:
                ok += 1
            else:
                fail += 1
            if i % 50 == 0:
                print(f"  Progreso manual: {i}/{len(tasks)} (ok={ok} fail={fail})")
    for _, _, sku in tasks:
        skus_set.add(sku)
    print(f"\nManual user: OK={ok} FAIL={fail} | SKUs únicos: {len(skus_set)}")
    return {"ok": ok, "fail": fail, "skus": sorted(skus_set)}


def main():
    out_dir = Path(r"C:\Users\lucho\Downloads\protrade-pipeline\output")
    user_dir = Path(r"C:\tmp\protrade-user-imgs")
    data = json.loads((out_dir / "all-products.json").read_text(encoding="utf-8"))
    products = data["products"]
    print(f"Productos en JSON: {len(products)}")

    # 1) Subir imágenes del proveedor
    img_root = out_dir / "images"
    r1 = upload_proveedor_images(products, img_root)

    # 2) Subir pre-generadas del user
    r2 = upload_user_images(user_dir)

    # 3) Resumen final
    summary = {
        "proveedor": r1,
        "manual_user": r2,
        "bucket": BUCKET,
        "url_pattern_proveedor": f"{SUPA_URL}/storage/v1/object/public/{BUCKET}/proveedor/{{sku}}.png",
        "url_pattern_manual": f"{SUPA_URL}/storage/v1/object/public/{BUCKET}/manual/{{sku}}/portada.png",
    }
    summary_path = out_dir / "supabase-upload-summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n=== RESUMEN ===")
    print(f"Proveedor:  OK={r1['ok']} FAIL={r1['fail']}")
    print(f"Manual:     OK={r2['ok']} FAIL={r2['fail']} ({len(r2['skus'])} SKUs)")
    print(f"\nSummary: {summary_path}")


if __name__ == "__main__":
    main()
