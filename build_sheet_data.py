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
from datetime import date
from pathlib import Path

OUT = Path(r"C:\Users\lucho\Downloads\protrade-pipeline\output")
USER_IMGS = Path(r"C:\tmp\protrade-ia-full")
SUPA_PUBLIC = "https://arjvysbtvznibqjexcek.supabase.co/storage/v1/object/public/protrade-productos"
CATEGORIAS_JSON = Path(__file__).parent / "categorias.json"


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
    print(f"Productos: {len(products)} | SKUs con imagen IA del user: {len(user_skus)} | reglas de categorías: {len(mapping)}")

    hoy = date.today().isoformat()
    rows = []
    # Dedup por SKU (último gana)
    by_sku = {}
    for p in products:
        by_sku[p["sku"]] = p

    matched_manual = 0
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
            "precio_manual": "",
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
    print(f"\nCategorías unificadas:")
    for cat, n in sorted(categorias_count.items(), key=lambda x: -x[1]):
        print(f"  {n:>5}  {cat}")
    print(f"\nCSV:  {csv_path}")
    print(f"JSON: {json_path}")


if __name__ == "__main__":
    main()
