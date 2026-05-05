"""
converter.py — Geodetically correct UTM → MUTM conversion

Both vector and raster use IDENTICAL transformation logic:
  t1 = Transformer(src_crs → WGS84,    always_xy=True)
  t2 = Transformer(WGS84   → MUTM_CRS, always_xy=True)

For vector: applied coordinate-by-coordinate via numpy arrays.
For raster: the same t1+t2 transformers are used to build a pixel
            mapping grid (row/col → MUTM x/y), then scipy.ndimage
            maps source pixels to output — no rasterio.reproject(),
            no GDAL datum shift path, identical math to vector.
"""

import io, zipfile, tempfile, logging
from pathlib import Path

import numpy as np
import fiona
import fiona.crs
import rasterio
from rasterio.transform import from_bounds
from rasterio.crs import CRS as RioCRS
from pyproj import CRS, Transformer
from scipy.ndimage import map_coordinates

log = logging.getLogger(__name__)

# ── NEPAL SURVEY DEPT. PARAMETERS ────────────────────────────────────────────

EVEREST_A  = 6377276.345
EVEREST_RF = 300.8017

MUTM_ZONES = {
    81: (79.5, 82.5),
    84: (82.5, 85.5),
    87: (85.5, 88.5),
}

PARAM3         = dict(x=+293.17, y=+726.18, z=+245.36)
PARAM7_TOWGS84 = "-124.3813,521.6700,764.5137,17.1488,-8.11536,11.1842,-2.1105"

WGS84      = CRS.from_epsg(4326)
CHUNK_SIZE = 300


# ── CRS HELPERS ───────────────────────────────────────────────────────────────

def _mutm_proj4_str(zone: int, method: str) -> str:
    towgs84 = (
        f"{PARAM3['x']},{PARAM3['y']},{PARAM3['z']},0,0,0,0"
        if method == "3param" else PARAM7_TOWGS84
    )
    return (
        f"+proj=tmerc +lat_0=0 +lon_0={zone} +k=0.9999 "
        f"+x_0=500000 +y_0=0 "
        f"+a={EVEREST_A} +rf={EVEREST_RF} "
        f"+towgs84={towgs84} +units=m +no_defs"
    )


def _build_transformers(src_crs: CRS, zone: int, method: str):
    """
    Build the exact same two pyproj Transformers used by vector conversion.
      t1: src_crs → WGS84
      t2: WGS84   → MUTM (Everest 1830, with datum shift)
    """
    mutm_crs = CRS.from_proj4(_mutm_proj4_str(zone, method))
    t1 = Transformer.from_crs(src_crs, WGS84,    always_xy=True)
    t2 = Transformer.from_crs(WGS84,   mutm_crs, always_xy=True)
    return t1, t2


def _detect_zone_from_bbox(left, bottom, right, top, src_crs: CRS) -> int:
    t = Transformer.from_crs(src_crs, WGS84, always_xy=True)
    lons, _ = t.transform(
        [left, right, left, right],
        [bottom, bottom, top, top]
    )
    lon = float(np.mean(lons))
    for zone, (lo, hi) in MUTM_ZONES.items():
        if lo <= lon < hi:
            return zone
    raise ValueError(
        f"Centroid longitude {lon:.4f} is outside Nepal MUTM coverage "
        f"(79.5-88.5 E). Check your input CRS."
    )


def _extract_horizontal_crs(rio_crs: RioCRS) -> RioCRS:
    """
    If rio_crs is a compound CRS (3D — horizontal + vertical datum),
    extract and return only the horizontal component.
    Same horizontal-only CRS is what the vector pipeline uses.
    """
    wkt = rio_crs.to_wkt()
    if "COMPD_CS" not in wkt and "COMPOUND" not in wkt.upper():
        return rio_crs
    log.info("Compound CRS detected — extracting horizontal component")
    try:
        compound  = CRS.from_wkt(wkt)
        horiz_crs = compound.sub_crs_list[0]
        log.info(f"Horizontal CRS: {horiz_crs.name}")
        return RioCRS.from_wkt(horiz_crs.to_wkt())
    except Exception as e:
        log.warning(f"Could not split compound CRS ({e}), using as-is")
        return rio_crs


# ── FAST VECTORIZED GEOMETRY TRANSFORM (vector) ───────────────────────────────

def _transform_geometry(geom: dict, t1: Transformer, t2: Transformer) -> dict:
    gtype  = geom["type"]
    coords = geom["coordinates"]

    def tx(ring):
        if not ring:
            return ring
        arr    = np.array(ring)
        x1, y1 = t1.transform(arr[:, 0], arr[:, 1])
        x2, y2 = t2.transform(x1, y1)
        return list(zip(x2.tolist(), y2.tolist()))

    if gtype == "Point":
        x1, y1 = t1.transform(coords[0], coords[1])
        x2, y2 = t2.transform(x1, y1)
        new_coords = [float(x2), float(y2)]
    elif gtype == "MultiPoint":
        new_coords = tx(coords)
    elif gtype == "LineString":
        new_coords = tx(coords)
    elif gtype == "MultiLineString":
        new_coords = [tx(r) for r in coords]
    elif gtype == "Polygon":
        new_coords = [tx(r) for r in coords]
    elif gtype == "MultiPolygon":
        new_coords = [[tx(r) for r in poly] for poly in coords]
    else:
        return geom

    return {"type": gtype, "coordinates": new_coords}


# ── SHAPEFILE CONVERSION ──────────────────────────────────────────────────────

def convert_shapefile(zip_bytes: bytes, method: str = "7param") -> tuple[bytes, dict]:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            zf.extractall(tmp / "in")
        del zip_bytes

        shp_files = list((tmp / "in").rglob("*.shp"))
        if not shp_files:
            raise ValueError("No .shp file found inside the ZIP.")
        shp_path = shp_files[0]
        log.info(f"Reading shapefile: {shp_path.name}")

        with fiona.open(shp_path) as src:
            if not src.crs:
                raise ValueError("Shapefile has no CRS (.prj missing). Please add a .prj file.")

            src_crs      = CRS(src.crs)
            src_crs_name = src_crs.name
            total_feats  = len(src)
            geom_type    = src.schema["geometry"]
            left, bottom, right, top = src.bounds

            log.info(f"Source CRS: {src_crs_name}  |  Features: {total_feats}")

            zone = _detect_zone_from_bbox(left, bottom, right, top, src_crs)
            log.info(f"Detected MUTM zone: {zone}")

            t1, t2 = _build_transformers(src_crs, zone, method)

            out_name      = shp_path.stem + f"_MUTM{zone}"
            out_dir       = tmp / "out"
            out_dir.mkdir()
            out_shp       = out_dir / f"{out_name}.shp"
            dst_crs_fiona = fiona.crs.from_string(_mutm_proj4_str(zone, method))

            with fiona.open(
                out_shp, "w",
                driver="ESRI Shapefile",
                crs=dst_crs_fiona,
                schema=src.schema.copy(),
            ) as dst:
                chunk, processed = [], 0
                for feat in src:
                    geom = feat.geometry
                    if geom is None:
                        chunk.append(feat)
                    else:
                        chunk.append({
                            "type":       "Feature",
                            "geometry":   _transform_geometry(dict(geom), t1, t2),
                            "properties": dict(feat.properties),
                        })
                    if len(chunk) >= CHUNK_SIZE:
                        dst.writerecords(chunk)
                        processed += len(chunk)
                        log.info(f"  {processed}/{total_feats} features written…")
                        chunk = []
                if chunk:
                    dst.writerecords(chunk)
                    processed += len(chunk)

            log.info(f"Conversion complete: {processed} features → MUTM{zone}")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in out_dir.iterdir():
                zf.write(f, f.name)
        buf.seek(0)

        return buf.read(), {
            "zone": zone, "method": method, "src_crs": src_crs_name,
            "features": processed, "geometry_type": geom_type,
            "output_filename": f"{out_name}.zip",
        }


# ── RASTER CONVERSION ─────────────────────────────────────────────────────────

def convert_raster(
    file_bytes: bytes,
    src_filename: str,
    method: str = "7param",
) -> tuple[bytes, dict]:
    """
    Raster conversion using IDENTICAL pyproj Transformers as vector.

    rasterio.reproject() is NOT used because it routes through GDAL's
    internal PROJ pipeline, which may take a different datum shift path
    than the explicit t1+t2 Transformers used by the vector conversion.

    Instead:
      1. Build the same t1, t2 Transformers as vector
      2. Compute the output MUTM bounding box by transforming all 4 corners
      3. Create an output grid of MUTM (x, y) coordinates
      4. Back-transform each output pixel to source pixel coordinates
         using the inverse transformers (t2_inv, t1_inv)
      5. Resample source pixel values at those locations using
         scipy.ndimage.map_coordinates (bilinear = order=1)

    This is mathematically identical to the vector pipeline — same
    Transformers, same parameter application, same datum shift.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        in_path = tmp / src_filename
        in_path.write_bytes(file_bytes)
        del file_bytes

        with rasterio.open(in_path) as src:
            if src.crs is None:
                raise ValueError("Raster has no CRS metadata. Cannot reproject.")

            # Handle compound (3D) CRS — extract horizontal component only
            src_rio_crs  = _extract_horizontal_crs(src.crs)
            src_crs      = CRS.from_wkt(src_rio_crs.to_wkt())
            src_crs_name = src_rio_crs.to_string()
            log.info(f"Source raster CRS: {src_crs_name}")

            b    = src.bounds
            zone = _detect_zone_from_bbox(
                b.left, b.bottom, b.right, b.top, src_crs
            )
            log.info(f"Detected MUTM zone: {zone}")

            # ── Same transformers as vector ───────────────────────────────
            t1, t2 = _build_transformers(src_crs, zone, method)

            # Inverse transformers for back-projection
            mutm_crs = CRS.from_proj4(_mutm_proj4_str(zone, method))
            t2_inv   = Transformer.from_crs(mutm_crs, WGS84,    always_xy=True)
            t1_inv   = Transformer.from_crs(WGS84,    src_crs,  always_xy=True)

            # ── Compute output MUTM bounding box ──────────────────────────
            # Transform all 4 corners src → WGS84 → MUTM (same as vector)
            corners_src_x = [b.left,  b.right, b.left,  b.right]
            corners_src_y = [b.bottom, b.bottom, b.top,  b.top]
            cx1, cy1 = t1.transform(corners_src_x, corners_src_y)  # → WGS84
            cx2, cy2 = t2.transform(cx1, cy1)                       # → MUTM

            out_left   = float(np.min(cx2))
            out_right  = float(np.max(cx2))
            out_bottom = float(np.min(cy2))
            out_top    = float(np.max(cy2))

            # Output resolution: preserve approximate pixel size
            src_res_x = (b.right - b.left)   / src.width
            src_res_y = (b.top   - b.bottom) / src.height
            # Scale to MUTM extent
            scale_x   = (out_right - out_left)   / (b.right - b.left)
            scale_y   = (out_top   - out_bottom) / (b.top   - b.bottom)
            out_res_x = src_res_x * scale_x
            out_res_y = src_res_y * scale_y
            out_width  = max(1, int(round((out_right - out_left)   / out_res_x)))
            out_height = max(1, int(round((out_top   - out_bottom) / out_res_y)))

            # Strip size: process N rows at a time to stay within 512MB RAM.
            # 7355 x 3435 full grid = ~1.6 GB at float64. At 50 rows:
            # 7355 x 50 x 8 arrays x 8 bytes = ~23 MB per strip — safe.
            STRIP_ROWS = 50

            log.info(f"Output grid: {out_width} x {out_height} pixels "
                     f"(processing in strips of {STRIP_ROWS} rows)")

            out_transform = from_bounds(
                out_left, out_bottom, out_right, out_top,
                out_width, out_height
            )

            stem     = Path(src_filename).stem
            out_name = f"{stem}_MUTM{zone}.tif"
            out_path = tmp / out_name
            dtype    = src.dtypes[0]
            cval     = float(src.nodata) if src.nodata is not None else 0.0
            mutm_rio = RioCRS.from_proj4(_mutm_proj4_str(zone, method))

            out_meta = {
                "driver":    "GTiff",
                "dtype":     dtype,
                "width":     out_width,
                "height":    out_height,
                "count":     src.count,
                "crs":       mutm_rio,
                "transform": out_transform,
                "compress":  "lzw",
                "nodata":    src.nodata,
            }

            # Column centres in MUTM — computed once, reused every strip
            cols   = np.arange(out_width, dtype=np.float64)
            mutm_x_row = out_left + (cols + 0.5) * (out_right - out_left) / out_width

            # Process one band at a time, one strip at a time.
            # Never hold more than:
            #   - one source band in RAM (read fresh per band)
            #   - one strip of coordinate grids (~23 MB for 7355x50)
            #   - one strip of output data
            # Total peak RAM ~ 2 x source_band_size + 23MB — safe on 512MB.

            # Determine nodata value to write for out-of-extent pixels
            src_nodata = src.nodata
            if src_nodata is not None:
                nodata_write = float(src_nodata)
            elif np.issubdtype(np.dtype(dtype), np.floating):
                nodata_write = np.nan
            else:
                nodata_write = 0.0

            # Update output meta with correct nodata
            out_meta["nodata"] = src_nodata if src_nodata is not None else (
                np.nan if np.issubdtype(np.dtype(dtype), np.floating) else None
            )

            with rasterio.open(out_path, "w", **out_meta) as dst:

                for band_idx in range(1, src.count + 1):
                    log.info(f"  Processing band {band_idx}/{src.count}…")

                    for strip_start in range(0, out_height, STRIP_ROWS):
                        strip_end  = min(strip_start + STRIP_ROWS, out_height)
                        strip_h    = strip_end - strip_start

                        rows_strip   = np.arange(strip_start, strip_end, dtype=np.float64)
                        mutm_y_strip = out_top - (rows_strip + 0.5) * (out_top - out_bottom) / out_height

                        mutm_x_grid = np.broadcast_to(mutm_x_row, (strip_h, out_width)).copy()
                        mutm_y_grid = mutm_y_strip[:, np.newaxis] * np.ones((1, out_width))

                        # Back-project: MUTM → WGS84 → src_crs (identical to vector)
                        wgs_x, wgs_y = t2_inv.transform(mutm_x_grid.ravel(), mutm_y_grid.ravel())
                        src_x, src_y = t1_inv.transform(wgs_x, wgs_y)

                        s_col = ((src_x - b.left) / (b.right - b.left) * src.width  - 0.5).reshape(strip_h, out_width)
                        s_row = ((b.top  - src_y)  / (b.top - b.bottom) * src.height - 0.5).reshape(strip_h, out_width)

                        # Identify pixels outside source extent
                        outside_mask = (
                            (s_col < 0) | (s_col > src.width  - 1) |
                            (s_row < 0) | (s_row > src.height - 1)
                        )

                        # ── Windowed source read — only the rows we need ──────────
                        # Instead of reading the full band (193MB for your DEM),
                        # read only the source rows that back-project into this strip.
                        # Clamp to valid range, add 1-row padding for interpolation.
                        row_min = max(0, int(np.floor(s_row[~outside_mask].min())) - 1) if (~outside_mask).any() else 0
                        row_max = min(src.height - 1, int(np.ceil(s_row[~outside_mask].max())) + 1) if (~outside_mask).any() else 0
                        win_height = row_max - row_min + 1

                        src_window = rasterio.windows.Window(
                            col_off=0, row_off=row_min,
                            width=src.width, height=win_height
                        )
                        src_tile = src.read(band_idx, window=src_window).astype(np.float64)

                        # Replace nodata with NaN to prevent edge bleed
                        if src_nodata is not None:
                            src_tile[src_tile == float(src_nodata)] = np.nan

                        # Adjust s_row to be relative to the tile's row_min
                        s_row_local = s_row - row_min

                        # Interpolate within the tile
                        strip_data = map_coordinates(
                            src_tile, [s_row_local, s_col],
                            order=0, mode="constant",
                            cval=0.0, prefilter=False,
                        )
                        del src_tile  # release immediately

                        # Apply nodata to outside and NaN pixels
                        invalid = outside_mask | np.isnan(strip_data)
                        strip_data[invalid] = nodata_write

                        if np.issubdtype(np.dtype(dtype), np.integer):
                            strip_data = np.round(strip_data)

                        # Write strip directly to disk
                        window = rasterio.windows.Window(
                            col_off=0, row_off=strip_start,
                            width=out_width, height=strip_h
                        )
                        dst.write(
                            strip_data.astype(dtype).reshape(strip_h, out_width),
                            band_idx,
                            window=window
                        )

                        del mutm_x_grid, mutm_y_grid, wgs_x, wgs_y
                        del src_x, src_y, s_col, s_row, s_row_local
                        del strip_data, outside_mask, invalid

                        if (strip_start // STRIP_ROWS) % 20 == 0:
                            log.info(f"    rows {strip_start}-{strip_end}/{out_height}…")

                    log.info(f"  Band {band_idx}/{src.count} written")

            log.info(f"Raster conversion complete: {out_name}")
            band_count = src.count

        return out_path.read_bytes(), {
            "zone": zone, "method": method, "src_crs": src_crs_name,
            "output_filename": out_name,
            "width": out_width, "height": out_height, "bands": band_count,
        }
