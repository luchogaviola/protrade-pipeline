"""
Pipeline ProTrade — sincroniza el catálogo desde la API del proveedor (Lambo Tech / HodERP).

Reemplaza el viejo flujo Drive+PDF+parse_pdf. Ahora:
1. Trae todos los productos de la API JSON del proveedor (storeId 971).
2. Filtra: solo vendibles (saleable) y con stock (no outofStock, statStock>0).
3. Mapea cada producto al schema de catalogo.json que lee la web.
4. Calcula el precio final ARS = costo × dólar blue venta del día × margen (+20%).
   - La mayoría de los precios del proveedor están en USD.
   - Algunos vienen en PESOS (marcados "PRECIO FINAL / EFECTIVO / pesos" en el título): esos NO se multiplican por el dólar.
5. Categorías = las 17 del proveedor (category1/category2) + overlay "Mundial" (selección/AFA).
6. Imágenes = URL del proveedor, con capa override por SKU (Nano Banana / IA en bucket Supabase manual/).
7. Sube catalogo.json a Supabase (con gate de sanidad).

Corre en easypanel por cron (Dockerfile). Config por env vars.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime

import requests

# ---------- Config ----------
API_BASE = os.environ.get("HOD_API_BASE", "https://hds-api.hoderp.com/mall")
STORE_ID = os.environ.get("HOD_STORE_ID", "971")
CURRENCY_ID = os.environ.get("HOD_CURRENCY_ID", "1")  # 1 = precio base (USD)
MARGIN = float(os.environ.get("MARGIN", "1.20"))      # +20%

SUPA_URL = os.environ.get("SUPABASE_URL", "https://arjvysbtvznibqjexcek.supabase.co")
SUPA_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")  # vacío en local -> no sube, escribe a disco
BUCKET = os.environ.get("SUPABASE_BUCKET", "protrade-productos")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

OUT_FILE = os.environ.get("OUT_FILE", "catalogo.json")  # local fallback

# Gate de sanidad: no pisar el catálogo bueno con uno roto.
MIN_PRODUCTOS = int(os.environ.get("MIN_PRODUCTOS", "1000"))

SUPA_PUBLIC = f"{SUPA_URL}/storage/v1/object/public/{BUCKET}"

# Productos cargados en PESOS por el proveedor (no multiplicar por dólar).
ARS_RE = re.compile(r"precio\s*final|efectivo|\bpesos\b|\bar\$", re.I)
# Overlay Mundial: selección argentina / AFA / mundial / bandera argentina.
MUNDIAL_RE = re.compile(
    r"(camiseta.*(seleccion|selecci[oó]n|argentin|mundial))|"
    r"(bandera.*argentin)|\bafa\b|seleccion\s*argentina|selecci[oó]n\s*argentina|\bmundial\b",
    re.I,
)


def log(msg: str):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def fetch_dolar_blue_venta() -> float:
    try:
        r = requests.get("https://dolarapi.com/v1/dolares/blue", timeout=15)
        r.raise_for_status()
        return float(r.json()["venta"])
    except Exception as e:
        log(f"[warn] no pude obtener dólar blue: {e}. Uso 1200 default.")
        return 1200.0


def fetch_products() -> list[dict]:
    """Trae TODOS los productos del proveedor paginando la API."""
    out: list[dict] = []
    page = 1
    while True:
        r = requests.get(
            f"{API_BASE}/products",
            params={
                "page": page, "pagesize": 500, "categoryId": -1,
                "keyword": "", "mark": "all",
                "currencyId": CURRENCY_ID, "storeId": STORE_ID,
            },
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=60,
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        lst = data.get("list") or data.get("records") or []
        out.extend(lst)
        total = data.get("total", len(out))
        log(f"  página {page}: {len(lst)} productos (acumulado {len(out)}/{total})")
        if not lst or len(out) >= total:
            break
        page += 1
    return out


def list_manual_overrides() -> set[str]:
    """SKUs que tienen imagen editada (Nano Banana / IA) en el bucket Supabase manual/{SKU}/.
    Si no hay service key (local) o falla, devuelve set vacío (usa imagen del proveedor)."""
    if not SUPA_KEY:
        return set()
    skus: set[str] = set()
    try:
        url = f"{SUPA_URL}/storage/v1/object/list/{BUCKET}"
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {SUPA_KEY}", "Content-Type": "application/json"},
            json={"prefix": "manual/", "limit": 100000},
            timeout=60,
        )
        if r.status_code < 300:
            for item in r.json():
                name = item.get("name", "")
                # estructura: manual/{SKU}/portada.png  -> el list con prefix manual/ devuelve carpetas {SKU}
                sku = name.split("/")[0] if "/" in name else name
                if sku:
                    skus.add(sku)
        else:
            log(f"[warn] list manual/ -> {r.status_code}")
    except Exception as e:
        log(f"[warn] no pude listar overrides manual/: {e}")
    return skus


def safe_sku(s: str) -> str:
    return re.sub(r"[^\w\-.]", "_", s)


def build_catalog(products: list[dict], blue: float, manual_skus: set[str]) -> dict:
    hoy = date.today().isoformat()
    rows = []
    cat_count: dict[str, int] = {}
    con_img_manual = 0
    n_ars = 0
    for p in products:
        if not p.get("saleable"):
            continue
        if p.get("outofStock"):
            continue
        stock = p.get("statStock", 0) or 0
        if stock <= 0:
            continue
        sku = str(p.get("productNumber") or "").strip()
        if not sku:
            continue
        title = (p.get("title2") or p.get("title1") or "").strip()
        if not title:
            continue
        price1 = float(p.get("price1") or 0)
        if price1 <= 0:
            continue
        bulto = int(p.get("packingBox") or 0) or 1

        blob = f"{p.get('title1','')} {title} {p.get('description1','')}"
        es_ars = bool(ARS_RE.search(blob))

        # Precio final ARS (margen +20%). USD -> x dólar; ARS -> directo.
        if es_ars:
            costo_usd = 0.0
            precio_final = round(price1 * MARGIN)
            n_ars += 1
        else:
            costo_usd = round(price1, 4)
            precio_final = round(price1 * blue * MARGIN)

        # Categoría: la del proveedor + overlay Mundial.
        cat = ((p.get("category1") or {}).get("name") or "Otros").strip()
        sub = ((p.get("category2") or {}).get("name") or "").strip()
        if MUNDIAL_RE.search(blob):
            cat, sub = "Mundial", "Selección / AFA"

        # Imagen: override Nano Banana/IA (bucket manual/) si existe, sino la del proveedor.
        prov_img = ""
        imgs = p.get("images") or []
        if imgs and isinstance(imgs[0], dict):
            prov_img = imgs[0].get("url", "") or ""
        img_manual = ""
        if sku in manual_skus:
            img_manual = f"{SUPA_PUBLIC}/manual/{safe_sku(sku)}/portada.png"
            con_img_manual += 1
        imagen = img_manual or prov_img

        rows.append({
            "IMAGEN": imagen,
            "COD. ART.": sku,
            "CATEGORIA": cat,
            "SUB_CATEGORIA": sub,
            "RUBRO_PDF": cat,            # compat con la web (fallback)
            "ARTICULO": title,
            "COSTO USD": costo_usd,
            "UN. x BULTO": bulto,
            "imagen_proveedor": prov_img,
            "imagen_manual": img_manual,
            "imagen_contexto": "",
            "precio_manual": precio_final,   # precio final ARS ya calculado (la web lo usa directo)
            "activo": "TRUE",
            "ultima_act": hoy,
            "stock": stock,
        })
        cat_count[cat] = cat_count.get(cat, 0) + 1

    return {
        "dolar_blue_venta": blue,
        "fecha": hoy,
        "total": len(rows),
        "con_imagen_ia": con_img_manual,
        "productos_en_pesos": n_ars,
        "columnas": list(rows[0].keys()) if rows else [],
        "categorias_count": cat_count,
        "rows": rows,
    }


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


def main():
    log("=== SYNC HODMALL — inicio ===")
    products = fetch_products()
    if len(products) < MIN_PRODUCTOS:
        msg = f"ABORTADO: la API devolvió {len(products)} productos (mín {MIN_PRODUCTOS}). Catálogo INTACTO."
        log(msg)
        notify_telegram(f"⚠️ *Sync ProTrade* — {msg}")
        return

    blue = fetch_dolar_blue_venta()
    log(f"Dólar blue venta: {blue}")
    manual_skus = list_manual_overrides()
    log(f"Overrides de imagen (Nano Banana/IA): {len(manual_skus)}")

    catalog = build_catalog(products, blue, manual_skus)
    log(f"Catálogo armado: {catalog['total']} productos | en pesos: {catalog['productos_en_pesos']} | "
        f"con imagen override: {catalog['con_imagen_ia']} | categorías: {len(catalog['categorias_count'])}")

    # Gate de sanidad
    if catalog["total"] < MIN_PRODUCTOS:
        msg = f"RECHAZADO: catálogo nuevo con {catalog['total']} productos (mín {MIN_PRODUCTOS}). Prod INTACTO."
        log(msg)
        notify_telegram(f"🔴 *Sync ProTrade* — {msg}")
        return

    payload = json.dumps(catalog, ensure_ascii=False).encode("utf-8")

    if not SUPA_KEY:
        with open(OUT_FILE, "wb") as f:
            f.write(payload)
        log(f"[local] sin SUPABASE_SERVICE_KEY -> escrito a {OUT_FILE} ({len(payload)} bytes). No subo.")
        return

    url = f"{SUPA_URL}/storage/v1/object/{BUCKET}/data/catalogo.json"
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {SUPA_KEY}", "Content-Type": "application/json", "x-upsert": "true"},
        data=payload, timeout=60,
    )
    if resp.status_code >= 300:
        msg = f"upload a Supabase falló ({resp.status_code}). Catálogo previo intacto."
        log(f"  {msg}: {resp.text[:200]}")
        notify_telegram(f"🔴 *Sync ProTrade* — {msg}")
        return
    log(f"  catalogo.json subido ({catalog['total']} productos, dólar {blue})")
    resumen = (f"*Sync ProTrade {catalog['fecha']}*\n"
               f"Productos: {catalog['total']}\n"
               f"Dólar venta: ${blue}\n"
               f"Margen: +{round((MARGIN-1)*100)}%")
    notify_telegram(resumen)
    log("=== SYNC HODMALL — fin ===")


if __name__ == "__main__":
    main()
