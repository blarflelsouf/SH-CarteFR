import streamlit as st
import pandas as pd
from opencage.geocoder import OpenCageGeocode
from geopy.distance import geodesic
import folium
from folium.plugins import HeatMap
from streamlit_folium import st_folium
import requests
import urllib.request
import os
import numpy as np
from shapely.geometry import Point, Polygon

# === CONFIGURATION & CHARGEMENT DES DONNÉES ===

OC_API_KEY = st.secrets["OPENCAGE_API_KEY"]
HERE_API_KEY = st.secrets["HERE_API_KEY"]

def telecharger_csv_si_absent(fichier, url):
    if not os.path.exists(fichier):
        with st.spinner(f"Téléchargement du fichier CSV depuis {url} ..."):
            urllib.request.urlretrieve(url, fichier)
        st.success("Téléchargement terminé.")

CSV_URL = "https://www.data.gouv.fr/api/1/datasets/r/f5df602b-3800-44d7-b2df-fa40a0350325"
CSV_FICHIER = "communes-france-2025.csv"
telecharger_csv_si_absent(CSV_FICHIER, CSV_URL)

@st.cache_data
def load_data():
    df = pd.read_csv('communes-france-2025.csv')
    df_clean = df.loc[:, ['nom_standard', 'reg_nom', 'population', 'latitude_mairie', 'longitude_mairie']]
    return df_clean

# === UTILS ===

def couleur_par_distance(distance, mode="km"):
    try:
        if distance is None or distance == "-":
            return "gray"
        d = float(str(distance).replace(",", "."))
        if mode == "km":
            if d < 50: return 'green'
            elif d < 120: return 'orange'
            else: return 'red'
        else:  # mode temps (min)
            if d < 30: return 'green'
            elif d < 80: return 'orange'
            else: return 'red'
    except:
        return "gray"

def geocode_adresse(adresse):
    geocoder = OpenCageGeocode(OC_API_KEY)
    try:
        results = geocoder.geocode(adresse, language='fr', no_annotations=1)
        if results and len(results) > 0:
            latitude = results[0]['geometry']['lat']
            longitude = results[0]['geometry']['lng']
            details = results[0]['components']
            region = details.get('state')
            country = results[0]['components'].get('country_code', '').upper()
            return latitude, longitude, region, country
        else:
            return None, None, None, None
    except Exception as e:
        st.error(f"Erreur géocodage OpenCage : {e}")
        st.stop()

def rayon_max_isochrone(polygone_iso, centre):
    # polygone_iso = Shapely Polygon, centre = (lat, lon)
    points = list(polygone_iso.exterior.coords)
    max_dist = 0
    for lon, lat in points:
        d = geodesic(centre, (lat, lon)).km
        if d > max_dist:
            max_dist = d
    return max_dist

# ======================= API HERE
@st.cache_data
def get_here_isochrone(lat, lon, minutes, api_key):
    print(f"Appel HERE API: {lat}, {lon}, {minutes} min")
    url = (
        "https://isoline.route.ls.hereapi.com/routing/7.2/calculateisoline.json?"
        f"apiKey={api_key}"
        f"&mode=fastest;car;traffic:disabled"
        f"&start=geo!{lat},{lon}"
        f"&rangeType=time"
        f"&range={int(minutes)*60}"  # secondes
    )
    response = requests.get(url)
    if response.status_code == 429:
        st.error("⚠️ Vous avez dépassé la limite d'utilisation de l'API HERE. Réessayez dans quelques minutes ou consultez votre quota sur https://developer.here.com.")
        st.stop()
    response.raise_for_status()
    data = response.json()
    try:
        shape_coords = data['response']['isoline'][0]['component'][0]['shape']
        coords = [tuple(map(float, s.split(','))) for s in shape_coords]
        # Ici, Here renvoie des tuples (lat, lon), alors que Shapely attend (lon, lat)
        coords = [(lon, lat) for lat, lon in coords]
        return Polygon(coords)
    except Exception as e:
        st.error("Impossible de générer la zone de recherche depuis l’API HERE. Détail : " + str(e))
        st.stop()

@st.cache_data
def villes_dans_rayon_km(df_clean, coord_depart, rayon):
    df_all_in_radius = df_clean.copy()
    df_all_in_radius['distance_km'] = df_all_in_radius.apply(
        lambda row: geodesic(coord_depart, (row['latitude_mairie'], row['longitude_mairie'])).km, axis=1)
    return df_all_in_radius[df_all_in_radius['distance_km'] <= rayon].reset_index(drop=True)

def villes_dans_isochrone(df, poly: Polygon):
    df = df.copy()
    df['in_isochrone'] = df.apply(
        lambda row: poly.contains(Point(row['longitude_mairie'], row['latitude_mairie'])),
        axis=1
    )
    return df[df['in_isochrone']].reset_index(drop=True)

# === AGGLOMÉRATION (regroupement spatial) ===

def _agglos(df_temp, n, mode="km"):
    N = len(df_temp)
    if N == 0:
        return pd.DataFrame()
    group_ids = np.full(N, -1)
    current_group = 0
    for i in range(N):
        if group_ids[i] != -1:
            continue
        group_ids[i] = current_group
        group_stack = [i]
        while group_stack:
            idx = group_stack.pop()
            lat1, lon1 = df_temp.loc[idx, ['latitude_mairie', 'longitude_mairie']]
            for j in range(N):
                if group_ids[j] != -1:
                    continue
                lat2, lon2 = df_temp.loc[j, ['latitude_mairie', 'longitude_mairie']]
                distance = geodesic((lat1, lon1), (lat2, lon2)).km
                if distance < 15:
                    group_ids[j] = current_group
                    group_stack.append(j)
        current_group += 1
    df_temp['agglomeration'] = group_ids

    agglo = (
        df_temp.groupby('agglomeration')
        .apply(lambda g: pd.Series({
            'Ville': g.loc[g['population'].idxmax()]['nom_standard'],
            'Latitude': g.loc[g['population'].idxmax()]['latitude_mairie'],
            'Longitude': g.loc[g['population'].idxmax()]['longitude_mairie'],
            'Distance (en km)': g.loc[g['population'].idxmax()]['distance_km'] if mode == "km" else None,
            'Population': int(g['population'].sum()),
            'Région': g['reg_nom'].mode()[0]
        }))
        .reset_index(drop=True)
    )

    agglo = agglo.sort_values('Population', ascending=False).head(n).reset_index(drop=True)
    return agglo

def gd_villes_dans_rayon_km(df_clean, coord_depart, rayon, min_pop, n):
    df_temp = df_clean[df_clean['population'] > min_pop].copy()
    df_temp['distance_km'] = df_temp.apply(
        lambda row: geodesic(coord_depart, (row['latitude_mairie'], row['longitude_mairie'])).km, axis=1)
    df_temp = df_temp[df_temp['distance_km'] <= rayon].reset_index(drop=True)
    return _agglos(df_temp, n, mode="km")

def gd_villes_dans_isochrone(df_clean, min_pop, n, polygone_isochrone):
    df_temp = df_clean[df_clean['population'] > min_pop].copy()
    df_temp['in_isochrone'] = df_temp.apply(
        lambda row: polygone_isochrone.contains(Point(row['longitude_mairie'], row['latitude_mairie'])),
        axis=1
    )
    df_temp = df_temp[df_temp['in_isochrone']].reset_index(drop=True)
    df_temp['distance_km'] = None
    return _agglos(df_temp, n, mode="isochrone")

# === INTERFACE STREAMLIT ===

df_clean = load_data()
st.title("Stanhome Regional Explorer")
st.sidebar.title("Paramètres de la recherche")
adresse = st.sidebar.text_input("Adresse de départ", value="Paris")
mode_recherche = st.sidebar.radio(
    "Mode de recherche",
    options=["Rayon (km)", "Temps de trajet (minutes)"]
)
if mode_recherche == "Rayon (km)":
    rayon = st.sidebar.slider("Rayon de recherche (km)", 10, 400, 200)
else:
    temps_min = st.sidebar.slider("Temps de trajet (minutes)", 5, 120, 60)  # Limite HERE : 4h

min_pop = st.sidebar.number_input("Population minimale", min_value=0, value=10000)
n = st.sidebar.number_input("Nombre d'agglomérations à afficher", min_value=1, max_value=30, value=10)

if adresse:
    lat, lon, REGION, country = geocode_adresse(adresse)
    if lat is None:
        st.error("Adresse non trouvée ou géocodage indisponible.")
        st.stop()
    if country != "FR":
        st.warning("Attention : l’adresse saisie n’est pas en France. Le radar ne fonctionne que pour la France.")
        st.stop()
    coord_depart = (lat, lon)
    coord_depart_lonlat = (lon, lat)
get_here_isochrone(lat, lon, temps_min, HERE_API_KEY)

    # === TRAITEMENT SELON MODE ===
    if mode_recherche == "Rayon (km)":
        df_all_in_radius = villes_dans_rayon_km(df_clean, coord_depart, rayon)
        df_filtre = gd_villes_dans_rayon_km(df_clean, coord_depart, rayon, min_pop, n)
    else:
        # -- Isochrone HERE --
        polygone_recherche = get_here_isochrone(lat, lon, temps_min, HERE_API_KEY)
        dmax = rayon_max_isochrone(polygone_recherche, coord_depart)
        villes_candidates = villes_dans_rayon_km(df_clean, coord_depart, dmax + 5)
        df_all_in_radius = villes_dans_isochrone(villes_candidates, polygone_recherche)
        df_filtre = gd_villes_dans_isochrone(villes_candidates, min_pop, n, polygone_recherche)

    nombre_total_villes = len(df_all_in_radius)
    population_totale = int(df_all_in_radius['population'].sum())
    population_totale_str = f"{population_totale:,}".replace(",", ".")
    if df_filtre.empty:
        st.warning("Aucune agglomération trouvée dans le périmètre et avec la population minimale sélectionnée.")
        st.stop()
    population_totale_gd_ville = int(df_filtre['Population'].sum())
    population_totale_gd_ville_str = f"{population_totale_gd_ville:,}".replace(",", ".")
    df_stats = pd.DataFrame({
        "Indicateur": ["Nombre total de villes dans le périmètre", "Population totale dans le périmètre", "Population totale des grandes villes"],
        "Valeur": [nombre_total_villes, population_totale_str, population_totale_gd_ville_str]
    })

    # === Affichage de la carte Folium ===
    m = folium.Map(location=coord_depart, zoom_start=8)
    if mode_recherche == "Rayon (km)":
        folium.Circle(
            radius=rayon * 1000,
            location=coord_depart,
            color="purple",
            fill=True,
            fill_opacity=0.1,
            popup=f"{rayon} km autour de {adresse}"
        ).add_to(m)
    else:
        geojson = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[list(coord) for coord in polygone_recherche.exterior.coords]]
                }
            }]
        }
        folium.GeoJson(
            geojson,
            style_function=lambda feature: {
                'fillColor': 'purple',
                'color': 'purple',
                'weight': 2,
                'fillOpacity': 0.15,
            },
            name="Isochrone"
        ).add_to(m)

    # Heatmap (optionnelle)
    heatmap_pop = st.sidebar.checkbox("Afficher le mode heatmap")
    heat_data_pop = [
        [row['Latitude'], row['Longitude'], row['Population']]
        for _, row in df_filtre.iterrows()
        if row['Population'] > 0
    ]
    if heatmap_pop and len(heat_data_pop) >= 2:
        HeatMap(heat_data_pop, min_opacity=0.3, radius=25, blur=15).add_to(m)

    # Affichage région GeoJSON
    url_geojson = "https://france-geojson.gregoiredavid.fr/repo/regions.geojson"
    region_geojson_all = requests.get(url_geojson).json()
    region_feature = None
    for feature in region_geojson_all['features']:
        if feature['properties']['nom'] == REGION:
            region_feature = feature
            break
    if region_feature:
        region_geojson = {"type": "FeatureCollection", "features": [region_feature]}
        folium.GeoJson(
            region_geojson,
            style_function=lambda feature: {
                'fillColor': 'none',
                'color': 'blue',
                'weight': 2,
                'dashArray': '5, 5'
            },
            name="Frontière région",
            highlight_function=lambda x: {'weight': 3, 'color': 'yellow'},
        ).add_to(m)

    # Marker de départ (icône custom)
    big_icon_url = "https://raw.githubusercontent.com/blarflelsouf/SH-CarteFR/master/logopng.png"
    custom_icon = folium.CustomIcon(
        big_icon_url,
        icon_size=(60, 60),
        icon_anchor=(30, 60)
    )
    folium.Marker(
        location=coord_depart,
        popup="Départ",
        icon=custom_icon
    ).add_to(m)

    # Markers villes/agglos principales
    for _, row in df_filtre.iterrows():
        # Distance = km OU "-" si mode isochrone
        if mode_recherche == "Rayon (km)":
            d_affiche = f"{row['Distance (en km)']:.2f}".replace(".", ",") if row['Distance (en km)'] is not None else "-"
        else:
            d_affiche = "-"
        couleur = couleur_par_distance(row['Distance (en km)'], mode="km" if mode_recherche == "Rayon (km)" else "min")
        folium.Marker(
            location=[row['Latitude'], row['Longitude']],
            popup=f"{row['Ville']} ({d_affiche} {'km' if mode_recherche == 'Rayon (km)' else 'min'}, {row['Population']} hab)",
            icon=folium.Icon(color=couleur)
        ).add_to(m)

    st.markdown(f"### Carte des {len(df_filtre)} plus grandes agglomérations autour de {adresse}")
    st_data = st_folium(m, width=900, height=600)

    # Tableau HTML coloré
    df_display = df_filtre[['Ville', 'Distance (en km)', 'Population', 'Région']].copy()
    if mode_recherche == "Rayon (km)":
        df_display["Distance (en km)"] = df_display["Distance (en km)"].apply(lambda x: f"{x:.2f}".replace(".", ",") if x is not None else "-")
        titre_dist = "Distance (en km)"
    else:
        # On affiche un "-" car pas de temps exact par ville (isochrone) — pour le temps par ville, il faudrait batcher des requests (optionnel)
        df_display["Distance (en km)"] = "-"
        titre_dist = "Distance (en min)"
    df_display["Population"] = df_display["Population"].apply(lambda x: f"{int(x):,}".replace(",", "."))

    rows = []
    for _, row in df_display.iterrows():
        try:
            couleur = couleur_par_distance(row["Distance (en km)"], mode="km" if mode_recherche == "Rayon (km)" else "min")
        except:
            couleur = "gray"
        ville_html = f'<span style="color:{couleur}; font-weight:bold">{row["Ville"]}</span>'
        rows.append(f"<tr><td>{ville_html}</td><td>{row['Distance (en km)']} {'km' if mode_recherche == 'Rayon (km)' else 'min'}</td><td>{row['Population']}</td><td>{row['Région']}</td></tr>")
    table_html = f"""
    <table style="width:100%; border-collapse:collapse; font-size: 1.08em;">
    <thead>
    <tr style="background-color:#223366; color:white;">
        <th style="padding:8px; border:1px solid #AAA;">Ville</th>
        <th style="padding:8px; border:1px solid #AAA;">{titre_dist}</th>
        <th style="padding:8px; border:1px solid #AAA;">Population</th>
        <th style="padding:8px; border:1px solid #AAA;">Région</th>
    </tr>
    </thead>
    <tbody>
    {''.join(rows)}
    </tbody>
    </table>
    """
    st.markdown(table_html, unsafe_allow_html=True)

    # Tableau synthèse sous le HTML
    st.markdown("#### Synthèse dans le périmètre (toutes villes confondues)")
    st.dataframe(df_stats, hide_index=True)


    # Légende adaptée
    st.sidebar.markdown("---")
    st.sidebar.markdown("#### Légende des marqueurs")
    if mode_recherche == "Rayon (km)":
        st.sidebar.markdown("""
        <span style='display:inline-block; width:16px; height:16px; background-color:purple; border-radius:50%; margin-right:8px;'></span> <b>Départ</b><br>
        <span style='display:inline-block; width:16px; height:16px; background-color:green; border-radius:50%; margin-right:8px;'></span> <b>&lt; 50 km</b><br>
        <span style='display:inline-block; width:16px; height:16px; background-color:orange; border-radius:50%; margin-right:8px;'></span> <b>&lt; 120 km</b><br>
        <span style='display:inline-block; width:16px; height:16px; background-color:red; border-radius:50%; margin-right:8px;'></span> <b>&gt; 120 km</b>
    """, unsafe_allow_html=True)
    else:
        st.sidebar.markdown("""
        <span style='display:inline-block; width:16px; height:16px; background-color:purple; border-radius:50%; margin-right:8px;'></span> <b>Départ</b><br>
        <span style='display:inline-block; width:16px; height:16px; background-color:green; border-radius:50%; margin-right:8px;'></span> <b>&lt; 30 mins</b><br>
        <span style='display:inline-block; width:16px; height:16px; background-color:orange; border-radius:50%; margin-right:8px;'></span> <b>&lt; 80 mins</b><br>
        <span style='display:inline-block; width:16px; height:16px; background-color:red; border-radius:50%; margin-right:8px;'></span> <b>&gt; 120 mins</b>
    """, unsafe_allow_html=True)
