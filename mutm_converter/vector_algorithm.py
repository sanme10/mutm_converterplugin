import io
import zipfile
import tempfile
from pathlib import Path

from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFileDestination,
    QgsVectorLayer,
    QgsVectorFileWriter,
    QgsCoordinateTransformContext,
    QgsProject,
)


METHOD_OPTIONS = ["7param (Helmert, recommended)", "3param (Molodensky)"]


def _ensure_fiona(feedback):
    """
    Try to import fiona. If missing, install via pip internal API
    (avoids subprocess which QGIS intercepts on Windows).
    Clears sys.modules cache after install so the fresh package is
    importable in the same session without restarting QGIS.
    """
    import sys, importlib

    def _clear_cache():
        for mod in ["fiona", "fiona.crs", "fiona.ogrext", "geopandas", "pyproj", "numpy", "scipy"]:
            sys.modules.pop(mod, None)
        importlib.invalidate_caches()

    try:
        import fiona
        return True
    except ImportError:
        pass

    feedback.pushInfo("fiona not found — attempting automatic install…")
    _clear_cache()

    try:
        from pip._internal.cli.main import main as pip_main
        pip_main(["install", "fiona", "geopandas", "scipy", "--quiet"])
    except Exception as e:
        feedback.pushWarning(f"Auto-install failed: {e}")
        return False

    # Refresh site-packages path so newly installed packages are found
    import site
    for path in site.getsitepackages():
        if path not in sys.path:
            sys.path.insert(0, path)

    _clear_cache()

    try:
        import fiona
        feedback.pushInfo("fiona installed successfully.")
        return True
    except ImportError:
        feedback.pushWarning(
            "fiona installed but still not importable in this session. Please restart QGIS and run the algorithm again,it will work after restart."
        )
        return False


class VectorToMUTMAlgorithm(QgsProcessingAlgorithm):

    INPUT  = "INPUT"
    METHOD = "METHOD"
    OUTPUT = "OUTPUT"

    def name(self):
        return "vector_to_mutm"

    def displayName(self):
        return "Convert shapefile to MUTM (Nepal)"

    def group(self):
        return "UTM to MUTM Converter"

    def groupId(self):
        return "mutm_converter"

    def shortHelpString(self):
        return (
            "Converts a vector layer from any UTM/projected CRS to "
            "Nepal Modified UTM (Everest 1830).\n\n"
            "Input: any vector layer loaded in QGIS.\n"
            "Output: a ZIP file containing the reprojected shapefile.\n\n"
            "Method:\n"
            "  7-param — full Helmert transform (recommended)\n"
            "  3-param — translation only (Molodensky)\n\n"
            "The converted layer is automatically added to the QGIS canvas."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.INPUT,
                "Input vector layer",
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
                "Output ZIP file",
                fileFilter="ZIP files (*.zip)",
                optional=True,
                defaultValue="TEMPORARY_OUTPUT",
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        layer      = self.parameterAsVectorLayer(parameters, self.INPUT, context)
        method_idx = self.parameterAsEnum(parameters, self.METHOD, context)
        output_zip = self.parameterAsFileOutput(parameters, self.OUTPUT, context)

        method = "7param" if method_idx == 0 else "3param"

        if layer is None:
            raise QgsProcessingException("No input layer provided.")

        feedback.setProgressText("Exporting layer to shapefile…")
        feedback.setProgress(5)

        zip_bytes = self._layer_to_zip(layer, feedback)

        feedback.setProgressText(f"Running MUTM conversion ({method})…")
        feedback.setProgress(20)

        if not _ensure_fiona(feedback):
            raise QgsProcessingException(
                "fiona could not be installed automatically."
                "Please install manually:"
                "  1. Open OSGeo4W Shell (Start Menu, run as Administrator)"
                "  2. Run: pip install fiona geopandas scipy"
                "  3. Restart QGIS and try again."
            )

        try:
            from .converter import convert_shapefile
        except ImportError as e:
            raise QgsProcessingException(
                f"Could not import converter.py: {e}\n"
                "Make sure pyproj, fiona, geopandas, and scipy are installed "
                "in your QGIS Python environment."
            )

        if feedback.isCanceled():
            return {}

        try:
            out_bytes, info = convert_shapefile(zip_bytes, method=method)
        except ValueError as e:
            raise QgsProcessingException(str(e))
        except Exception as e:
            raise QgsProcessingException(f"Conversion failed: {e}")

        feedback.setProgress(90)
        feedback.setProgressText("Writing output ZIP…")

        # Handle TEMPORARY_OUTPUT
        if not output_zip or output_zip == "TEMPORARY_OUTPUT":
            import tempfile as _tf
            tmp_f = _tf.NamedTemporaryFile(
                suffix=".zip", delete=False,
                prefix=info["output_filename"].replace(".zip", "") + "_",
            )
            output_zip = tmp_f.name
            tmp_f.close()

        with open(output_zip, "wb") as f:
            f.write(out_bytes)

        feedback.setProgress(95)

        self._load_from_zip(out_bytes, info["output_filename"], feedback)

        feedback.setProgress(100)
        feedback.pushInfo(
            f"Conversion complete → MUTM{info['zone']} "
            f"({info['features']} features, method={info['method']}, "
            f"source CRS: {info['src_crs']})"
        )

        return {self.OUTPUT: output_zip}

    # ── helpers ───────────────────────────────────────────────────────────────

    def _layer_to_zip(self, layer, feedback) -> bytes:
        """
        Export a QGIS vector layer to an in-memory shapefile ZIP using
        QgsVectorFileWriter. This correctly handles any source format
        including /vsizip/ paths and |layername= sources.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)

            # Build a clean filename — never derive from layer.source()
            # which contains /vsizip/ prefixes and |layername= suffixes
            stem = "".join(
                c for c in layer.name() if c.isalnum() or c in "_-"
            ) or "input"
            shp_path = str(tmp / f"{stem}.shp")

            options              = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName   = "ESRI Shapefile"
            options.fileEncoding = "UTF-8"

            error, msg, _, _ = QgsVectorFileWriter.writeAsVectorFormatV3(
                layer,
                shp_path,
                QgsCoordinateTransformContext(),
                options,
            )

            if error != QgsVectorFileWriter.NoError:
                raise QgsProcessingException(
                    f"Failed to export layer to shapefile: {msg}"
                )

            feedback.pushInfo(f"Layer exported to temporary shapefile.")

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in tmp.iterdir():
                    zf.write(f, f.name)
            buf.seek(0)
            return buf.read()

    def _load_from_zip(self, zip_bytes: bytes, zip_filename: str, feedback):
        """
        Extract the converted shapefile to a persistent temp directory and
        load it into the canvas. The directory is NOT deleted automatically —
        on Windows, QGIS holds shapefile component files open (via OGR) after
        addMapLayer(), so TemporaryDirectory.__exit__ raises PermissionError.
        We use mkdtemp() and let the OS clean it up on reboot instead.
        """
        import tempfile as _tf
        stem    = Path(zip_filename).stem
        out_dir = Path(_tf.mkdtemp(prefix="mutm_"))
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            zf.extractall(out_dir)
        shp_files = list(out_dir.glob("*.shp"))
        if shp_files:
            vl = QgsVectorLayer(str(shp_files[0]), stem, "ogr")
            if vl.isValid():
                QgsProject.instance().addMapLayer(vl)
                feedback.pushInfo(f"Layer '{stem}' added to canvas.")
            else:
                feedback.pushWarning(
                    "Output layer could not be loaded into canvas — "
                    "the ZIP was saved successfully."
                )

    def createInstance(self):
        return VectorToMUTMAlgorithm()
