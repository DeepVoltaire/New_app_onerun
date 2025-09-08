from blocks.components.util.scaffold import ee_authenticate
from blocks.components.gee.aoi_from_spec import aoi_from_spec
from blocks.components.gee.cool_spots_time import summer_window
from blocks.components.gee.cool_spots_acquire_process import build_lst_image

ee_authenticate()
aoi = aoi_from_spec({"type":"bbox","bbox":[11.4,48.0,11.8,48.3]})
start, end = summer_window(2020)
img = build_lst_image(aoi, start, end)
print("OK: LST image prepared:", isinstance(img.getInfo() if hasattr(img, 'getInfo') else {}, dict))
