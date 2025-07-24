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
import openrouteservice
from shapely.geometry import shape, Point

# =======================
# CONFIGURATION & DATA
# =======================

OC_API_KEY = st.secrets["OPENCAGE_API_KEY"]
ORS_API_KEY = st.secrets["ORS_API_KEY"]
ors_client = openrouteservice.Client(key=ORS_API_KEY)

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

def couleur_par_distance(distance):
    try:
        if distance is None:
            return "gray"
        if distance < 50:
            return 'green'
        elif distance < 120:
            return 'orange'
        else:
            return 'red'
    except:
        return "gray"

def couleur_par_trajet(temps):
    try:
        if temps is None:
            return "gray"
        if temps < 30:
            return 'green'
        elif temps < 80:
            return 'orange'
        else:
            return 'red'
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
    points = list(polygone_iso.exterior.coords)
    max_dist = 0
    for lon, lat in points:
        d = geodesic(centre, (lat, lon)).km
        if d > max_dist:
            max_dist = d
    return max_dist

@st.cache_data
def villes_dans_rayon_km(df_clean, coord_depart, rayon):
    df_all_in_radius = df_clean.copy()
    df_all_in_radius['distance_km'] = df_all_in_radius.apply(
        lambda row: geodesic(coord_depart, (row['latitude_mairie'], row['longitude_mairie'])).km, axis=1)
    return df_all_in_radius[df_all_in_radius['distance_km'] <= rayon].reset_index(drop=True)

def villes_dans_isochrone(df_candidates, polygone_isochrone):
    df_all_in_iso = df_candidates.copy()
    df_all_in_iso['in_isochrone'] = df_all_in_iso.apply(
        lambda row: polygone_isochrone.contains(Point(row['longitude_mairie'], row['latitude_mairie'])),
        axis=1
    )
    df_all_in_iso = df_all_in_iso[df_all_in_iso['in_isochrone']].reset_index(drop=True)
    df_all_in_iso['distance_km'] = None
    return df_all_in_iso

@st.cache_data
def gd_villes_dans_rayon_km(df_clean, coord_depart, rayon, min_pop, n):
    df_temp = df_clean[df_clean['population'] > min_pop].copy()
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

def get_travel_time_batch(ors_client, start_coord, villes_df):
    times = []
    for _, row in villes_df.iterrows():
        try:
            route = ors_client.directions(
                coordinates=[[start_coord[1], start_coord[0]], [row['Longitude'], row['Latitude']]],
                profile='driving-car',
                format='geojson'
            )
            duration = route['features'][0]['properties']['summary']['duration']
            times.append(duration / 60)  # en minutes
        except Exception as e:
            times.append(None)
    return times

# =======================
# INTERFACE STREAMLIT
# =======================

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
    temps_min = st.sidebar.slider("Temps de trajet (minutes)", 5, 60, 30)
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

    polygone_recherche = None
    if mode_recherche == "Rayon (km)":
        df_all_in_radius = villes_dans_rayon_km(df_clean, coord_depart, rayon)
        print(df_all_in_radius.columns())
        df_filtre = gd_villes_dans_rayon_km(df_all_in_radius, coord_depart, rayon, min_pop, n)
    else:
        iso = ors_client.isochrones(
            locations=[coord_depart_lonlat],
            profile='driving-car',
            range=[temps_min * 60],
            intervals=[temps_min * 60],
            units='m'
        )
        polygone_recherche = shape(iso['features'][0]['geometry'])
        dmax = rayon_max_isochrone(polygone_recherche, coord_depart)
        villes_candidates = villes_dans_rayon_km(df_clean, coord_depart, dmax + 5)
        df_all_in_radius = villes_dans_isochrone(villes_candidates, polygone_recherche)
        df_filtre = gd_villes_dans_isochrone(villes_candidates, min_pop, n, polygone_recherche)
        # Calculer temps de trajet réel pour les N agglos trouvées
        df_filtre['Temps (min)'] = get_travel_time_batch(ors_client, coord_depart, df_filtre)

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

    # --- Affichage de la carte Folium ---
    m = folium.Map(location=coord_depart, zoom_start=8)
    # Isochrone (polygone ou cercle)
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
        folium.GeoJson(
            data=polygone_recherche,
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

    # Marqueurs principales
    for idx, row in df_filtre.iterrows():
        if mode_recherche == "Rayon (km)":
            couleur = couleur_par_distance(row['Distance (en km)'])
            popup_info = f"{row['Ville']} ({row['Distance (en km)']} km, {row['Population']} hab)"
        else:
            temps = row['Temps (min)']
            couleur = couleur_par_trajet(temps)
            popup_info = f"{row['Ville']} ({temps:.0f} min, {row['Population']} hab)" if temps is not None else f"{row['Ville']} (- min, {row['Population']} hab)"
        folium.Marker(
            location=[row['Latitude'], row['Longitude']],
            popup=popup_info,
            icon=folium.Icon(color=couleur)
        ).add_to(m)

    st.markdown(f"### Carte des {len(df_filtre)} plus grandes agglomérations autour de {adresse}")
    st_data = st_folium(m, width=900, height=600)

    # --- Tableau HTML coloré ---
    if mode_recherche == "Rayon (km)":
        df_display = df_filtre[['Ville', 'Distance (en km)', 'Population', 'Région']].copy()
        df_display["Distance (en km)"] = df_display["Distance (en km)"].apply(lambda x: f"{x:.2f}".replace(".", ",") if x is not None else "-")
        df_display["Population"] = df_display["Population"].apply(lambda x: f"{int(x):,}".replace(",", "."))
    else:
        df_display = df_filtre[['Ville', 'Temps (min)', 'Population', 'Région']].copy()
        df_display["Temps (min)"] = df_display["Temps (min)"].apply(lambda x: f"{x:.0f}" if x is not None else "-")
        df_display["Population"] = df_display["Population"].apply(lambda x: f"{int(x):,}".replace(",", "."))

    rows = []
    for _, row in df_display.iterrows():
        if mode_recherche == "Rayon (km)":
            try:
                couleur = couleur_par_distance(float(row["Distance (en km)"].replace(",", "."))) if row["Distance (en km)"] != "-" else "gray"
            except:
                couleur = "gray"
            ville_html = f'<span style="color:{couleur}; font-weight:bold">{row["Ville"]}</span>'
            rows.append(f"<tr><td>{ville_html}</td><td>{row['Distance (en km)']} km</td><td>{row['Population']}</td><td>{row['Région']}</td></tr>")
        else:
            try:
                couleur = couleur_par_trajet(float(row["Temps (min)"])) if row["Temps (min)"] != "-" else "gray"
            except:
                couleur = "gray"
            ville_html = f'<span style="color:{couleur}; font-weight:bold">{row["Ville"]}</span>'
            rows.append(f"<tr><td>{ville_html}</td><td>{row['Temps (min)']} min</td><td>{row['Population']}</td><td>{row['Région']}</td></tr>")
    table_html = f"""
    <table style="width:100%; border-collapse:collapse; font-size: 1.08em;">
    <thead>
    <tr style="background-color:#223366; color:white;">
        <th style="padding:8px; border:1px solid #AAA;">Ville</th>
        <th style="padding:8px; border:1px solid #AAA;">{"Distance (en km)" if mode_recherche == "Rayon (km)" else "Temps (en min)"}</th>
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

    # Tableau synthèse
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
