# Pro Trade Pipeline — Manual del proyecto

> Este archivo lo carga automáticamente Claude Code al abrir el repo. Es la "biblia" del pipeline: reglas de negocio, arquitectura, lo que está prohibido tocar, gotchas conocidos. Si algo cambia, actualizá este archivo en el mismo PR.

---

## Qué hace este pipeline

**Regenera el catálogo de productos de Pro Trade Group todos los días a las 8 AM.** Es el motor que mantiene actualizado `https://protradegroup.shop` (catálogo B2B mayorista) y el Google Sheet editable del cliente.

Flujo end-to-end:

```
PDFs del proveedor (Google Drive)
   ↓ gdown (descarga)
parse_pdf.py (pdfplumber + PyMuPDF)
   ↓ extrae SKU + descripción + costo + bulto + imagen embebida
upload_supabase.py (sube imágenes de proveedor a bucket protrade-productos/proveedor/)
   ↓
build_sheet_data.py
   ├─ Lee Google Sheet (preserva precio_manual e imagen_manual editadas a mano)
   ├─ Asigna CATEGORIA por filename del PDF (mapping de categorias.json)
   ├─ Asigna SUB_CATEGORIA por keyword en ARTICULO (primer-match-wins)
   └─ Genera output/sheet-data.json con todas las columnas
   ↓
run_pipeline.py
   ├─ Sube sheet-data.json a Supabase Storage → protrade-productos/data/catalogo.json
   └─ Dispara webhook n8n (workflow ouQkj8LZABzIakkH) → escribe al Google Sheet
   ↓
Web (protrade-web-mayorista) lee catalogo.json cada 5 min
```

---

## Stack

- **Python 3.12** (Docker base: `python:3.12-slim`)
- **pdfplumber 0.11.4** — parseo de tablas en PDFs
- **PyMuPDF 1.24.10** — extracción de imágenes
- **gdown 6.0.0** — descarga de carpetas de Drive
- **requests 2.32.3** — HTTP a Supabase y n8n
- **Docker + cron** (Dockerfile incluido) — corre en easypanel diario 8 AM

---

## Reglas de negocio CRÍTICAS

### 1. Precios (cómo se calculan)
- Si el Sheet tiene `precio_manual` con valor → se usa ese (override del user).
- Sino: `COSTO USD × dolar_blue_venta × 1.10` (margen mayorista 10%). El 10% es el margen mayorista actual (histórico: 30% hasta 2026-06-02, 20% hasta 2026-06-03, 10% desde 2026-06-03).
- El dólar blue se trae de [dolarapi.com](https://dolarapi.com) (campo `venta`).
- NUNCA hardcodear precios en el código. Toda la lógica debe pasar por estas 2 fórmulas.

### 2. Ediciones manuales del Sheet (precio + imagen)
- El user puede editar `precio_manual` y `imagen_manual` en el Google Sheet a mano.
- **El pipeline las LEE primero** (vía `fetch_sheet_overrides()` en `build_sheet_data.py`) y las preserva al generar el JSON nuevo.
- El workflow n8n que escribe al Sheet **NO emite esas 2 columnas** → `appendOrUpdate` no las toca → las ediciones del user nunca se pisan.
- Esto es crítico: si rompés esta cadena, perdés las ediciones manuales del cliente.

### 3. Taxonomía de categorías (2 niveles)
- **9 categorías madre**: Cosmética, Electrónica, Juguetería, Bazar, Ferretería, Indumentaria, Belleza, Hogar, Anteojos.
- **~101 subcategorías** definidas en `categorias.json` sección `subcategorias`.
- Mapeo:
  - **CATEGORIA madre** se asigna por filename del PDF (sección `mapping` de categorias.json, substring case-insensitive).
  - **SUB_CATEGORIA** se asigna por keyword en el campo ARTICULO del producto (sección `subcategorias`, primer-match-wins, keywords en UPPERCASE sin acentos).
- Cobertura actual: **94.2%** de productos clasificados en una sub específica. El resto cae en "Otros".
- Si necesitás agregar una sub nueva o refinar keywords, editá `categorias.json` → push → próximo cron 8 AM aplica.

### 4. SKUs con caracteres raros
- Algunos PDFs traen SKUs con caracteres chinos, símbolos asiáticos full-width, o errores de tipeo (CORRECOR, MICROFONNO, MU;ECO, FUBKO POP, etc).
- `parse_pdf.py` los limpia con `re.sub(r"[^\x00-\x7F]", "", sku_raw)` — saca todo lo no-ASCII.
- Los typos del proveedor se quedan en el ARTICULO tal cual (no corregimos datos del proveedor). Los keyword_patterns en `categorias.json` incluyen tanto la forma correcta como la errada para no perder matches.

### 5. Imágenes (3 fuentes posibles, en orden de prioridad)
1. **`imagen_manual` del Sheet** (override total del user, ej una URL externa).
2. **`imagen IA del user`** generada con Gemini/Higgsfield → vive en `C:\tmp\protrade-ia-full\{SKU}/portada.png` (PC del user, escaneada por `scan_user_skus`). Cuando se sube, queda en Supabase `manual/{SKU}/portada.png`.
3. **`imagen_proveedor`** extraída del PDF → siempre en `proveedor/{SKU}.png`.

`imagen_final` = `imagen_manual or imagen_proveedor`. La web muestra `imagen_final`.

**Importante en easypanel:** la carpeta `C:\tmp\protrade-ia-full` NO existe en el contenedor. Pero gracias a `fetch_sheet_overrides()`, las URLs IA que YA están en el Sheet se preservan via el override → easypanel no pierde las imágenes generadas en local.

### 6. Frecuencia
- **Pipeline pesado** (PDFs → JSON): diario 8 AM via cron (`/etc/cron.d/protrade` en Dockerfile).
- **Workflow n8n** que copia JSON → Sheet: debería correr **8:15 AM** (post-pipeline).
- NO conviene más frecuente: los PDFs no cambian más seguido y consume CPU/RAM en server al 86% RAM.

---

## Arquitectura: dónde corre todo

### En easypanel (server compartido)
- Servicio Docker que tira este repo desde GitHub
- Build: `Dockerfile` (instala deps + setea cron)
- Volumen montado: `/data` (persiste `state.json` para hash-based change detection)
- Cron line: `0 8 * * * root cd /app && python run_pipeline.py >> /data/pipeline.log 2>&1`
- **OJO**: el `root` user en la cron line es OBLIGATORIO en `/etc/cron.d/`. Si lo sacás, el cron NO ejecuta (es una restricción del formato de cron.d).

### En GitHub
- Repo público (sin secretos hardcodeados): https://github.com/luchogaviola/protrade-pipeline
- Push a `main` → easypanel redeploya solo si está configurado el webhook (sino, manual desde el panel)

### En Supabase (proyecto arjvysbtvznibqjexcek)
- **Bucket `protrade-productos`** (público):
  - `data/catalogo.json` → fuente de verdad del catálogo (lo lee la web)
  - `proveedor/{SKU}.png` → imágenes extraídas de los PDFs
  - `manual/{SKU}/portada.png` y `contexto.png` → imágenes IA del user

### En n8n Komercia
- **Workflow `ouQkj8LZABzIakkH`** ("Reporte Diario FACTURACIÓN - ProTrade"). NO confundir con este: es el del reporte de caja. El n8n que escribe el Sheet desde el JSON es OTRO workflow (también en n8n Komercia, ver memoria reciente).

### En Google
- **Sheet `1VkumWHXdcaYXolwMoK9qSr9VSoCr-u5TQGZ8B7LhloE`** (LISTA_MAYORISTA_PROTRADE)
- Hoja con datos reales: **gid `1028595591`** (la primera del documento puede ser una hoja "Sheet1" template — la que importa es 1028595591).
- **Drive folder** `1vRh7MP5ng5YymTJF-XTD6nva1mP5Uuqs` → donde el proveedor sube los PDFs.

---

## Archivos del repo

```
.
├── Dockerfile               # imagen base + cron + entrypoint
├── requirements.txt         # 3 deps mínimas (pdfplumber, PyMuPDF, gdown, requests)
├── .env.example             # plantilla; el real va por easypanel env vars
├── parse_pdf.py             # parsea tablas + extrae imágenes embebidas
├── upload_supabase.py       # sube imágenes de proveedor al bucket
├── categorias.json          # mapping PDF→cat madre + keywords→sub (101 subs)
├── build_sheet_data.py      # combina todo, lee Sheet overrides, asigna cat/sub, genera sheet-data.json
├── run_pipeline.py          # orquestador: descarga PDFs → cambios → parsea → sube imágenes → genera JSON → triggerea n8n
├── output/                  # output local (ignorado en .gitignore)
└── poc_preview.py           # helpers para preview HTML (no productivos)
```

---

## Variables de entorno (vars en easypanel)

| Var | Para qué | Crítica |
|---|---|---|
| `SUPABASE_SERVICE_KEY` | Subir al bucket (catalogo.json + imágenes). | SÍ |
| `SUPABASE_URL` | Default ok (`https://arjvysbtvznibqjexcek.supabase.co`). | NO |
| `SUPABASE_BUCKET` | Default `protrade-productos`. | NO |
| `PROVEEDOR_DRIVE_FOLDER` | ID de la carpeta Drive con los PDFs. | SÍ |
| `N8N_SHEET_WEBHOOK` | URL del webhook que dispara la copia JSON→Sheet. | NO (opcional) |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Notificar resumen post-corrida. | NO (opcional) |
| `PIPELINE_WORK_DIR` | Default `/data` (volumen persistente). | NO |

Ver `.env.example` para template.

---

## Cómo correr local (para debug)

```bash
# 1. Clonar
git clone https://github.com/luchogaviola/protrade-pipeline.git
cd protrade-pipeline

# 2. Setup (Python 3.12)
pip install -r requirements.txt

# 3. Env vars mínimas
cp .env.example .env
# Editar .env y poner SUPABASE_SERVICE_KEY del proyecto ProTrade

# 4. Correr el orquestador (descarga PDFs, parsea, sube)
python run_pipeline.py

# O solo regenerar JSON desde all-products.json ya parseado:
python build_sheet_data.py
```

Output local: `output/sheet-data.csv` y `output/sheet-data.json`.

---

## Qué NO tocar sin avisar

- **`categorias.json` sección `mapping`**: si cambiás un pattern PDF→categoria, los productos pueden recategorizarse y el web pierde URLs SEO.
- **Naming convention de las imágenes**: `proveedor/{safe_sku}.png` y `manual/{safe_sku}/{portada|contexto}.png`. Si cambia, el frontend rompe.
- **Estructura del JSON output (`sheet-data.json`)**: la web y el workflow n8n esperan esas columnas exactas (ver función `main()` de `build_sheet_data.py`). Si agregás columnas nuevas, también hay que actualizar `src/lib/sheet.ts` del repo web Y el code node "Filas → items" del workflow n8n.
- **Cron line del Dockerfile**: el `root` user es obligatorio en `/etc/cron.d/`. Si la modificás, validá que el cron sigue ejecutando (con `docker exec -it ... cat /var/log/cron.log`).

---

## Errores comunes / Gotchas conocidos

1. **`fetch()` no existe en Code nodes de n8n** — usar HTTP Request nodes (aplica al workflow que copia JSON→Sheet, no al pipeline Python).
2. **API PUT de n8n rompe encoding con acentos** — al programáticamente editar workflows n8n, usar `ensure_ascii=True` en Python.
3. **Supabase nodo nativo de n8n rompe acentos** — usar HTTP Request directo a Supabase REST.
4. **`/etc/cron.d/` cron line SIN user** = no ejecuta. Tiene que decir `0 8 * * * root cd /app && ...`.
5. **WhatsApp formato bold**: `*texto*` (un asterisco), NO `**texto**` (eso es Markdown estándar pero WA usa el de un solo asterisco).
6. **gviz CSV del Sheet** es público sin auth: `https://docs.google.com/spreadsheets/d/{ID}/export?format=csv&gid={GID}`. Útil para previews y para que el pipeline LEA del Sheet sin OAuth.
7. **`scan_user_skus()` solo funciona en la PC del user** (carpeta `C:\tmp\protrade-ia-full`). En easypanel devuelve {} → las imágenes IA se preservan vía override del Sheet, no via filesystem scan.
8. **state.json se persiste en `/data`** (volumen Docker en easypanel) y trackea hashes de PDFs para detectar cambios. Si lo borrás, el próximo run reprocesa TODOS los PDFs.

---

## Cómo desplegar — Flujo de trabajo en equipo

### Setup actual
- Repo: https://github.com/luchogaviola/protrade-pipeline (público)
- Hosting: easypanel (servicio Docker)
- CI/CD: NO hay GitHub Actions por ahora — el deploy es manual desde el panel de easypanel después de pushear.

### Flujo
1. Branch desde `main`: `git checkout -b feat/<descripcion>`.
2. Commits en español, imperativo.
3. Push + Pull Request.
4. Test local antes de mergear (correr `python build_sheet_data.py` y validar el output).
5. Merge a `main` → ir a easypanel → "Deploy" en el servicio para que tire el código nuevo.

---

## Patrones a respetar

### Print con encoding UTF-8 (Windows)
Cuando corras en Windows local, la consola es cp1252 y rompe con acentos/flechas. Para evitarlo:
```python
import sys
def out(s):
    sys.stdout.buffer.write((s + "\n").encode("utf-8", "replace"))
    sys.stdout.flush()
```

### Subir a Supabase Storage
```python
url = f"{SUPA_URL}/storage/v1/object/{BUCKET}/data/catalogo.json"
requests.post(
    url,
    headers={"Authorization": f"Bearer {SUPA_KEY}", "Content-Type": "application/json", "x-upsert": "true"},
    data=catalog.read_bytes(),
    timeout=60,
)
```

### Leer del Sheet sin OAuth (gviz CSV público)
```python
url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(req, timeout=30) as r:
    text = r.read().decode("utf-8", errors="replace")
```

---

## Recursos externos

- **Repo web (consumidor del JSON)**: https://github.com/luchogaviola/protrade-web-mayorista
- **Producción web**: https://protradegroup.shop
- **easypanel**: panel.pko.komerciamayorista.com → proyecto `protrade` → servicio `protrade-catalogo`
- **Supabase Storage (público)**: `arjvysbtvznibqjexcek.supabase.co/storage/v1/object/public/protrade-productos/data/catalogo.json`
- **Sheet (LISTA_MAYORISTA_PROTRADE)**: `1VkumWHXdcaYXolwMoK9qSr9VSoCr-u5TQGZ8B7LhloE`, gid `1028595591`
- **n8n Komercia**: `komercia-n8n.lzzo0i.easypanel.host`
- **WhatsApp checkout (consumidor del catálogo)**: +54 9 11 6267-0551
