# blocks/components/visual/split_map_right.py
"""
Purpose
-------
Geteilte Karte: rechte Seite zeigt einen Layer mit übergebenen Visualisierungsparametern.
Keine Hartkodierung von Paletten/Min/Max/Opacity; alles kommt als Argument.

Contracts
---------
def render_split_map_right(
    m,
    right_layer,
    vis_params: dict,
    title: str,
    height: int = 680,
    colorbar_label: str | None = None,
) -> None

Args
----
m : geemap.foliumap.Map
right_layer : beliebiges EE-Objekt (typisch ee.Image)
vis_params : Dict[str, Any]  (z. B. {"min": 20, "max": 45, "palette": [...], "opacity": 0.6})
title : str
height : int
colorbar_label : Optional[str]

Returns
-------
None

Side Effects
------------
Rendert die Karte nach Streamlit.
"""

from typing import Dict, Any, Optional
from geemap.foliumap import Map

def _add_layer(m, layer, vis, name):
    fn = getattr(m, "add_layer", None) or getattr(m, "addLayer", None)
    if fn is None:
        raise RuntimeError("Map object lacks add_layer/addLayer")
    return fn(layer, vis, name)

def render_split_map_right(
    m: Map,
    right_layer: Any,
    vis_params: Dict[str, Any],
    title: str,
    height: int = 680,
    colorbar_label: Optional[str] = None,
) -> None:
    """Render split map; do not hardcode palettes/min/max; use vis_params."""
    # Split-Map (rechte Seite)
    try:
        # Basemap-Layer-Objekt statt String:
        from geemap.foliumap import basemaps
        left = getattr(basemaps, "OpenStreetMap", None)
        if left is None:
            raise RuntimeError("Basemap not available")
        m.split_map(
            left_layer=left,           # echtes Tile-Layer-Objekt
            right_layer=right_layer,   # ee.Image oder Tile-Layer
            right_vis=vis_params,
            right_name=title,
        )
    except Exception:
        # Fallback: normales Layer
        fn = getattr(m, "add_layer", None) or getattr(m, "addLayer", None)
        if fn is None:
            raise RuntimeError("Map-Objekt unterstützt weder add_layer noch addLayer.")
        fn(right_layer, vis_params, title)


    # Optionale Farbleiste, nur wenn Min/Max/Palette vorhanden
    try:
        if all(k in vis_params for k in ("min", "max", "palette")):
            m.add_colorbar(
                vis_params={"min": vis_params["min"], "max": vis_params["max"], "palette": vis_params["palette"]},
                label=(colorbar_label if colorbar_label is not None else title),
                orientation="horizontal",
                layer_name=title,
            )
    except Exception:
        pass

    # Streamlit-Render
    m.to_streamlit(height=int(height))
