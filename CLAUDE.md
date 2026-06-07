# Pro Trade Pipeline — Manual del proyecto

> Lo carga Claude Code al abrir el repo. Es la "biblia": qué hace, reglas de negocio, qué NO tocar, gotchas. Si algo cambia, actualizá este archivo en el MISMO PR.

---

## Qué hace

**Sincroniza el catálogo de Pro Trade Group desde la API del proveedor (Lambo Tech / HodERP) cada hora.** Mantiene actualizado `https://protradegroup.shop`.

> **CAMBIO 2026-06-07:** se reemplazó el viejo flujo Drive+PDF (gdown + pdfplumber + PyMuPDF + parse_pdf + build_sheet_data) por una **sync directa de la API JSON del proveedor**. Sin PDFs, sin extraer imágenes, sin OOM. Un solo script: `sync_hodmall.py`.

Flujo:

```
API hoderp (hds-api.hoderp.com/mall/products, storeId 971, ~2134 productos)
   ↓ filtra: saleable=true + con stock (statStock>0, no outofStock)
   ↓ mapea cada producto al schema de catalogo.json
   ↓ precio final ARS = costo × dólar blue venta del día × MARGIN (+20%)
   ↓ sube catalogo.json a Supabase (con gate de sanidad)
Web (protrade-web-mayorista) lee catalogo.json
```

---

## Stack

- **Python 3.12** (Docker `python:3.12-slim`) · **requests** (única dep) · **Docker + cron** (easypanel, cada hora).

---

## Reglas de negocio CRÍTICAS

### 1. Precio
- `precio_manual` (en el row) = **precio final ARS ya calculado** = `price1 × dólar_blue_venta × MARGIN`.
- `MARGIN` = **1.20 (+20%)**, env var. (Histórico del margen viejo: 30→20→10%; ahora 20% sobre el costo del proveedor.)
- Dólar = blue **venta** del día, de [dolarapi.com](https://dolarapi.com) (campo `venta`), traído en cada corrida (cada hora).
- **Productos en PESOS**: el proveedor carga algunos con el precio en ARS (los marca "PRECIO FINAL / EFECTIVO / pesos" en el título). Esos NO se multiplican por el dólar: `precio = price1 × MARGIN`. Detección: regex `ARS_RE` en `sync_hodmall.py`.
- NUNCA hardcodear precios.

### 2. Productos / stock
- Solo se traen los **saleable=true** y **con stock** (`statStock>0`, `outofStock=false`). El catálogo refleja lo que realmente hay.

### 3. Categorías
- **Se usan las del proveedor** (`category1` = madre, `category2` = sub). Son 17 (Electrónica, Belleza, Juguetería, Hogar y Cocina, Cuidado Personal, Iluminación, Ferretería, Librería, etc.).
- **Overlay "Mundial"**: productos de selección/AFA/mundial/bandera argentina se reclasifican a categoría `Mundial` (regex `MUNDIAL_RE`). Es jugada comercial WorldCup 2026.
- La web arma el menú dinámico por cantidad + fija "Mundial" primero (ver `src/app/page.tsx` del repo web).

### 4. Imágenes (override Nano Banana)
- Base = **URL del proveedor** (`images[0].url`, ya hosteada en `hds-rcdn.hoderp.com`). No se extrae ni copia nada.
- **Override**: si existe `manual/{SKU}/portada.png` en el bucket Supabase, esa pisa la del proveedor. Es la capa para las imágenes que el equipo edita con **Nano Banana**. El pipeline lista el prefijo `manual/` una vez por corrida (`list_manual_overrides()`) y matchea por SKU (`productNumber`).
- `imagen_final` = override `or` proveedor. La web muestra `IMAGEN`.

### 5. SKU
- `COD. ART.` = `productNumber` del proveedor (formato tipo `C-25121/H118`). Es la clave para el override de imágenes.

### 6. Frecuencia
- **Cada hora** (cron `0 * * * *` en el Dockerfile). La API es liviana; trae stock y dólar frescos.

---

## Dónde corre todo

### easypanel (panel.pko.komerciamayorista.com → proyecto `protrade` → servicio `protrade-catalogo`)
- Servicio Docker que tira este repo de GitHub. Build: `Dockerfile`. Auto-deploya en push a `main`.
- Cron: `0 * * * * root cd /app && python sync_hodmall.py >> /data/pipeline.log 2>&1`. **El `root` es OBLIGATORIO en `/etc/cron.d/`** (sino no ejecuta).
- El entrypoint corre `sync_hodmall.py` una vez al arrancar + `cron -f`. El log del run inicial va a `/data/pipeline.log` (verlo con la consola del container, no la de easypanel).

### Supabase (`arjvysbtvznibqjexcek`)
- Bucket `protrade-productos` (público):
  - `data/catalogo.json` → fuente de verdad del catálogo (lo lee la web).
  - `manual/{SKU}/portada.png` → imágenes override (Nano Banana / IA del equipo).

### GitHub
- Repo público (sin secretos): https://github.com/luchogaviola/protrade-pipeline

---

## Variables de entorno (en easypanel)

| Var | Para qué | Crítica |
|---|---|---|
| `SUPABASE_SERVICE_KEY` | Subir catalogo.json + listar overrides. | SÍ |
| `SUPABASE_URL` / `SUPABASE_BUCKET` | Defaults ok. | NO |
| `HOD_STORE_ID` | Default `971` (Lambo Tech AR). | NO |
| `HOD_CURRENCY_ID` | Default `1` (precio base USD). | NO |
| `MARGIN` | Default `1.20` (+20%). | NO |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Aviso post-corrida / alertas del gate. | NO (opcional) |

Sin `SUPABASE_SERVICE_KEY` (local) el script escribe `catalogo.json` a disco y no sube (modo test).

---

## Correr local (debug)

```bash
pip install -r requirements.txt
OUT_FILE=catalogo.json python sync_hodmall.py   # escribe a disco, no sube
```

---

## Qué NO tocar sin avisar

- **Schema del `catalogo.json`** (función `build_catalog`): la web (`src/lib/sheet.ts`) espera esas columnas exactas (`IMAGEN`, `COD. ART.`, `CATEGORIA`, `SUB_CATEGORIA`, `ARTICULO`, `COSTO USD`, `UN. x BULTO`, `precio_manual`, `activo`, `dolar_blue_venta`). Si cambiás algo, actualizá también el repo web.
- **Naming del override de imágenes**: `manual/{safe_sku}/portada.png`. Si cambia, se pierde la capa Nano Banana.
- **Gate de sanidad** (`MIN_PRODUCTOS`): evita pisar el catálogo bueno con uno roto (ej. si la API devuelve poco). No lo saques.
- **Cron line con `root`** en `/etc/cron.d/`.

---

## Gotchas

1. **Precios en pesos del proveedor**: algunos productos vienen en ARS marcados en el título. Si la detección `ARS_RE` falla, quedan multiplicados por el dólar (precios absurdos). Revisar el título de productos con precio alto raro.
2. **La API es pública** (no necesita login). El portal tiene login (Protrade71) pero NO cambia los precios; solo da un token. No hace falta para la sync.
3. **WhatsApp bold**: `*texto*` (un asterisco) en los avisos Telegram.
4. **`/data/pipeline.log`**: el output del run va a un archivo, no a la consola de easypanel. Para verlo: consola del container → `tail -50 /data/pipeline.log`.
5. **Imágenes del proveedor**: si un producto no tiene imagen en la API (~6 de 2134), `IMAGEN` queda vacío hasta que se suba un override Nano Banana.

---

## Deploy (equipo)

1. Branch desde `main` → commits en español imperativo → PR.
2. Test local (`python sync_hodmall.py` con OUT_FILE, validar el JSON).
3. Merge a `main` → easypanel auto-deploya (o "Deploy" manual en el panel).

---

## Recursos

- **Repo web**: https://github.com/luchogaviola/protrade-web-mayorista · **Prod**: https://protradegroup.shop
- **API proveedor**: `https://hds-api.hoderp.com/mall/products?storeId=971&currencyId=1&...` · portal `https://lambotech.hodmall.com/`
- **Supabase catálogo**: `arjvysbtvznibqjexcek.supabase.co/storage/v1/object/public/protrade-productos/data/catalogo.json`
- **easypanel**: panel.pko.komerciamayorista.com → `protrade` / `protrade-catalogo`
