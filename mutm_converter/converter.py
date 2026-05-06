"""
converter.py — Geodetically correct UTM → MUTM conversion

Uses ONLY osgeo.gdal / osgeo.ogr / osgeo.osr / numpy / scipy.
Zero pyproj. All CRS operations go through osr which shares
QGIS's internal PROJ runtime — no dll conflicts possible.
"""

import io, zipfile, tempfile, logging, json
from pathlib import Path

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


# ── CRS / PROJ HELPERS (osr only) ────────────────────────────────────────────

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


def _make_wgs84():
    """Return an osr.SpatialReference for WGS84."""
    from osgeo import osr
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def _make_mutm(zone: int, method: str):
    """Return an osr.SpatialReference for the MUTM zone."""
    from osgeo import osr
    srs = osr.SpatialReference()
    srs.ImportFromProj4(_mutm_proj4_str(zone, method))
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def _make_src_srs(wkt: str):
    """
    Build an osr.SpatialReference from WKT.
    If compound (3D), extract the horizontal component only.
    """
    from osgeo import osr
    srs = osr.SpatialReference()
    srs.ImportFromWkt(wkt)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    # Strip vertical component from compound CRS
    if srs.IsCompound():
        horiz_wkt = srs.GetAttrValue("PROJCS") or srs.GetAttrValue("GEOGCS")
        if horiz_wkt:
            h = osr.SpatialReference()
            h.ImportFromWkt(srs.ExportToWkt())
            # Use StripVertical if available (GDAL >= 3.4)
            if hasattr(h, 'StripVertical'):
                h.StripVertical()
            srs = h
    return srs


def _build_ct(src_srs, dst_srs):
    """Build an osr.CoordinateTransformation."""
    from osgeo import osr
    ct = osr.CoordinateTransformation(src_srs, dst_srs)
    return ct


def _detect_zone(src_srs, left, bottom, right, top) -> int:
    """Detect MUTM zone from bounding box by transforming corners to WGS84."""
    import numpy as np
    wgs84 = _make_wgs84()
    ct    = _build_ct(src_srs, wgs84)

    corners = [(left, bottom), (right, bottom), (left, top), (right, top)]
    lons = []
    for x, y in corners:
        lon, lat, _ = ct.TransformPoint(x, y)
        lons.append(lon)

    lon = float(np.mean(lons))
    for zone, (lo, hi) in MUTM_ZONES.items():
        if lo <= lon < hi:
            return zone
    raise ValueError(
        f"Centroid longitude {lon:.4f} is outside Nepal MUTM coverage "
        f"(79.5-88.5 E). Check your input CRS."
    )


# ── GEOMETRY TRANSFORM ────────────────────────────────────────────────────────

def _transform_geometry(geom_dict: dict, ct1, ct2) -> dict:
    """
    Transform a GeoJSON geometry dict through two osr CoordinateTransformations:
      ct1: src → WGS84
      ct2: WGS84 → MUTM
    """
    import numpy as np

    def tx(ring):
        if not ring:
            return ring
        arr  = np.array(ring, dtype=np.float64)
        # TransformPoints expects list of (x, y) or (x, y, z)
        pts1 = ct1.TransformPoints(arr.tolist())
        pts2 = ct2.TransformPoints([(p[0], p[1]) for p in pts1])
        return [(p[0], p[1]) for p in pts2]

    gtype  = geom_dict["type"]
    coords = geom_dict["coordinates"]

    if gtype == "Point":
        p1 = ct1.TransformPoint(coords[0], coords[1])
        p2 = ct2.TransformPoint(p1[0], p1[1])
        new_coords = [p2[0], p2[1]]
    elif gtype in ("MultiPoint", "LineString"):
        new_coords = tx(coords)
    elif gtype == "MultiLineString":
        new_coords = [tx(r) for r in coords]
    elif gtype == "Polygon":
        new_coords = [tx(r) for r in coords]
    elif gtype == "MultiPolygon":
        new_coords = [[tx(r) for r in poly] for poly in coords]
    else:
        return geom_dict

    return {"type": gtype, "coordinates": new_coords}


# ── SHAPEFILE CONVERSION ──────────────────────────────────────────────────────

def convert_shapefile(zip_bytes: bytes, method: str = "7param") -> tuple[bytes, dict]:
    from osgeo import ogr, osr
    import numpy as np

    ogr.UseExceptions()

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

        src_ds  = ogr.Open(str(shp_path))
        if src_ds is None:
            raise ValueError(f"Could not open shapefile: {shp_path.name}")

        src_lyr  = src_ds.GetLayer(0)
        src_srs  = src_lyr.GetSpatialRef()
        if src_srs is None:
            raise ValueError("Shapefile has no CRS (.prj missing). Please add a .prj file.")

        src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        src_crs_name = src_srs.GetName() or "Unknown"
        total_feats  = src_lyr.GetFeatureCount()
        geom_type    = src_lyr.GetGeomType()
        ext          = src_lyr.GetExtent()   # (min_x, max_x, min_y, max_y)
        left, right, bottom, top = ext

        log.info(f"Source CRS: {src_crs_name}  |  Features: {total_feats}")

        zone = _detect_zone(src_srs, left, bottom, right, top)
        log.info(f"Detected MUTM zone: {zone}")

        wgs84    = _make_wgs84()
        mutm_srs = _make_mutm(zone, method)
        ct1      = _build_ct(src_srs, wgs84)    # src → WGS84
        ct2      = _build_ct(wgs84, mutm_srs)   # WGS84 → MUTM

        # Output shapefile
        out_name = shp_path.stem + f"_MUTM{zone}"
        out_dir  = tmp / "out"
        out_dir.mkdir()
        out_shp  = out_dir / f"{out_name}.shp"

        driver   = ogr.GetDriverByName("ESRI Shapefile")
        dst_ds   = driver.CreateDataSource(str(out_shp))
        dst_lyr  = dst_ds.CreateLayer(out_name, srs=mutm_srs, geom_type=geom_type)

        src_defn = src_lyr.GetLayerDefn()
        for i in range(src_defn.GetFieldCount()):
            dst_lyr.CreateField(src_defn.GetFieldDefn(i))
        dst_defn = dst_lyr.GetLayerDefn()

        src_lyr.ResetReading()
        for feat in src_lyr:
            geom = feat.GetGeometryRef()
            if geom is None:
                continue
            geom_dict     = json.loads(geom.ExportToJson())
            new_geom_dict = _transform_geometry(geom_dict, ct1, ct2)
            new_geom      = ogr.CreateGeometryFromJson(json.dumps(new_geom_dict))

            new_feat = ogr.Feature(dst_defn)
            new_feat.SetGeometry(new_geom)
            for i in range(dst_defn.GetFieldCount()):
                new_feat.SetField(i, feat.GetField(i))
            dst_lyr.CreateFeature(new_feat)

        dst_ds.FlushCache()
        dst_ds = None
        src_ds = None

        log.info(f"Shapefile conversion complete: {out_name}.shp")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in out_dir.iterdir():
                zf.write(f, f.name)
        buf.seek(0)

        return buf.read(), {
            "zone": zone, "method": method, "src_crs": src_crs_name,
            "output_filename": f"{out_name}.zip",
            "features": total_feats,
            "geometry_type": ogr.GeometryTypeToName(geom_type),
        }


# ── RASTER CONVERSION ─────────────────────────────────────────────────────────

def convert_raster(
    file_bytes: bytes,
    src_filename: str,
    method: str = "7param",
) -> tuple[bytes, dict]:
    from osgeo import gdal, osr
    from scipy.ndimage import map_coordinates
    import numpy as np

    gdal.UseExceptions()

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        in_path = tmp / src_filename
        in_path.write_bytes(file_bytes)
        del file_bytes

        src_ds = gdal.Open(str(in_path))
        if src_ds is None:
            raise ValueError(f"Could not open raster: {src_filename}")

        src_wkt = src_ds.GetProjection()
        if not src_wkt:
            raise ValueError("Raster has no CRS metadata. Cannot reproject.")

        src_srs = _make_src_srs(src_wkt)
        src_crs_name = src_srs.GetName() or "Unknown"
        log.info(f"Source raster CRS: {src_crs_name}")

        gt         = src_ds.GetGeoTransform()
        src_width  = src_ds.RasterXSize
        src_height = src_ds.RasterYSize
        band_count = src_ds.RasterCount

        left   = gt[0]
        top_y  = gt[3]
        right  = left  + gt[1] * src_width
        bottom = top_y + gt[5] * src_height

        zone = _detect_zone(src_srs, left, bottom, right, top_y)
        log.info(f"Detected MUTM zone: {zone}")

        wgs84    = _make_wgs84()
        mutm_srs = _make_mutm(zone, method)

        ct1     = _build_ct(src_srs, wgs84)     # src → WGS84
        ct2     = _build_ct(wgs84, mutm_srs)    # WGS84 → MUTM
        ct2_inv = _build_ct(mutm_srs, wgs84)    # MUTM → WGS84
        ct1_inv = _build_ct(wgs84, src_srs)     # WGS84 → src

        # Output MUTM bounding box
        corners = [
            (left, bottom), (right, bottom),
            (left, top_y),  (right, top_y),
        ]
        mutm_corners = []
        for x, y in corners:
            wgs = ct1.TransformPoint(x, y)
            mut = ct2.TransformPoint(wgs[0], wgs[1])
            mutm_corners.append(mut)

        out_left   = min(p[0] for p in mutm_corners)
        out_right  = max(p[0] for p in mutm_corners)
        out_bottom = min(p[1] for p in mutm_corners)
        out_top    = max(p[1] for p in mutm_corners)

        src_res_x = abs(gt[1])
        src_res_y = abs(gt[5])
        out_width  = max(1, int(round((out_right - out_left)   / src_res_x)))
        out_height = max(1, int(round((out_top   - out_bottom) / src_res_y)))

        STRIP_ROWS = 50
        log.info(f"Output grid: {out_width} x {out_height} pixels")

        out_gt = (out_left, src_res_x, 0.0, out_top, 0.0, -src_res_y)

        stem     = Path(src_filename).stem
        out_name = f"{stem}_MUTM{zone}.tif"
        out_path = tmp / out_name

        band1      = src_ds.GetRasterBand(1)
        dtype_gdal = band1.DataType
        src_nodata = band1.GetNoDataValue()

        gdal_to_np = {
            gdal.GDT_Byte:    np.uint8,
            gdal.GDT_UInt16:  np.uint16,
            gdal.GDT_Int16:   np.int16,
            gdal.GDT_UInt32:  np.uint32,
            gdal.GDT_Int32:   np.int32,
            gdal.GDT_Float32: np.float32,
            gdal.GDT_Float64: np.float64,
        }
        np_dtype = gdal_to_np.get(dtype_gdal, np.float32)

        if src_nodata is not None:
            nodata_write = float(src_nodata)
        elif np.issubdtype(np_dtype, np.floating):
            nodata_write = np.nan
        else:
            nodata_write = 0.0

        driver = gdal.GetDriverByName("GTiff")
        dst_ds = driver.Create(
            str(out_path), out_width, out_height, band_count, dtype_gdal,
            options=["COMPRESS=LZW", "TILED=YES"],
        )
        dst_ds.SetGeoTransform(out_gt)
        dst_ds.SetProjection(mutm_srs.ExportToWkt())

        cols       = np.arange(out_width,  dtype=np.float64)
        mutm_x_row = out_left + (cols + 0.5) * src_res_x

        for band_idx in range(1, band_count + 1):
            log.info(f"  Processing band {band_idx}/{band_count}…")
            src_band = src_ds.GetRasterBand(band_idx)
            dst_band = dst_ds.GetRasterBand(band_idx)
            if src_nodata is not None:
                dst_band.SetNoDataValue(src_nodata)

            for strip_start in range(0, out_height, STRIP_ROWS):
                strip_end = min(strip_start + STRIP_ROWS, out_height)
                strip_h   = strip_end - strip_start

                rows_strip   = np.arange(strip_start, strip_end, dtype=np.float64)
                mutm_y_strip = out_top - (rows_strip + 0.5) * src_res_y

                mutm_x_grid = np.broadcast_to(mutm_x_row, (strip_h, out_width)).copy()
                mutm_y_grid = mutm_y_strip[:, np.newaxis] * np.ones((1, out_width))

                # Back-project: MUTM → WGS84 → src
                pts_mutm = list(zip(mutm_x_grid.ravel(), mutm_y_grid.ravel()))
                pts_wgs  = ct2_inv.TransformPoints(pts_mutm)
                pts_src  = ct1_inv.TransformPoints([(p[0], p[1]) for p in pts_wgs])

                src_x = np.array([p[0] for p in pts_src])
                src_y = np.array([p[1] for p in pts_src])

                s_col = ((src_x - left)  / (right - left)   * src_width  - 0.5).reshape(strip_h, out_width)
                s_row = ((top_y - src_y) / (top_y - bottom) * src_height - 0.5).reshape(strip_h, out_width)

                outside_mask = (
                    (s_col < 0) | (s_col > src_width  - 1) |
                    (s_row < 0) | (s_row > src_height - 1)
                )

                if (~outside_mask).any():
                    row_min = max(0, int(np.floor(s_row[~outside_mask].min())) - 1)
                    row_max = min(src_height - 1, int(np.ceil(s_row[~outside_mask].max())) + 1)
                else:
                    row_min, row_max = 0, 0
                win_height = row_max - row_min + 1

                src_tile = src_band.ReadAsArray(
                    xoff=0, yoff=row_min,
                    win_xsize=src_width, win_ysize=win_height,
                ).astype(np.float64)

                if src_nodata is not None:
                    src_tile[src_tile == float(src_nodata)] = np.nan

                s_row_local = s_row - row_min

                strip_data = map_coordinates(
                    src_tile, [s_row_local, s_col],
                    order=0, mode="constant", cval=0.0, prefilter=False,
                )
                del src_tile

                invalid = outside_mask | np.isnan(strip_data)
                strip_data[invalid] = nodata_write

                if np.issubdtype(np_dtype, np.integer):
                    strip_data = np.round(strip_data)

                dst_band.WriteArray(
                    strip_data.astype(np_dtype).reshape(strip_h, out_width),
                    xoff=0, yoff=strip_start,
                )

                del mutm_x_grid, mutm_y_grid, pts_mutm, pts_wgs, pts_src
                del src_x, src_y, s_col, s_row, s_row_local
                del strip_data, outside_mask, invalid

            dst_band.FlushCache()
            log.info(f"  Band {band_idx}/{band_count} written")

        dst_ds.FlushCache()
        dst_ds = None
        src_ds = None

        log.info(f"Raster conversion complete: {out_name}")

        return out_path.read_bytes(), {
            "zone": zone, "method": method, "src_crs": src_crs_name,
            "output_filename": out_name,
            "width": out_width, "height": out_height, "bands": band_count,
        }
