from qgis.core import QgsProcessingProvider
from .vector_algorithm import VectorToMUTMAlgorithm
from .raster_algorithm import RasterToMUTMAlgorithm


class MUTMProvider(QgsProcessingProvider):

    def id(self):
        return "mutm_converter"

    def name(self):
        return "UTM to MUTM Converter"

    def longName(self):
        return "UTM to MUTM Converter (Nepal Everest 1830)"

    def icon(self):
        return QgsProcessingProvider.icon(self)

    def loadAlgorithms(self):
        self.addAlgorithm(VectorToMUTMAlgorithm())
        self.addAlgorithm(RasterToMUTMAlgorithm())
