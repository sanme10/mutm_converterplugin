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
            "The converted layer is automatically added to the QGIS canvas.\n\n"
            "No extra installation required — uses QGIS built-in libraries."
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

        try:
            from .converter import convert_shapefile
        except Exception as e:
            raise QgsProcessingException(f"Could not load converter: {e}")

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
        QgsVectorFileWriter. Handles any source format including
        /vsizip/ paths and |layername= sources correctly.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp  = Path(tmp)
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

            feedback.pushInfo("Layer exported to temporary shapefile.")

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in tmp.iterdir():
                    zf.write(f, f.name)
            buf.seek(0)
            return buf.read()

    def _load_from_zip(self, zip_bytes: bytes, zip_filename: str, feedback):
        """
        Extract converted shapefile to a persistent temp dir and load into
        canvas. Uses mkdtemp() instead of TemporaryDirectory() because on
        Windows, QGIS holds shapefile files open after addMapLayer() and
        TemporaryDirectory cleanup raises PermissionError.
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
