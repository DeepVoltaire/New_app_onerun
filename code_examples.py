heatmap_code = """
import streamlit as st
import ee
import geemap.foliumap as geemap

def mask_landsat_l2(img):
    qa = img.select('QA_PIXEL')
    cond = (qa.bitwiseAnd(1 << 1).eq(0)   # dilated
            .And(qa.bitwiseAnd(1 << 2).eq(0))  # cirrus
            .And(qa.bitwiseAnd(1 << 3).eq(0))  # cloud
            .And(qa.bitwiseAnd(1 << 4).eq(0))  # shadow
            .And(qa.bitwiseAnd(1 << 5).eq(0))) # snow
    return img.updateMask(cond).copyProperties(img, img.propertyNames())

# -------------------- Data builders --------------------
def get_landsat_lst(aoi, start, end):
    col = (ee.ImageCollection('LANDSAT/LC08/C02/T1_L2')
           .merge(ee.ImageCollection('LANDSAT/LC09/C02/T1_L2'))
           .filterBounds(aoi)
           .filterDate(start, end)
           .map(mask_landsat_l2)
           .map(lambda img: img.addBands(
               img.select('ST_B10')
                  .multiply(0.00341802).add(149.0).subtract(273.15)
                  .rename('LST_C'),
               None, True)))
    return col.select('LST_C').median().clip(aoi)


def main():
    POI_LON, POI_LAT = 2.3522, 48.8566 # Paris
    RADIUS_KM = 15         # AOI radius
    YEAR = 2024            # Summer year

    TEMP_OPACITY = 0.6
    temp_vis = {
        "min": 20,
        "max": 45, 
        "palette": ['#313695','#4575b4','#74add1','#abd9e9','#e0f3f8','#ffffbf',
                    '#fee090','#fdae61','#f46d43','#d73027','#a50026']
    }

    Map = geemap.Map(center=(POI_LAT, POI_LON), zoom=12, basemap="SATELLITE") # Uses Esri.WorldImagery on Shenzhen
    Map.add_basemap("CartoDB.PositronOnlyLabels")  # labels-only overlay

    # -------------------- Helpers --------------------
    def make_aoi(lon, lat, radius_km):
        return ee.Geometry.Point([lon, lat]).buffer(radius_km * 1000).bounds()

    def summer_dates(year):
        start = ee.Date.fromYMD(year, 6, 1)
        end   = ee.Date.fromYMD(year, 8, 31).advance(1, 'day')  # exclusive
        return start, end

    # -------------------- Build imagery --------------------
    AOI = make_aoi(POI_LON, POI_LAT, RADIUS_KM)
    start, end = summer_dates(YEAR)
    lst  = get_landsat_lst(AOI, start, end)
    right_layer = geemap.ee_tile_layer(lst, temp_vis, 'Temperature (°C)', opacity=TEMP_OPACITY)

    Map.split_map("", right_layer) # empty left layer
    Map.add_colorbar(
        vis_params=temp_vis,
        label="Temperature (°C)",
        orientation="horizontal",  # or "vertical"
        background_color="white",        # <-- sets the bounding box background
        font_size=14,           # optional, makes labels more visible
        position="bottomright"  # optional, controls placement
    )

    Map.to_streamlit()
"""


no2_code = """
# Nitrogen Dioxide (NO₂) Monitoring with Sentinel-5P in Streamlit + geemap

import streamlit as st
import ee
import datetime
import folium
import geemap.foliumap as geemap

def main():
    Map = geemap.Map(zoom=1, basemap="SATELLITE") # Uses Esri.WorldImagery on Shenzhen
    Map.add_basemap("CartoDB.PositronOnlyLabels")  # labels-only overlay

    no2_vis = {"min": 0, "max": 0.0002, "palette": ['black', 'blue', 'purple', 'cyan', 'green', 'yellow', 'red']}

    st.title(f"Average NO2 concentration")
    max_year = datetime.date.today().year
    year = 2025
    month = 8

    # Compute date range
    start_date = datetime.date(int(year), int(month), 1)
    end_date = (start_date.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)  # next month's first day
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")

    col = (
        ee.ImageCollection('COPERNICUS/S5P/NRTI/L3_NO2')
        .select('NO2_column_number_density')
        .filterDate(start_str, end_str)
        .mean()
    )

    right_layer = geemap.ee_tile_layer(
        col, no2_vis, f"NO2 {start_str}-{end_str}", opacity=1.0
    )
    Map.split_map("", right_layer)

    Map.add_colorbar(
        vis_params=no2_vis,
        label="NO2 Concentration (mol/m²)",
        orientation="horizontal",  # or "vertical"
        background_color="white",        # <-- sets the bounding box background
        font_size=12,           # optional, makes labels more visible
        position="bottomright"  # optional, controls placement
    )
    Map.to_streamlit()
"""