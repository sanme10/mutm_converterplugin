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
            "The converted raster is automatically added to the QGIS canvas."
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
                defaultValue=0,
            )
        )
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT,
                "Output GeoTIFF file",
                fileFilter="GeoTIFF files (*.tif *.tiff)",
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
                "Input must be a GeoTIFF file (.tif / .tiff). "
                f"Got: {src_path}"
            )

        feedback.setProgressText("Reading raster file…")
        with open(src_path, "rb") as f:
            file_bytes = f.read()

        src_filename = Path(src_path).name

        feedback.setProgressText(f"Running MUTM conversion ({method})…")
        feedback.setProgress(10)

        try:
            from .converter import convert_raster
        except ImportError as e:
            raise QgsProcessingException(
                f"Could not import converter.py: {e}\n"
                "Make sure pyproj, rasterio, numpy, and scipy are installed "
                "in your QGIS Python environment."
            )

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

        with open(output_tif, "wb") as f:
            f.write(out_bytes)

        # Load converted raster into QGIS
        stem = Path(info["output_filename"]).stem
        rl   = QgsRasterLayer(output_tif, stem)
        if rl.isValid():
            QgsProject.instance().addMapLayer(rl)
            feedback.pushInfo(f"Raster '{stem}' added to canvas.")
        else:
            feedback.pushWarning("Output raster could not be loaded into canvas.")

        feedback.setProgress(100)
        feedback.pushInfo(
            f"Conversion complete → MUTM{info['zone']} "
            f"({info['width']}×{info['height']} px, "
            f"{info['bands']} band(s), method={info['method']})"
        )

        return {self.OUTPUT: output_tif}

    def createInstance(self):
        return RasterToMUTMAlgorithm()
