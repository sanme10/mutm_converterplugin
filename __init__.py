def classFactory(iface):
    from .mutm_converter import MUTMConverterPlugin
    return MUTMConverterPlugin(iface)
