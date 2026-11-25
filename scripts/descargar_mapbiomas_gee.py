#!/usr/bin/env python3
"""
Descarga automatizada por tiles y unión en mosaico (GeoTIFF/DEFLATE 30 m)
de MapBiomas Chile Colección 2.0 (LULC) y Colección 1.0 (Fuego) desde GEE.

Uso docente / preparación de insumos para ByC 2026 (FCF - Universidad de Concepción).
Genera 1 paquete comprimido liviano (.zip) por especie listo para distribuir en
GitHub Releases o Google Drive, permitiendo que los estudiantes trabajen en
BYC_Dist_AOO_EOO.ipynb a 30 m de resolución sin requerir cuenta ni código GEE.

Ejemplo de uso:
    python scripts/descargar_mapbiomas_gee.py --project byc-2026 --especies Ruil Queule
    python scripts/descargar_mapbiomas_gee.py --project byc-2026 --solo-inspeccionar
"""

import argparse
import concurrent.futures
import json
import math
import os
import re
import shutil
import sys
import time
import zipfile
from datetime import datetime, timezone

# Evitar conflicto con variables PROJ_LIB de instalaciones antiguas (ej. PostGIS en Windows)
try:
    import pyproj
    _pdir = pyproj.datadir.get_data_dir()
    if _pdir and os.path.isdir(_pdir):
        os.environ["PROJ_DATA"] = _pdir
        os.environ["PROJ_LIB"] = _pdir
except Exception:
    pass

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.merge import merge as rio_merge
import requests
from shapely.geometry import box
from shapely.ops import unary_union


# ==============================================================================
# CONFIGURACION OFICIAL DE ASSETS MAPBIOMAS CHILE Y ENVOLVENTES DE ESPECIES
# ==============================================================================
ASSETS_LULC_CANDIDATOS = [
    "projects/mapbiomas-chile/assets/LULC/COLLECTION-02/CLASSIFICATIONS/classification-final/clasificacion-final-2",
    "projects/mapbiomas-public/assets/chile/lulc/collection2/mapbiomas_chile_collection2_integration_v1",
    "projects/mapbiomas-public/assets/chile/collection2/mapbiomas_chile_collection2_integration_v1",
]

ASSETS_FUEGO_OFICIALES = {
    "fuego_freq": {
        "asset": "projects/mapbiomas-public/assets/chile/fire/collection1/mapbiomas_chile_fire_collection1_frequency_burned_v1",
        "dtype": "uint8",
        "descripcion": "Frecuencia de incendios acumulada 2013-2025 (veces quemada)",
    },
    "fuego_ultimo": {
        "asset": "projects/mapbiomas-public/assets/chile/fire/collection1/mapbiomas_chile_fire_collection1_year_last_fire_v1",
        "dtype": "uint16",
        "descripcion": "Año de la última ocurrencia de fuego (2013-2025)",
    },
    "fuego_cob": {
        "asset": "projects/mapbiomas-public/assets/chile/fire/collection1/mapbiomas_chile_fire_collection1_accumulated_burned_coverage_v1",
        "dtype": "uint8",
        "descripcion": "Cobertura y uso del suelo quemada acumulada (2013-2025)",
    },
}

# Envolventes biogeográficas nativas sincronizadas con BYC_Dist_AOO_EOO.ipynb
ENVOLVENTES_ESPECIES = {
    "Alerce": [
        {"lat_min": -43.20, "lat_max": -39.80, "lon_min": -74.35, "lon_max": -73.15},
        {"lat_min": -41.75, "lat_max": -41.00, "lon_min": -73.15, "lon_max": -72.75},
        {"lat_min": -43.60, "lat_max": -40.65, "lon_min": -72.75, "lon_max": -71.15},
    ],
    "Araucaria": [
        {"lat_min": -38.65, "lat_max": -37.50, "lon_min": -73.45, "lon_max": -72.85},
        {"lat_min": -40.35, "lat_max": -37.25, "lon_min": -72.15, "lon_max": -70.50},
    ],
    "Lleuque": [
        {"lat_min": -38.15, "lat_max": -37.55, "lon_min": -73.25, "lon_max": -72.75},
        {"lat_min": -39.30, "lat_max": -35.40, "lon_min": -72.00, "lon_max": -70.75},
    ],
    "Queule": [
        {"lat_min": -37.85, "lat_max": -35.70, "lon_min": -73.35, "lon_max": -72.60},
    ],
    "Ruil": [
        {"lat_min": -36.05, "lat_max": -35.00, "lon_min": -72.65, "lon_max": -72.00},
    ],
}

ANIOS_LULC_POR_DEFECTO = [1999, 2005, 2010, 2015, 2020, 2024]
URL_GADM_CHL_0 = "https://geodata.ucdavis.edu/gadm/gadm4.1/json/gadm41_CHL_0.json.zip"
URL_NE_10M = "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_countries.zip"


def _http_session():
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=Retry(total=3, backoff_factor=0.8, status_forcelist=[429, 500, 502, 503, 504])))
    return s


# ==============================================================================
# AUTENTICACION Y RESOLUCION DE ASSETS GEE
# ==============================================================================
def inicializar_gee(project_id: str, auth_mode: str = "auto"):
    """Inicializa la API de Google Earth Engine autenticando solo si faltan credenciales."""
    import ee

    try:
        ee.Initialize(project=project_id)
        print(f"GEE inicializado correctamente con proyecto '{project_id}'.")
        return ee
    except Exception:
        pass

    en_colab = "google.colab" in sys.modules
    modo = ("colab" if en_colab else "localhost") if auth_mode == "auto" else auth_mode
    print(f"No se encontraron credenciales locales de Earth Engine. Iniciando ee.Authenticate(auth_mode='{modo}')...")
    ee.Authenticate(auth_mode=modo)
    ee.Initialize(project=project_id)
    print(f"GEE autenticado e inicializado correctamente con proyecto '{project_id}'.")
    return ee


def cargar_asset_como_imagen(ee, asset_id: str):
    """Carga un asset GEE como ee.Image, soportando tanto Image como ImageCollection."""
    info = ee.data.getAsset(asset_id)
    atype = info.get("type", "").upper()
    if atype == "IMAGE":
        img = ee.Image(asset_id)
    elif atype == "IMAGE_COLLECTION":
        col = ee.ImageCollection(asset_id)
        # Intentar mosaico si todas las imágenes comparten bandas, o toBands si cada imagen es un año
        primera = col.first()
        n_bandas_primera = len(primera.bandNames().getInfo())
        n_imgs = col.size().getInfo()
        if n_bandas_primera == 1 and n_imgs > 1:
            # Revisar si los nombres de archivo/system:index contienen años distintos
            ids = col.aggregate_array("system:index").getInfo()
            anios_ids = [re.search(r"(19[89]\d|20[0-2]\d)", str(x)) for x in ids]
            if len({m.group(1) for m in anios_ids if m}) >= 2:
                img = col.toBands()
            else:
                img = col.mosaic()
        else:
            img = col.mosaic()
    else:
        raise ValueError(f"Tipo de asset GEE no soportado: {atype} ({asset_id})")
    bandas = img.bandNames().getInfo()
    return img, bandas, atype


def resolver_lulc_col2(ee, asset_custom: str = ""):
    """Localiza el asset accesible de MapBiomas Chile Colección 2.0 y sus nombres de bandas."""
    candidatos = ([asset_custom] if asset_custom else []) + ASSETS_LULC_CANDIDATOS
    errores = []
    for cand in candidatos:
        try:
            img, bandas, atype = cargar_asset_como_imagen(ee, cand)
            print(f"LULC MapBiomas Chile Col. 2.0 encontrado [{atype}]: {cand}")
            print(f"  Bandas disponibles ({len(bandas)}): {bandas[:4]} ... {bandas[-3:] if len(bandas) > 4 else ''}")
            return cand, img, bandas
        except Exception as exc:
            errores.append(f"  - {cand}: {exc}")
    raise RuntimeError("No se pudo acceder a ningún asset LULC de MapBiomas Chile:\n" + "\n".join(errores))


def seleccionar_banda_anio(bandas: list[str], anio: int) -> str:
    """Encuentra la banda correspondiente a un año específico (ej. classification_1999)."""
    s_anio = str(anio)
    for pat in (rf"^classification_{s_anio}$", rf".*_{s_anio}$", rf".*{s_anio}.*"):
        for b in bandas:
            if re.match(pat, b, flags=re.IGNORECASE):
                return b
    raise KeyError(f"No se encontró ninguna banda para el año {anio} en: {bandas}")


def seleccionar_banda_fuego(bandas: list[str], clave: str) -> str:
    """Selecciona la banda acumulada más reciente (ej. 2013_2025 o 2025) para MapBiomas Fuego Col. 1."""
    if len(bandas) == 1:
        return bandas[0]
    # Priorizar bandas acumuladas que terminen en 2025 / 2024 o que contengan el rango más amplio
    for pat in (r".*2013.*2025.*", r".*2025.*", r".*2024.*", r".*freq.*", r".*last.*", r".*accum.*"):
        hits = [b for b in bandas if re.match(pat, b, flags=re.IGNORECASE)]
        if hits:
            return sorted(hits)[-1]
    return sorted(bandas)[-1]


# ==============================================================================
# HUELLA GEOGRAFICA Y GRILLA DE TILES ALINEADOS A 30 m
# ==============================================================================
def cargar_mascara_chile_terrestre(cache_dir: str):
    """Carga máscara terrestre de Chile (GADM v4.1 CHL_0 con respaldo Natural Earth 1:10m)."""
    os.makedirs(cache_dir, exist_ok=True)
    # Reutilizar caché existente si ya fue descargado por el notebook principal
    for cand in (
        os.path.join(cache_dir, "gadm41_CHL_0.json.zip"),
        os.path.join("cache_limites_geo", "gadm41_CHL_0.json.zip"),
        "gadm41_CHL_0.json.zip",
    ):
        if os.path.exists(cand) and os.path.getsize(cand) > 10000:
            return gpd.read_file(f"zip://{cand}").to_crs("EPSG:4326")

    sess = _http_session()
    local_gadm = os.path.join(cache_dir, "gadm41_CHL_0.json.zip")
    try:
        print("Descargando máscara terrestre de Chile (GADM v4.1 CHL_0)...")
        r = sess.get(URL_GADM_CHL_0, timeout=25)
        r.raise_for_status()
        with open(local_gadm, "wb") as fh:
            fh.write(r.content)
        return gpd.read_file(f"zip://{local_gadm}").to_crs("EPSG:4326")
    except Exception as exc:
        print(f"Aviso GADM no disponible ({type(exc).__name__}); usando respaldo Natural Earth 1:10m...")

    local_ne = os.path.join(cache_dir, "ne_10m_admin_0_countries.zip")
    try:
        if not os.path.exists(local_ne):
            r = sess.get(URL_NE_10M, timeout=30)
            r.raise_for_status()
            with open(local_ne, "wb") as fh:
                fh.write(r.content)
        world = gpd.read_file(f"zip://{local_ne}").to_crs("EPSG:4326")
        col_iso = "ISO_A2" if "ISO_A2" in world.columns else "ADM0_A3"
        chile = world[world[col_iso].isin(["CL", "CHL"])]
        if not chile.empty:
            return chile
    except Exception as exc2:
        print(f"Aviso Natural Earth no disponible ({type(exc2).__name__}); usando envolventes sin recorte marino.")
    return None


def obtener_huella_especie(especie: str, ruta_csv: str, gdf_chile, margen_deg: float = 0.12):
    """Construye la huella terrestre chilena de la especie (envolventes + localidades + margen)."""
    pols = [
        box(e["lon_min"] - margen_deg, e["lat_min"] - margen_deg, e["lon_max"] + margen_deg, e["lat_max"] + margen_deg)
        for e in ENVOLVENTES_ESPECIES[especie]
    ]
    if ruta_csv and os.path.isfile(ruta_csv):
        with open(ruta_csv, "r", encoding="utf-8-sig", errors="ignore") as fh:
            first = fh.readline()
        sep = ";" if first.count(";") >= first.count(",") else ","
        df = pd.read_csv(ruta_csv, sep=sep, encoding="utf-8-sig")
        df.columns = [c.strip().lstrip("\ufeff") for c in df.columns]
        sub = df[df["Especie"].astype(str).str.strip() == especie]
        for _, row in sub.iterrows():
            lat, lon = float(row["Lat"]), float(row["Lon"])
            pols.append(box(lon - margen_deg, lat - margen_deg, lon + margen_deg, lat + margen_deg))

    huella_bruta = unary_union(pols)
    if gdf_chile is None or gdf_chile.empty:
        return huella_bruta
    chile_buf = gdf_chile.geometry.union_all().buffer(0.03)
    huella_chile = huella_bruta.intersection(chile_buf)
    if huella_chile.is_empty:
        huella_chile = huella_bruta
    return huella_chile


def construir_grilla_tiles(huella_geom, escala_m: float = 30.0, tile_px: int = 1800):
    """
    Genera subtiles perfectamente alineados a una grilla fija de píxeles en EPSG:4326.
    Con tile_px=1800 (~0.485° x 0.485° a 30 m):
      - uint8  -> ~3.2 MB/tile (muy por debajo del límite de 48 MB de ee.data.computePixels)
      - uint16 -> ~6.5 MB/tile
    """
    deg_px = float(escala_m) / 111320.0
    minx, miny, maxx, maxy = huella_geom.bounds

    # Anclar origen de la grilla a múltiplos exactos de deg_px (cero desfase entre capas/años)
    ix0 = math.floor(minx / deg_px)
    ix1 = math.ceil(maxx / deg_px)
    iy0 = math.floor(miny / deg_px)
    iy1 = math.ceil(maxy / deg_px)

    tiles = []
    r_idx = 0
    for y_start in range(iy0, iy1, tile_px):
        y_end = min(y_start + tile_px, iy1)
        h_px = y_end - y_start
        lat_min = y_start * deg_px
        lat_max = y_end * deg_px
        c_idx = 0
        for x_start in range(ix0, ix1, tile_px):
            x_end = min(x_start + tile_px, ix1)
            w_px = x_end - x_start
            lon_min = x_start * deg_px
            lon_max = x_end * deg_px
            tile_box = box(lon_min, lat_min, lon_max, lat_max)
            if tile_box.intersects(huella_geom):
                tiles.append({
                    "row": r_idx,
                    "col": c_idx,
                    "width": w_px,
                    "height": h_px,
                    "deg_px": deg_px,
                    "lon_min": lon_min,
                    "lat_max": lat_max,
                    "bounds": (lon_min, lat_min, lon_max, lat_max),
                })
            c_idx += 1
        r_idx += 1
    return tiles


# ==============================================================================
# DESCARGA PARALELA (ee.data.computePixels) + UNION EN MOSAICO (rasterio.merge)
# ==============================================================================
def descargar_tile_compute_pixels(ee, img_banda, tile: dict, ruta_salida: str, reintentos: int = 4):
    """Descarga un tile individual como GeoTIFF desde GEE con reintentos exponenciales."""
    if os.path.exists(ruta_salida) and os.path.getsize(ruta_salida) > 200:
        return ruta_salida

    peticion = {
        "expression": img_banda,
        "fileFormat": "GEO_TIFF",
        "grid": {
            "dimensions": {"width": tile["width"], "height": tile["height"]},
            "affineTransform": {
                "scaleX": tile["deg_px"],
                "shearX": 0.0,
                "translateX": tile["lon_min"],
                "shearY": 0.0,
                "scaleY": -tile["deg_px"],
                "translateY": tile["lat_max"],
            },
            "crsCode": "EPSG:4326",
        },
    }

    ultimo_err = None
    for intento in range(1, reintentos + 1):
        try:
            raw_bytes = ee.data.computePixels(peticion)
            tmp_path = ruta_salida + ".part"
            with open(tmp_path, "wb") as fh:
                fh.write(raw_bytes)
            os.replace(tmp_path, ruta_salida)
            return ruta_salida
        except Exception as exc:
            ultimo_err = exc
            espera = 2.0 ** intento
            print(f"    [Reintento {intento}/{reintentos}] tile r{tile['row']}_c{tile['col']}: {exc} (esperando {espera:.0f}s)")
            time.sleep(espera)
    raise RuntimeError(f"Falló descarga del tile r{tile['row']}_c{tile['col']} tras {reintentos} intentos: {ultimo_err}")


def unir_tiles_geotiff(rutas_tiles: list[str], ruta_mosaico: str, dtype: str = "uint8", nodata: int = 0):
    """Une múltiples tiles GeoTIFF en un único mosaico comprimido (DEFLATE + TILED)."""
    srcs = [rasterio.open(p) for p in rutas_tiles]
    try:
        mosaic, out_transform = rio_merge(srcs, nodata=nodata)
        arr = mosaic[0].astype(dtype)
        profile = srcs[0].profile.copy()
        profile.update({
            "driver": "GTiff",
            "height": arr.shape[0],
            "width": arr.shape[1],
            "count": 1,
            "dtype": dtype,
            "transform": out_transform,
            "crs": "EPSG:4326",
            "nodata": nodata,
            "compress": "deflate",
            "predictor": 2,
            "tiled": True,
            "blockxsize": 256,
            "blockysize": 256,
        })
        os.makedirs(os.path.dirname(ruta_mosaico), exist_ok=True)
        with rasterio.open(ruta_mosaico, "w", **profile) as dst:
            dst.write(arr, 1)
        px_validos = int((arr > 0).sum())
        tam_mb = os.path.getsize(ruta_mosaico) / 1e6
        return {
            "archivo": os.path.basename(ruta_mosaico),
            "ancho_px": int(arr.shape[1]),
            "alto_px": int(arr.shape[0]),
            "px_con_dato": px_validos,
            "tamano_mb": round(tam_mb, 3),
        }
    finally:
        for s in srcs:
            s.close()


def descargar_y_unir_capa(ee, img_banda, tiles: list[dict], dir_tiles: str, ruta_mosaico: str,
                          dtype: str = "uint8", max_workers: int = 4, mantener_tiles: bool = False):
    """Orquesta la descarga paralela de subtiles y su unión en un único GeoTIFF comprimido."""
    os.makedirs(dir_tiles, exist_ok=True)
    img_cast = img_banda.unmask(0).toUint16() if dtype == "uint16" else img_banda.unmask(0).toUint8()

    tareas = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        for t in tiles:
            p_tile = os.path.join(dir_tiles, f"tile_r{t['row']:02d}_c{t['col']:02d}.tif")
            tareas.append(pool.submit(descargar_tile_compute_pixels, ee, img_cast, t, p_tile))
        rutas_ok = [fut.result() for fut in concurrent.futures.as_completed(tareas)]

    rutas_ok = sorted(rutas_ok)
    stats = unir_tiles_geotiff(rutas_ok, ruta_mosaico, dtype=dtype, nodata=0)
    if not mantener_tiles:
        shutil.rmtree(dir_tiles, ignore_errors=True)
    return stats


# ==============================================================================
# PIPELINE PRINCIPAL (OPCION A: PAQUETES LIVIANOS POR ESPECIE)
# ==============================================================================
def ejecutar_descarga(args):
    ee = inicializar_gee(args.project, auth_mode=args.auth_mode)

    # 1. Verificar assets oficiales de MapBiomas Chile (LULC Col. 2.0 + Fuego Col. 1)
    asset_lulc_id, img_lulc, bandas_lulc = resolver_lulc_col2(ee, args.asset_lulc)

    fuego_cargado = {}
    if not args.sin_fuego:
        for clave, cfg in ASSETS_FUEGO_OFICIALES.items():
            try:
                img_f, bandas_f, atype_f = cargar_asset_como_imagen(ee, cfg["asset"])
                banda_sel = seleccionar_banda_fuego(bandas_f, clave)
                fuego_cargado[clave] = {
                    "asset": cfg["asset"],
                    "image": img_f.select([banda_sel]),
                    "banda": banda_sel,
                    "dtype": cfg["dtype"],
                    "descripcion": cfg["descripcion"],
                }
                print(f"Fuego Col. 1 [{clave}] listo ({atype_f}): banda '{banda_sel}'")
            except Exception as exc:
                print(f"Aviso: no se pudo cargar {clave} ({cfg['asset']}): {exc}")

    # 2. Preparar máscara terrestre chilena y especies solicitadas
    cache_dir = os.path.join(args.salida, "_cache_geo")
    gdf_chile = cargar_mascara_chile_terrestre(cache_dir)
    especies_obj = list(ENVOLVENTES_ESPECIES.keys()) if "TODAS" in args.especies else args.especies

    print("\n--- Plan de descarga por especie (Opción A: 30 m en tiles alineados) ---")
    planes = {}
    for sp in especies_obj:
        huella = obtener_huella_especie(sp, args.csv_localidades, gdf_chile, margen_deg=args.margen_deg)
        tiles = construir_grilla_tiles(huella, escala_m=args.escala, tile_px=args.tile_px)
        planes[sp] = (huella, tiles)
        b = huella.bounds
        print(f"  {sp:<10s}: {len(tiles):>2d} tiles terrestres | bbox=[{b[0]:.2f}, {b[1]:.2f}, {b[2]:.2f}, {b[3]:.2f}]")

    if args.solo_inspeccionar:
        print("\nModo --solo-inspeccionar finalizado. No se descargaron archivos.")
        return

    # 3. Descargar, unir y empaquetar cada especie en un .zip listo para Módulo 12
    for sp in especies_obj:
        t0 = time.time()
        huella, tiles = planes[sp]
        sp_low = sp.lower()
        dir_sp = os.path.join(args.salida, sp_low)
        os.makedirs(dir_sp, exist_ok=True)
        print(f"\n================ PROCESANDO {sp.upper()} ({len(tiles)} tiles/capa) ================")

        archivos_zip = []
        meta = {
            "especie": sp,
            "proyecto_gee": args.project,
            "escala_m": args.escala,
            "crs": "EPSG:4326",
            "fecha_generacion_utc": datetime.now(timezone.utc).isoformat(),
            "atribucion": "MapBiomas Chile — Colección 2.0 Cobertura y Uso del Suelo (1999-2024) y Colección 1.0 Fuego (2013-2025). Licencia CC BY 4.0 (https://chile.mapbiomas.org/).",
            "asset_lulc": asset_lulc_id,
            "capas": [],
        }

        # 3a. Capas LULC Colección 2.0 (años solicitados)
        for anio in args.anios:
            banda = seleccionar_banda_anio(bandas_lulc, anio)
            out_tif = os.path.join(dir_sp, f"lulc_{sp_low}_{anio}.tif")
            dir_tmp = os.path.join(dir_sp, f"_tiles_lulc_{anio}")
            print(f"  -> Descargando LULC {anio} (banda '{banda}')...")
            st = descargar_y_unir_capa(
                ee, img_lulc.select([banda]), tiles, dir_tmp, out_tif,
                dtype="uint8", max_workers=args.workers, mantener_tiles=args.mantener_tiles
            )
            st["tipo"] = "LULC_COL2"
            st["anio"] = anio
            st["banda_gee"] = banda
            meta["capas"].append(st)
            archivos_zip.append(out_tif)
            print(f"     OK: {st['archivo']} ({st['ancho_px']}x{st['alto_px']} px, {st['tamano_mb']:.2f} MB)")

        # 3b. Capas MapBiomas Fuego Colección 1 (2013-2025)
        for clave, fcfg in fuego_cargado.items():
            out_tif = os.path.join(dir_sp, f"{clave}_{sp_low}_2013_2025.tif")
            dir_tmp = os.path.join(dir_sp, f"_tiles_{clave}")
            print(f"  -> Descargando Fuego Col. 1 [{clave}] (banda '{fcfg['banda']}')...")
            st = descargar_y_unir_capa(
                ee, fcfg["image"], tiles, dir_tmp, out_tif,
                dtype=fcfg["dtype"], max_workers=args.workers, mantener_tiles=args.mantener_tiles
            )
            st["tipo"] = "FUEGO_COL1"
            st["clave"] = clave
            st["asset_gee"] = fcfg["asset"]
            st["banda_gee"] = fcfg["banda"]
            meta["capas"].append(st)
            archivos_zip.append(out_tif)
            print(f"     OK: {st['archivo']} ({st['ancho_px']}x{st['alto_px']} px, {st['tamano_mb']:.2f} MB)")

        # 3c. Metadatos y empaquetado final en .zip
        meta_path = os.path.join(dir_sp, f"metadata_mapbiomas_{sp_low}.json")
        with open(meta_path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
        archivos_zip.append(meta_path)

        zip_path = os.path.join(args.salida, f"mapbiomas_{sp_low}_30m.zip")
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for fp in archivos_zip:
                zf.write(fp, arcname=os.path.basename(fp))

        zip_mb = os.path.getsize(zip_path) / 1e6
        print(f"  PAQUETE LISTO: {zip_path} ({zip_mb:.2f} MB) en {time.time() - t0:.1f}s")

    print("\nDescarga y unión de tiles finalizada exitosamente.")


def parse_args():
    p = argparse.ArgumentParser(description="Descarga y mosaico automático de MapBiomas Chile (Col. 2 LULC + Col. 1 Fuego) desde GEE.")
    p.add_argument("--project", default="byc-2026", help="Cloud Project ID activo en Google Earth Engine (default: byc-2026)")
    p.add_argument("--especies", nargs="+", default=["TODAS"], choices=["TODAS"] + list(ENVOLVENTES_ESPECIES.keys()),
                   help="Especies a descargar (default: TODAS)")
    p.add_argument("--anios", nargs="+", type=int, default=ANIOS_LULC_POR_DEFECTO,
                   help="Años de MapBiomas Col. 2.0 LULC (default: 1999 2005 2010 2015 2020 2024)")
    p.add_argument("--escala", type=float, default=30.0, help="Resolución espacial en metros (default: 30)")
    p.add_argument("--tile-px", type=int, default=1800, help="Tamaño de subtile en píxeles (default: 1800)")
    p.add_argument("--margen-deg", type=float, default=0.12, help="Margen extra en grados alrededor de envolventes/localidades (~13 km)")
    p.add_argument("--workers", type=int, default=4, help="Descargas paralelas de subtiles (default: 4)")
    p.add_argument("--csv-localidades", default="data/Coordenadas_codigo.csv", help="Ruta al CSV de localidades del curso")
    p.add_argument("--salida", default="rasters_mapbiomas", help="Carpeta de salida para los .zip y mosaicos")
    p.add_argument("--asset-lulc", default="", help="Asset GEE alternativo de MapBiomas Chile LULC Col. 2 (opcional)")
    p.add_argument("--auth-mode", default="auto", choices=["auto", "localhost", "notebook", "colab", "gcloud"],
                   help="Modo de autenticación GEE si no hay credenciales locales")
    p.add_argument("--sin-fuego", action="store_true", help="Omitir capas de MapBiomas Fuego Col. 1")
    p.add_argument("--mantener-tiles", action="store_true", help="Conservar subtiles temporales tras el mosaico")
    p.add_argument("--solo-inspeccionar", action="store_true", help="Solo inspeccionar assets GEE y plan de tiles sin descargar")
    return p.parse_args()


if __name__ == "__main__":
    ejecutar_descarga(parse_args())
