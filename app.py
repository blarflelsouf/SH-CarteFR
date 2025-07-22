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


OC_API_KEY = st.secrets["OPENCAGE_API_KEY"]

# Chargement CSV
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

df_clean = load_data()

# Sidebar / Entrées utilisateur
st.title("Stanhome Regional Explorer")

st.sidebar.title("Paramètres de la recherche")
adresse = st.sidebar.text_input("Adresse de départ", value="Paris")
rayon = st.sidebar.slider("Rayon de recherche (km)", 10, 400, 200)
min_pop = st.sidebar.number_input("Population minimale", min_value=0, value=10000)
n = st.sidebar.number_input("Nombre de villes à afficher", min_value=1, max_value=30, value=10)

# --- GEOCODAGE AVEC OPENCAGE ---
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
            return None, None, None
    except Exception as e:
        st.error(f"Erreur géocodage OpenCage : {e}")
        st.stop()

# --- Recherche uniquement si l'adresse est renseignée
if adresse:
    lat, lon, REGION, country = geocode_adresse(adresse)
    if lat is None:
        st.error("Adresse non trouvée ou géocodage indisponible.")
        st.stop()
    
    if country != "FR":
        st.warning("Attention : l’adresse saisie n’est pas en France. Le radar ne fonctionne que pour la France.")
        st.stop()

    # 
    coord_depart = (lat, lon)

    
    # Calcul pop et nbr de villes pour chaque ville dans le rayon
    @st.cache_data
    def villes_dans_rayon(df_clean, coord_depart, rayon):
        df_all_in_radius = df_clean.copy()
        df_all_in_radius['distance_km'] = df_all_in_radius.apply(
            lambda row: geodesic(coord_depart, (row['latitude_mairie'], row['longitude_mairie'])).km, axis=1)
        df_all_in_radius = df_all_in_radius[df_all_in_radius['distance_km'] <= rayon]
        return df_all_in_radius

    df_all_in_radius = villes_dans_rayon(df_clean, coord_depart, rayon)
    nombre_total_villes = len(df_all_in_radius)
    population_totale = int(df_all_in_radius['population'].sum())
    population_totale_str = f"{population_totale:,}".replace(",", ".")

    

    
    # Calcul distance pour chaque grande ville
    @st.cache_data
    def gd_villes_dans_rayon(df_clean, coord_depart, rayon):
        df_temp = df_clean[df_clean['population'] > min_pop].copy()
        df_temp['distance_km'] = df_temp.apply(
            lambda row: geodesic(coord_depart, (row['latitude_mairie'], row['longitude_mairie'])).km, axis=1)
        df_filtre = df_temp[df_temp['distance_km'] <= rayon].sort_values('population', ascending=False).head(n)
        return df_filtre
        
    df_filtre = gd_villes_dans_rayon(df_clean, coord_depart, rayon

    population_totale_gd_ville = int(df_filtre['population'].sum())
    population_totale_gd_ville_str = f"{population_totale:,}".replace(",", ".")                            
                                     
    df_stats = pd.DataFrame({
        "Indicateur": ["Nombre total de villes dans le rayon", "Population totale dans le rayon", "Population totale des grandes villes"],
        "Valeur": [nombre_total_villes, population_totale_str, population_totale_gd_ville_str]
    })

    
    # Fonction couleur distance
    def couleur_par_distance(distance):
        if distance < 50:
            return 'green'
        elif distance < 120:
            return 'orange'
        else:
            return 'red'

    # Carte folium
    m = folium.Map(location=coord_depart, zoom_start=8)
    
    #Génération heatmap
    heat_data_pop = [
        [row['latitude_mairie'], row['longitude_mairie'], row['population']]
        for _, row in df_filtre.iterrows()
    ]

    heatmap_pop = st.sidebar.checkbox(
        "Afficher le mode heatmap"
    )
    if heatmap_pop:
        HeatMap(heat_data_pop, min_opacity=0.3, radius=25, blur=15, max_zoom=1).add_to(m)

    
    # Génération cercle
    isochrone_mode = st.sidebar.checkbox("Afficher le mode isochrone (rayon)")
    if isochrone_mode:
        folium.Circle(
            radius=rayon * 1000,  # rayon en mètres
            location=coord_depart,
            color="purple",
            fill=True,
            fill_opacity=0.1,
            popup=f"{rayon} km autour de {adresse}"
        ).add_to(m)

    
    # Afficher la région sur la carte
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

    # Marker de départ
    big_icon_url = "https://raw.githubusercontent.com/blarflelsouf/SH-CarteFR/refs/heads/master/logopng.png"
    custom_icon = folium.CustomIcon(
        big_icon_url,
        icon_size=(60, 60),  # Largeur, hauteur en pixels
        icon_anchor=(30, 60)  # Position de la pointe (centre bas ici)
    )
    
    folium.Marker(
        location=coord_depart,
        popup="Départ",
        icon=custom_icon
    ).add_to(m)

    # Markers villes
    for _, row in df_filtre.iterrows():
        couleur = couleur_par_distance(row['distance_km'])
        folium.Marker(
            location=[row['latitude_mairie'], row['longitude_mairie']],
            popup=f"{row['nom_standard']} ({int(row['distance_km'])} km, {row['population']} hab)",
            icon=folium.Icon(color=couleur)
        ).add_to(m)

    st.markdown(f"### Carte des {len(df_filtre)} plus grandes villes autour de {adresse}")
    st_data = st_folium(m, width=900, height=600)

    

    # tableau des résultats sous la carte
    df_display = df_filtre[['nom_standard', 'distance_km', 'population', 'reg_nom']].copy()
    df_display.columns = ["Ville", "Distance (en km)", "Population", "Région"]
    df_display["Distance (en km)"] = df_display["Distance (en km)"].apply(lambda x: f"{x:.2f}".replace(".", ","))
    df_display["Population"] = df_display["Population"].apply(lambda x: f"{int(x):,}".replace(",", "."))


    st.markdown("#### Détail des villes affichées")
    # st.dataframe(df_display.reset_index(drop=True))

    def couleur_par_distance(distance_str):
    # distance_str au format "12,42"
        try:
            distance = float(distance_str.replace(",", "."))
        except:
            distance = 0
        if distance < 50:
            return 'green'
        elif distance < 120:
            return 'orange'
        else:
            return 'red'
    
    # Création du tableau HTML coloré
    rows = []
    for _, row in df_display.iterrows():
        couleur = couleur_par_distance(row["Distance (en km)"])
        ville_html = f'<span style="color:{couleur}; font-weight:bold">{row["Ville"]}</span>'
        rows.append(f"<tr><td>{ville_html}</td><td>{row['Distance (en km)']} km</td><td>{row['Population']}</td><td>{row['Région']}</td></tr>")
    
    # Tableau HTML dynamique et coloré
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

    st.markdown("#### Synthèse dans le rayon (toutes villes confondues)")
    st.dataframe(df_stats, hide_index=True)
