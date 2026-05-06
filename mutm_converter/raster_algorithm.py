from pathlib import Path

from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterEnum,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterFileDestination,
    QgsRasterLayer,
    QgsProject,
)


METHOD_OPTIONS = ["7param (Helmert, recommended)", "3param (Molodensky)"]


def _ensure_rasterio(feedback):
    """
    Try to import rasterio. If missing, install via pip internal API.
    IMPORTANT: never touch pyproj or numpy in sys.modules — QGIS owns
    those and reloading them causes a PROJ dll conflict and hard crash.
    """
    import sys, importlib, site

    SAFE_TO_CLEAR = ["rasterio", "scipy"]

    def _clear_cache():
        for mod in list(sys.modules.keys()):
            if any(mod == s or mod.startswith(s + ".") for s in SAFE_TO_CLEAR):
                sys.modules.pop(mod, None)
        importlib.invalidate_caches()

    def _refresh_path():
        try:
            for path in site.getsitepackages():
                if path not in sys.path:
                    sys.path.insert(0, path)
        except Exception:
            pass

    try:
        import rasterio
        return True
    except ImportError:
        pass

    feedback.pushInfo("rasterio not found — attempting automatic install…")
    _clear_cache()
    _refresh_path()

    try:
        from pip._internal.cli.main import main as pip_main
        pip_main(["install", "rasterio", "scipy", "--quiet"])
    except Exception as e:
        feedback.pushWarning(f"Auto-install failed: {e}")
        return False

    _refresh_path()
    _clear_cache()

    try:
        import rasterio
        feedback.pushInfo("rasterio installed successfully.")
        return True
    except ImportError:
        feedback.pushWarning(
            "rasterio installed but cannot be imported yet.
"
            "Please restart QGIS — the algorithm will work after restart."
        )
        return False


class RasterToMUTMAlgorithm(QgsProcessingAlgorithm):

    INPUT  = "INPUT"
    METHOD = "METHOD"
    OUTPUT = "OUTPUT"

    def name(self):
        return "raster_to_mutm"

    def displayName(self):
        return "Convert raster (GeoTIFF) to MUTM (Nepal)"

    def group(self):
        return "UTM to MUTM Converter"

    def groupId(self):
        return "mutm_converter"

    def shortHelpString(self):
        return (
            "Converts a GeoTIFF raster from any UTM/projected CRS to "
            "Nepal Modified UTM (Everest 1830).\n\n"
            "Input: any raster layer loaded in QGIS (GeoTIFF).\n"
            "Output: a reprojected GeoTIFF file.\n\n"
            "Method:\n"
            "  7-param — full Helmert transform (recommended)\n"
            "  3-param — translation only (Molodensky)\n\n"
            "Pixel size is preserved exactly (e.g. 1×1 m stays 1×1 m).\n"
            "The converted raster is automatically added to the QGIS canvas.\n\n"
            "Requires: rasterio, scipy.\n"
            "If missing, the algorithm will attempt to install them automatically.\n"
            "If auto-install fails, open OSGeo4W Shell and run:\n"
            "    pip install rasterio scipy"
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterRasterLayer(
                self.INPUT,
                "Input raster layer (GeoTIFF)",
            )
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.METHOD,
                "Datum shift method",
                options=METHOD_OPTIONS,
                defaultValue=1,
            )
        )
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT,
                "Output GeoTIFF file",
                fileFilter="GeoTIFF files (*.tif *.tiff)",
                optional=True,
                defaultValue="TEMPORARY_OUTPUT",
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        layer      = self.parameterAsRasterLayer(parameters, self.INPUT, context)
        method_idx = self.parameterAsEnum(parameters, self.METHOD, context)
        output_tif = self.parameterAsFileOutput(parameters, self.OUTPUT, context)

        method = "7param" if method_idx == 0 else "3param"

        if layer is None:
            raise QgsProcessingException("No input raster layer provided.")

        src_path = layer.source()
        if not src_path.lower().endswith((".tif", ".tiff", ".geotiff")):
            raise QgsProcessingException(
                f"Input must be a GeoTIFF file (.tif / .tiff). Got: {src_path}"
            )

        # ── Check / install rasterio ──────────────────────────────────────────
        if not _ensure_rasterio(feedback):
            raise QgsProcessingException(
                "rasterio is not installed and automatic install failed.\n\n"
                "Please install it manually:\n"
                "  1. Open OSGeo4W Shell (search in Start Menu, run as Administrator)\n"
                "  2. Run: pip install rasterio scipy\n"
                "  3. Restart QGIS and try again."
            )

        feedback.setProgressText("Reading raster file…")
        feedback.setProgress(5)

        with open(src_path, "rb") as f:
            file_bytes = f.read()

        src_filename = Path(src_path).name
        feedback.setProgressText(f"Running MUTM conversion ({method})…")
        feedback.setProgress(10)

        try:
            from .converter import convert_raster
        except ImportError as e:
            raise QgsProcessingException(f"Could not import converter.py: {e}")

        if feedback.isCanceled():
            return {}

        try:
            out_bytes, info = convert_raster(file_bytes, src_filename, method=method)
        except ValueError as e:
            raise QgsProcessingException(str(e))
        except Exception as e:
            raise QgsProcessingException(f"Conversion failed: {e}")

        feedback.setProgress(90)
        feedback.setProgressText("Writing output GeoTIFF…")

        # Handle TEMPORARY_OUTPUT
        if not output_tif or output_tif == "TEMPORARY_OUTPUT":
            import tempfile as _tf
            tmp_f = _tf.NamedTemporaryFile(
                suffix=".tif", delete=False,
                prefix=info["output_filename"].replace(".tif", "") + "_",
            )
            output_tif = tmp_f.name
            tmp_f.close()

        with open(output_tif, "wb") as f:
            f.write(out_bytes)

        # Load into canvas
        stem = Path(info["output_filename"]).stem
        rl   = QgsRasterLayer(output_tif, stem)
        if rl.isValid():
            QgsProject.instance().addMapLayer(rl)
            feedback.pushInfo(f"Raster '{stem}' added to canvas.")
        else:
            feedback.pushWarning(
                "Output raster could not be loaded into canvas — "
                "the file was saved successfully."
            )

        feedback.setProgress(100)
        feedback.pushInfo(
            f"Conversion complete → MUTM{info['zone']} "
            f"({info['width']}×{info['height']} px, "
            f"{info['bands']} band(s), method={info['method']})"
        )

        return {self.OUTPUT: output_tif}

    def createInstance(self):
        return RasterToMUTMAlgorithm()
