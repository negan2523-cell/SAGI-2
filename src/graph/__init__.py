def cargar_instancia(df):
    from .osrm_network import cargar_instancia as cargar_osrm

    return cargar_osrm(df)


__all__ = ["cargar_instancia"]
