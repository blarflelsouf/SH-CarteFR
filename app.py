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

# =======================
# CONFIGURATION & DATA
# =======================

# Récupération clé API OpenCage (gérée dans les secrets Streamlit Cloud)
OC_API_KEY = st.secrets["OPENCAGE_API_KEY"]

# 1. Téléchargement du fichier CSV communes (si absent en local)
def telecharger_csv_si_absent(fichier, url):
    if not os.path.exists(fichier):
        with st.spinner(f"Téléchargement du fichier CSV depuis {url} ..."):
            urllib.request.urlretrieve(url, fichier)
        st.success("Téléchargement terminé.")

CSV_URL = "https://www.data.gouv.fr/api/1/datasets/r/f5df602b-3800-44d7-b2df-fa40a0350325"
CSV_FICHIER = "communes-france-2025.csv"
telecharger_csv_si_absent(CSV_FICHIER, CSV_URL)

# 2. Chargement du dataframe propre (caché par Streamlit)
@st.cache_data
def load_data():
    df = pd.read_csv('communes-france-2025.csv')
    # On garde uniquement les colonnes utiles pour la suite
    df_clean = df.loc[:, ['nom_standard', 'reg_nom', 'population', 'latitude_mairie', 'longitude_mairie']]
    return df_clean

# =======================
# FONCTIONS UTILES
# =======================

# Calcul des villes dans le rayon sans critère de population (totaux synthèse)
@st.cache_data
def villes_dans_rayon(df_clean, coord_depart, rayon):
    df_all_in_radius = df_clean.copy()
    df_all_in_radius['distance_km'] = df_all_in_radius.apply(
        lambda row: geodesic(coord_depart, (row['latitude_mairie'], row['longitude_mairie'])).km, axis=1)
    df_all_in_radius = df_all_in_radius[df_all_in_radius['distance_km'] <= rayon]
    return df_all_in_radius

# Calcul des agglomérations de grandes villes (regroupement spatial <15 km)
@st.cache_data
def gd_villes_dans_rayon(df_clean, coord_depart, rayon, min_pop, n):
    # Filtre population
    df_temp = df_clean[df_clean['population'] > min_pop].copy()
    
    # Calcul de la distance à l'adresse de départ
    df_temp['distance_km'] = df_temp.apply(
        lambda row: geodesic(coord_depart, (row['latitude_mairie'], row['longitude_mairie'])).km, axis=1)
    
    # On garde les villes dans le cercle
    df_temp = df_temp[df_temp['distance_km'] <= rayon].reset_index(drop=True)
    N = len(df_temp)
    if N == 0:
        return pd.DataFrame()
        
    # Algorithme "maison" de clustering spatial (agglomération = groupe de villes à <15 km)
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
    # Pour chaque groupe : nom de la ville principale (plus grande pop), population totale, coordonnées, etc.
    agglo = (
        df_temp.groupby('agglomeration')
        .apply(lambda g: pd.Series({
            'Ville': g.loc[g['population'].idxmax()]['nom_standard'],
            'Latitude': g.loc[g['population'].idxmax()]['latitude_mairie'],
            'Longitude': g.loc[g['population'].idxmax()]['longitude_mairie'],
            'Distance (en km)': g.loc[g['population'].idxmax()]['distance_km'],
            'Population': int(g['population'].sum()),
            'Région': g['reg_nom'].mode()[0]
        }))
        .reset_index(drop=True)
    )
    # On trie par population totale et on ne garde que les n plus grandes agglos
    agglo = agglo.sort_values('Population', ascending=False).head(n).reset_index(drop=True)
    return agglo

# Fonction couleur selon la distance (pour la table & les markers)
def couleur_par_distance(distance):
    if distance < 50:
        return 'green'
    elif distance < 120:
        return 'orange'
    else:
        return 'red'

# Géocodage avec OpenCage (retourne coordonnées, région et pays)
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

# =======================
# INTERFACE STREAMLIT
# =======================

df_clean = load_data()
st.title("Stanhome Regional Explorer")
st.sidebar.title("Paramètres de la recherche")
adresse = st.sidebar.text_input("Adresse de départ", value="Paris")
rayon = st.sidebar.slider("Rayon de recherche (km)", 10, 400, 200)
min_pop = st.sidebar.number_input("Population minimale", min_value=0, value=10000)
n = st.sidebar.number_input("Nombre d'agglomérations à afficher", min_value=1, max_value=30, value=10)



# L'utilisateur lance la recherche seulement si une adresse est fournie
if adresse:
    # Géocodage et vérification France
    lat, lon, REGION, country = geocode_adresse(adresse)
    if lat is None:
        st.error("Adresse non trouvée ou géocodage indisponible.")
        st.stop()
    if country != "FR":
        st.warning("Attention : l’adresse saisie n’est pas en France. Le radar ne fonctionne que pour la France.")
        st.stop()
    coord_depart = (lat, lon)
    
    # Calcul des totaux dans le rayon
    df_all_in_radius = villes_dans_rayon(df_clean, coord_depart, rayon)
    nombre_total_villes = len(df_all_in_radius)
    population_totale = int(df_all_in_radius['population'].sum())
    population_totale_str = f"{population_totale:,}".replace(",", ".")
    
    # Calcul grandes villes/agglos (filtre pop, groupement spatial)
    df_filtre = gd_villes_dans_rayon(df_clean, coord_depart, rayon, min_pop, n)
    if df_filtre.empty:
        st.warning("Aucune agglomération trouvée dans le rayon et avec la population minimale sélectionnée.")
        st.stop()
    population_totale_gd_ville = int(df_filtre['Population'].sum())
    population_totale_gd_ville_str = f"{population_totale_gd_ville:,}".replace(",", ".")
    df_stats = pd.DataFrame({
        "Indicateur": ["Nombre total de villes dans le rayon", "Population totale dans le rayon", "Population totale des grandes villes"],
        "Valeur": [nombre_total_villes, population_totale_str, population_totale_gd_ville_str]
    })
    
    # Affichage de la carte Folium
    m = folium.Map(location=coord_depart, zoom_start=8)
    
    # Heatmap (optionnelle)
    heat_data_pop = [
        [row['Latitude'], row['Longitude'], row['Population']]
        for _, row in df_filtre.iterrows()
        if row['Population'] > 0
    ]
    heatmap_pop = st.sidebar.checkbox("Afficher le mode heatmap")
    if heatmap_pop and len(heat_data_pop) >= 2:
        HeatMap(heat_data_pop, min_opacity=0.3, radius=25, blur=15).add_to(m)
        
    # Isochrone visuel
    isochrone_mode = st.sidebar.checkbox("Afficher le mode isochrone (rayon)")
    if isochrone_mode:
        folium.Circle(
            radius=rayon * 1000,
            location=coord_depart,
            color="purple",
            fill=True,
            fill_opacity=0.1,
            popup=f"{rayon} km autour de {adresse}"
        ).add_to(m)
        
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
        couleur = couleur_par_distance(row['Distance (en km)'])
        folium.Marker(
            location=[row['Latitude'], row['Longitude']],
            popup=f"{row['Ville']} ({row['Distance (en km)']:.0f} km, {row['Population']} hab)",
            icon=folium.Icon(color=couleur)
        ).add_to(m)
    st.markdown(f"### Carte des {len(df_filtre)} plus grandes agglomérations autour de {adresse}")
    st_data = st_folium(m, width=900, height=600)
    
    # Préparation du tableau HTML coloré
    df_display = df_filtre[['Ville', 'Distance (en km)', 'Population', 'Région']].copy()
    df_display["Distance (en km)"] = df_display["Distance (en km)"].apply(lambda x: f"{x:.2f}".replace(".", ","))
    df_display["Population"] = df_display["Population"].apply(lambda x: f"{int(x):,}".replace(",", "."))
    
    # Génération du HTML avec couleurs sur Ville
    rows = []
    for _, row in df_display.iterrows():
        couleur = couleur_par_distance(float(row["Distance (en km)"].replace(",", ".")))
        ville_html = f'<span style="color:{couleur}; font-weight:bold">{row["Ville"]}</span>'
        rows.append(f"<tr><td>{ville_html}</td><td>{row['Distance (en km)']} km</td><td>{row['Population']}</td><td>{row['Région']}</td></tr>")
    table_html = f"""
    <table style="width:100%; border-collapse:collapse; font-size: 1.08em;">
    <thead>
    <tr style="background-color:#223366; color:white;">
        <th style="padding:8px; border:1px solid #AAA;">Ville</th>
        <th style="padding:8px; border:1px solid #AAA;">Distance (en km)</th>
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
    st.markdown("#### Synthèse dans le rayon (toutes villes confondues)")
    st.dataframe(df_stats, hide_index=True)

    # Légende des markers
    st.sidebar.markdown("---")
    st.sidebar.markdown("#### Légende des marqueurs")
    st.sidebar.markdown("""
    <span style='display:inline-block; width:16px; height:16px; background-color:purple; border-radius:50%; margin-right:8px;'></span> <b>Départ (violet)</b><br>
    <span style='display:inline-block; width:16px; height:16px; background-color:green; border-radius:50%; margin-right:8px;'></span> <b>&lt; 50 km</b><br>
    <span style='display:inline-block; width:16px; height:16px; background-color:orange; border-radius:50%; margin-right:8px;'></span> <b>&lt; 120 km</b><br>
    <span style='display:inline-block; width:16px; height:16px; background-color:red; border-radius:50%; margin-right:8px;'></span> <b>&gt; 120 km</b>
    """, unsafe_allow_html=True)
