"""
ProTrade PDF parser PoC.

Lee un PDF de catálogo del proveedor, extrae:
- Tabla estructurada: SKU, descripción, bulto, costo USD
- Imágenes embebidas por página
- Asocia cada producto a la imagen más cercana en Y de su página

Output: JSON listo para upsert a Google Sheets.
"""
from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF
import pdfplumber
import requests

# Forzar utf-8 en stdout (Windows cp1252 rompe acentos/chino)
sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1)


@dataclass
class Product:
    sku: str
    descripcion: str
    bulto: int
    costo_usd: float
    rubro: str
    pagina: int
    imagen_local: Optional[str] = None
    imagen_filename: Optional[str] = None  # nombre final ${sku}.png
    y_position: float = 0.0


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^\w\s-]", "", s).strip().lower()
    s = re.sub(r"[-\s]+", "-", s)
    return s


def infer_rubro_from_filename(filename: str) -> str:
    """Heurística: agarrar las primeras palabras significativas del filename."""
    name = Path(filename).stem
    # Sacar año al inicio (2025, 2026)
    name = re.sub(r"^\s*20\d{2}\s*", "", name)
    # Sacar fechas dd.mm o dd.mm.yyyy (siempre al final, separadas por espacio)
    name = re.sub(r"\s+\d{1,2}\.\d{1,2}(\.\d{2,4})?\s*$", "", name)
    # Sacar fechas con guión
    name = re.sub(r"\s+\d{1,2}-\d{1,2}\s*$", "", name)
    # Casos especiales: "2026Art.belle23.05" → quitar el año pegado y la fecha
    name = re.sub(r"^Art\.", "Art ", name)
    return name.strip().title() or "Sin rubro"


# Patrón estándar de columnas de los PDFs del proveedor (orden fijo conocido)
DEFAULT_COL_IDX = {"foto": 0, "sku": 1, "chino": 2, "desc": 3, "bulto": 4, "costo": 5}


def fetch_dolar_blue_venta() -> float:
    try:
        r = requests.get("https://dolarapi.com/v1/dolares/blue", timeout=10)
        r.raise_for_status()
        return float(r.json()["venta"])
    except Exception as e:
        print(f"[warn] No pude obtener dolar blue: {e}. Uso 1200 default.", file=sys.stderr)
        return 1200.0


def parse_pdf(pdf_path: Path, out_dir: Path) -> list[Product]:
    rubro = infer_rubro_from_filename(pdf_path.name)
    print(f"\n=== Parseando: {pdf_path.name} ===")
    print(f"Rubro inferido: {rubro}")

    img_dir = out_dir / "images" / slugify(pdf_path.stem)
    img_dir.mkdir(parents=True, exist_ok=True)

    # 1) Extraer todas las imágenes con su bbox (posición Y) por página
    doc = fitz.open(str(pdf_path))
    images_per_page: dict[int, list[dict]] = {}
    for page_num, page in enumerate(doc):
        page_imgs = []
        for img_idx, img in enumerate(page.get_images(full=True)):
            xref = img[0]
            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.n - pix.alpha >= 4:  # CMYK → RGB
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                if pix.width < 200 or pix.height < 200:
                    continue  # descartar logos/iconos chicos
                rects = page.get_image_rects(xref)
                y_center = float(rects[0].y0 + rects[0].y1) / 2 if rects else 0.0
                fname = img_dir / f"page{page_num:02d}_img{img_idx:02d}.png"
                pix.save(str(fname))
                w, h = pix.width, pix.height
                pix = None  # liberar memoria del Pixmap inmediatamente (clave para 512MB)
                page_imgs.append({"path": str(fname), "y": y_center, "w": w, "h": h})
            except Exception as e:
                print(f"[warn] skip image p{page_num} i{img_idx}: {e}", file=sys.stderr)
        # Ordenar por Y ascendente (de arriba abajo)
        page_imgs.sort(key=lambda x: x["y"])
        images_per_page[page_num] = page_imgs
        print(f"  page {page_num}: {len(page_imgs)} imágenes válidas (>200px)")
    doc.close()

    # 2) Pre-scan: buscar header en las primeras 3 filas de cada tabla de cada página
    cached_col_idx: dict[str, int] = {}
    n_cols_seen = 0
    with pdfplumber.open(str(pdf_path)) as p:
        for page in p.pages:
            if cached_col_idx:
                break
            for t in page.find_tables():
                rows = t.extract()
                if not rows:
                    continue
                if not n_cols_seen and rows:
                    n_cols_seen = max(len(r) for r in rows[:3])
                # Probar las primeras 3 filas como posible header
                for candidate_row in rows[:3]:
                    header = [(c or "").strip() for c in candidate_row]
                    detected = {}
                    for i, h in enumerate(header):
                        hl = h.lower()
                        if "codigo" in hl or "código" in hl:
                            detected["sku"] = i
                        elif "descrip" in hl:
                            detected["desc"] = i
                        elif "bulto" in hl or "unidad" in hl or hl.startswith("uni") or "/b" in hl:
                            detected["bulto"] = i
                        elif "precio" in hl:
                            detected["costo"] = i
                    if all(k in detected for k in ("sku", "desc", "bulto", "costo")):
                        cached_col_idx = detected
                        break

    # Fallback inteligente según número de columnas observadas
    if not cached_col_idx:
        if n_cols_seen == 5:
            # Foto | Codigo | Descripcion | Uni/B | Precio
            cached_col_idx = {"sku": 1, "desc": 2, "bulto": 3, "costo": 4}
        elif n_cols_seen == 6:
            # Foto | Codigo | Chino | Descripcion | Bulto | Precio
            cached_col_idx = {"sku": 1, "desc": 3, "bulto": 4, "costo": 5}
        else:
            cached_col_idx = {"sku": 1, "desc": 3, "bulto": 4, "costo": 5}
        print(f"  (sin header detectado, n_cols={n_cols_seen}, uso {cached_col_idx})")
    else:
        print(f"  header detectado: {cached_col_idx}")

    # 3) Procesar cada tabla con el mapping global del PDF
    products: list[Product] = []

    def looks_like_data_row(row: list) -> bool:
        """Una fila parece data si la columna SKU tiene patrón [LETRA]-[NUMERO]."""
        if cached_col_idx["sku"] >= len(row):
            return False
        cell = (row[cached_col_idx["sku"]] or "").strip()
        return bool(re.match(r"^[A-Z]+[-]?\d+", cell))

    with pdfplumber.open(str(pdf_path)) as p:
        for page_num, page in enumerate(p.pages):
            tables = page.find_tables()
            if not tables:
                continue
            for t in tables:
                rows = t.extract()
                if not rows:
                    continue

                col_idx = cached_col_idx
                # Si la primera fila es header (no es data) saltarla; sino usar todas
                first_is_data = looks_like_data_row(rows[0])
                data_rows = rows if first_is_data else rows[1:]

                top = float(t.bbox[1])
                bottom = float(t.bbox[3])
                step = (bottom - top) / max(len(data_rows), 1)

                for r_idx, row in enumerate(data_rows):
                    # Guard: la fila puede tener menos columnas que el header esperado
                    max_idx = max(col_idx.values())
                    if len(row) <= max_idx:
                        continue
                    sku_raw = (row[col_idx["sku"]] or "").strip()
                    if not sku_raw:
                        continue
                    # Limpiar SKU: primera línea, sacar caracteres no-ASCII (chino),
                    # y quedarse con el patrón válido [LETRAS]-[alfanum/./]
                    sku_raw = sku_raw.split("\n")[0]
                    sku_raw = re.sub(r"[^\x00-\x7F]", "", sku_raw).strip()
                    m_sku = re.match(r"^([A-Za-z]+[-]?[\w/.\-]*)", sku_raw)
                    if m_sku:
                        sku_raw = m_sku.group(1).rstrip("-/.").strip()
                    if not sku_raw or not re.match(r"^[A-Za-z]", sku_raw):
                        continue
                    desc = (row[col_idx["desc"]] or "").strip().replace("\n", " ")
                    bulto_raw = (row[col_idx["bulto"]] or "").strip().replace(".", "")
                    costo_raw = (row[col_idx["costo"]] or "").strip().replace(",", ".")
                    try:
                        bulto = int(re.sub(r"\D", "", bulto_raw) or 0)
                        costo = float(re.sub(r"[^\d.]", "", costo_raw) or 0)
                    except ValueError:
                        continue
                    if bulto <= 0 or costo <= 0:
                        continue
                    y_pos = top + step * (r_idx + 0.5)
                    products.append(Product(
                        sku=sku_raw,
                        descripcion=desc,
                        bulto=bulto,
                        costo_usd=costo,
                        rubro=rubro,
                        pagina=page_num,
                        y_position=y_pos,
                    ))

    # 3) Asociar cada producto a la imagen más cercana de SU página
    for prod in products:
        page_imgs = images_per_page.get(prod.pagina, [])
        if not page_imgs:
            continue
        # imagen con Y más cercano al Y del producto
        best = min(page_imgs, key=lambda img: abs(img["y"] - prod.y_position))
        prod.imagen_local = best["path"]
        # nombre final: {sku safe}.png
        safe_sku = re.sub(r"[^\w\-.]", "_", prod.sku)
        prod.imagen_filename = f"{safe_sku}.png"

    return products


def main():
    pdfs_dir = Path("C:/tmp/protrade-pdfs/Catálogos")
    if not pdfs_dir.exists():
        print(f"ERROR: no encuentro {pdfs_dir}", file=sys.stderr)
        sys.exit(1)

    out_dir = Path("C:/Users/lucho/Downloads/protrade-pipeline/output")
    out_dir.mkdir(parents=True, exist_ok=True)

    blue = fetch_dolar_blue_venta()
    print(f"Dólar blue venta: ARS {blue}\n")

    all_pdfs = sorted(pdfs_dir.glob("*.pdf"))
    print(f"=== BATCH: {len(all_pdfs)} PDFs a procesar ===\n")

    summary = []
    all_products = []
    failed = []

    for i, pdf in enumerate(all_pdfs, 1):
        try:
            print(f"[{i}/{len(all_pdfs)}] {pdf.name}")
            products = parse_pdf(pdf, out_dir)
            summary.append({
                "pdf": pdf.name,
                "rubro": products[0].rubro if products else "?",
                "productos": len(products),
            })
            for p in products:
                d = asdict(p)
                d["y_position"] = round(p.y_position, 1)
                d["costo_ars"] = round(p.costo_usd * blue, 0)
                d["precio_30"] = round(p.costo_usd * blue * 1.20, 0)  # margen 20% (field name kept for compat)
                d["pdf_source"] = pdf.name
                d["imagen_local"] = (Path(p.imagen_local).name if p.imagen_local else None)
                all_products.append(d)
        except Exception as e:
            print(f"  ERROR: {e}")
            failed.append({"pdf": pdf.name, "error": str(e)})

    consolidated = out_dir / "all-products.json"
    consolidated.write_text(json.dumps({
        "dolar_blue_venta": blue,
        "total_productos": len(all_products),
        "por_pdf": summary,
        "failed": failed,
        "products": all_products,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'='*60}")
    print(f"BATCH COMPLETO")
    print(f"{'='*60}")
    print(f"PDFs OK:    {len(summary)}/{len(all_pdfs)}")
    print(f"Productos:  {len(all_products)}")
    print(f"Imágenes:   {out_dir / 'images'}")
    print(f"JSON:       {consolidated}")
    print(f"\nResumen por rubro:")
    for s in summary:
        print(f"  {s['productos']:>4} productos  |  {s['rubro']:<25}  ({s['pdf']})")
    if failed:
        print(f"\nFALLAS:")
        for f in failed:
            print(f"  {f['pdf']}: {f['error']}")


if __name__ == "__main__":
    main()
