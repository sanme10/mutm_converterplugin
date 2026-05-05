from qgis.core import QgsApplication
from .provider import MUTMProvider


class MUTMConverterPlugin:
    def __init__(self, iface):
        self.iface    = iface
        self.provider = None

    def initGui(self):
        self.provider = MUTMProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def unload(self):
        QgsApplication.processingRegistry().removeProvider(self.provider)
