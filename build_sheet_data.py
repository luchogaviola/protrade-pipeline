"""
Construye el dataset final para el Google Sheet ProTrade.
Combina: productos parseados + URLs Supabase (proveedor) + override manual (user IA).

Columnas de salida (orden del sheet):
  IMAGEN | COD. ART. | RUBRO | ARTICULO | COSTO USD | UN. x BULTO |
  imagen_proveedor | imagen_manual | imagen_contexto | precio_manual | activo | ultima_act

Las fórmulas (COSTO ARS, PRECIO +30%, EFECTIVO, TRANSFERENCIA) las pone el sheet.
"""
import json
import re
import csv
import urllib.request
from datetime import date
from pathlib import Path

OUT = Path(r"C:\Users\lucho\Downloads\protrade-pipeline\output")
USER_IMGS = Path(r"C:\tmp\protrade-ia-full")
SUPA_PUBLIC = "https://arjvysbtvznibqjexcek.supabase.co/storage/v1/object/public/protrade-productos"
CATEGORIAS_JSON = Path(__file__).parent / "categorias.json"

# Google Sheet: lectura de ediciones manuales del user (precio_manual, imagen_manual).
# El user edita en el Sheet → el pipeline las lee y las propaga al JSON → la web las respeta.
SHEET_ID = "1VkumWHXdcaYXolwMoK9qSr9VSoCr-u5TQGZ8B7LhloE"
SHEET_GID = "1028595591"


def fetch_sheet_overrides() -> dict:
    """Lee el Sheet público vía gviz CSV y devuelve {sku: {precio_manual, imagen_manual}}."""
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[overrides] no se pudo leer Sheet ({e}); sigo sin overrides")
        return {}

    rows = list(csv.reader(text.splitlines()))
    if not rows:
        return {}
    header = rows[0]
    def idx(name):
        return header.index(name) if name in header else -1
    i_sku = idx("COD. ART.")
    i_pre = idx("precio_manual")
    i_img = idx("imagen_manual")
    if i_sku < 0:
        print("[overrides] columna 'COD. ART.' no encontrada en Sheet")
        return {}

    overrides = {}
    for row in rows[1:]:
        if len(row) <= i_sku:
            continue
        sku = (row[i_sku] or "").strip()
        if not sku:
            continue
        ov = {}
        if i_pre >= 0 and len(row) > i_pre:
            pm = (row[i_pre] or "").strip()
            if pm:
                ov["precio_manual"] = pm
        if i_img >= 0 and len(row) > i_img:
            im = (row[i_img] or "").strip()
            if im:
                ov["imagen_manual"] = im
        if ov:
            overrides[sku] = ov
    return overrides


def safe_sku(s: str) -> str:
    return re.sub(r"[^\w\-.]", "_", s)


def load_categorias():
    """Carga el mapeo PDF → categoría/sub_categoria desde categorias.json"""
    if not CATEGORIAS_JSON.exists():
        return [], "Otros", ""
    cfg = json.loads(CATEGORIAS_JSON.read_text(encoding="utf-8"))
    return (
        cfg.get("mapping", []),
        cfg.get("_default_categoria", "Otros"),
        cfg.get("_default_subcategoria", ""),
    )


def map_categoria(pdf_source: str, mapping: list, default_cat: str, default_sub: str):
    """Dado el filename del PDF, busca la primera regla que matchee (substring case-insensitive)."""
    if not pdf_source:
        return default_cat, default_sub
    name = pdf_source.lower()
    for rule in mapping:
        if rule.get("pattern", "").lower() in name:
            return rule.get("categoria", default_cat), rule.get("sub_categoria", default_sub)
    return default_cat, default_sub


def scan_user_skus() -> dict:
    """Mapea SKU → {portada: bool, contexto: bool} escaneando carpetas del user."""
    result = {}
    if not USER_IMGS.exists():
        return result
    for folder in USER_IMGS.rglob("*"):
        if not folder.is_dir():
            continue
        m = re.match(r"^([A-Z0-9][\w\-./]+?)\s*-\s+", folder.name)
        if not m:
            continue
        sku = m.group(1).strip()
        has_portada = (folder / "portada.png").exists()
        has_contexto = (folder / "contexto.png").exists()
        if has_portada or has_contexto:
            result[sku] = {"portada": has_portada, "contexto": has_contexto}
    return result


def main():
    data = json.loads((OUT / "all-products.json").read_text(encoding="utf-8"))
    products = data["products"]
    blue = data["dolar_blue_venta"]

    user_skus = scan_user_skus()
    mapping, def_cat, def_sub = load_categorias()
    overrides = fetch_sheet_overrides()
    print(f"Productos: {len(products)} | SKUs con imagen IA del user: {len(user_skus)} | reglas de categorías: {len(mapping)} | overrides manuales del Sheet: {len(overrides)}")

    hoy = date.today().isoformat()
    rows = []
    # Dedup por SKU (último gana)
    by_sku = {}
    for p in products:
        by_sku[p["sku"]] = p

    matched_manual = 0
    matched_overrides_precio = 0
    matched_overrides_imagen = 0
    categorias_count = {}
    for sku, p in by_sku.items():
        sku_s = safe_sku(sku)
        img_prov = f"{SUPA_PUBLIC}/proveedor/{sku_s}.png"
        img_manual = ""
        img_contexto = ""
        if sku in user_skus:
            if user_skus[sku]["portada"]:
                img_manual = f"{SUPA_PUBLIC}/manual/{sku_s}/portada.png"
                matched_manual += 1
            if user_skus[sku]["contexto"]:
                img_contexto = f"{SUPA_PUBLIC}/manual/{sku_s}/contexto.png"

        # Override manual desde el Sheet (las ediciones del user mandan)
        ov = overrides.get(sku, {})
        precio_manual_val = ov.get("precio_manual", "")
        if precio_manual_val:
            matched_overrides_precio += 1
        if ov.get("imagen_manual"):
            img_manual = ov["imagen_manual"]
            matched_overrides_imagen += 1

        imagen_final = img_manual or img_prov

        # Mapeo de categoría unificada (taxonomía propia, independiente del nombre del PDF)
        categoria, sub_categoria = map_categoria(p.get("pdf_source", ""), mapping, def_cat, def_sub)
        categorias_count[categoria] = categorias_count.get(categoria, 0) + 1

        rows.append({
            "IMAGEN": imagen_final,
            "COD. ART.": sku,
            "CATEGORIA": categoria,
            "SUB_CATEGORIA": sub_categoria,
            "RUBRO_PDF": p["rubro"],
            "ARTICULO": p["descripcion"],
            "COSTO USD": p["costo_usd"],
            "UN. x BULTO": p["bulto"],
            "imagen_proveedor": img_prov,
            "imagen_manual": img_manual,
            "imagen_contexto": img_contexto,
            "precio_manual": precio_manual_val,
            "activo": "TRUE",
            "ultima_act": hoy,
        })

    # Escribir CSV
    csv_path = OUT / "sheet-data.csv"
    cols = ["IMAGEN", "COD. ART.", "CATEGORIA", "SUB_CATEGORIA", "RUBRO_PDF", "ARTICULO",
            "COSTO USD", "UN. x BULTO",
            "imagen_proveedor", "imagen_manual", "imagen_contexto",
            "precio_manual", "activo", "ultima_act"]
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    # Escribir JSON (para n8n)
    json_path = OUT / "sheet-data.json"
    json_path.write_text(json.dumps({
        "dolar_blue_venta": blue,
        "fecha": hoy,
        "total": len(rows),
        "con_imagen_ia": matched_manual,
        "columnas": cols,
        "categorias_count": categorias_count,
        "rows": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nTotal filas: {len(rows)}")
    print(f"Con imagen IA del user (override): {matched_manual}")
    print(f"Solo imagen proveedor: {len(rows) - matched_manual}")
    print(f"Overrides aplicados desde Sheet -> precio_manual: {matched_overrides_precio} | imagen_manual: {matched_overrides_imagen}")
    print(f"\nCategorías unificadas:")
    for cat, n in sorted(categorias_count.items(), key=lambda x: -x[1]):
        print(f"  {n:>5}  {cat}")
    print(f"\nCSV:  {csv_path}")
    print(f"JSON: {json_path}")


if __name__ == "__main__":
    main()
