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
    QgsProcessingParameterFile,
    QgsVectorLayer,
    QgsProject,
    QgsMessageLog,
    Qgis,
)


METHOD_OPTIONS = ["7param (Helmert, recommended)", "3param (Molodensky)"]


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
                defaultValue=0,
            )
        )
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT,
                "Output ZIP file",
                fileFilter="ZIP files (*.zip)",
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        layer  = self.parameterAsVectorLayer(parameters, self.INPUT, context)
        method_idx = self.parameterAsEnum(parameters, self.METHOD, context)
        output_zip = self.parameterAsFileOutput(parameters, self.OUTPUT, context)

        method = "7param" if method_idx == 0 else "3param"

        if layer is None:
            raise QgsProcessingException("No input layer provided.")

        feedback.setProgressText("Exporting layer to shapefile ZIP…")

        # Export the QGIS layer to a temporary shapefile ZIP in memory
        zip_bytes = self._layer_to_zip(layer, feedback)

        feedback.setProgressText(f"Running MUTM conversion ({method})…")

        # Import your existing converter
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

        feedback.setProgressText(
            f"Writing output ZIP… zone=MUTM{info['zone']}, "
            f"features={info['features']}"
        )

        # Write the output ZIP
        with open(output_zip, "wb") as f:
            f.write(out_bytes)

        # Load converted shapefile into QGIS
        self._load_from_zip(out_bytes, info["output_filename"], context, feedback)

        feedback.pushInfo(
            f"Conversion complete → MUTM{info['zone']} "
            f"({info['features']} features, method={info['method']})"
        )

        return {self.OUTPUT: output_zip}

    # ── helpers ───────────────────────────────────────────────────────────────

    def _layer_to_zip(self, layer, feedback) -> bytes:
        """Export a QGIS vector layer to an in-memory shapefile ZIP."""
        import processing  # QGIS Processing module

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            shp_path = str(tmp / f"{layer.name()}.shp")

            result = processing.run(
                "native:savefeatures",
                {
                    "INPUT":       layer,
                    "OUTPUT":      shp_path,
                    "LAYER_NAME":  layer.name(),
                    "DATASOURCE_OPTIONS": "",
                    "LAYER_OPTIONS": "",
                },
            )

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in tmp.iterdir():
                    zf.write(f, f.name)
            buf.seek(0)
            return buf.read()

    def _load_from_zip(self, zip_bytes, zip_filename, context, feedback):
        """Extract shapefile from ZIP and add it to the QGIS project."""
        stem = Path(zip_filename).stem  # e.g. "myfile_MUTM84"
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                zf.extractall(tmp)
            shp_files = list(tmp.glob("*.shp"))
            if shp_files:
                vl = QgsVectorLayer(str(shp_files[0]), stem, "ogr")
                if vl.isValid():
                    QgsProject.instance().addMapLayer(vl)
                    feedback.pushInfo(f"Layer '{stem}' added to canvas.")
                else:
                    feedback.pushWarning("Output layer could not be loaded into canvas.")

    def createInstance(self):
        return VectorToMUTMAlgorithm()
