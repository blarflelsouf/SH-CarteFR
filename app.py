import streamlit as st
import pandas as pd
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
import folium
from streamlit_folium import st_folium
import requests
import urllib.request
import os
from geopy.exc import GeocoderUnavailable

# Chargement CSV une fois pour toutes
def telecharger_csv_si_absent(fichier, url):
    if not os.path.exists(fichier):
        with st.spinner(f"Téléchargement du fichier CSV depuis {url} ..."):
            urllib.request.urlretrieve(url, fichier)
        st.success("Téléchargement terminé.")

# Utilisation :
CSV_URL = "https://www.data.gouv.fr/api/1/datasets/r/f5df602b-3800-44d7-b2df-fa40a0350325"
CSV_FICHIER = "communes-france-2025.csv"
telecharger_csv_si_absent(CSV_FICHIER, CSV_URL)

@st.cache_data
def load_data():
    df = pd.read_csv('communes-france-2025.csv')
    df_clean = df.loc[:, ['nom_standard', 'reg_nom', 'population', 'densite', 'latitude_mairie', 'longitude_mairie', 'grille_densite']]
    return df_clean

df_clean = load_data()

# Sidebar / Entrées utilisateur
st.sidebar.title("Paramètres de la recherche")
adresse = st.sidebar.text_input("Adresse de départ", value="Paris")
rayon = st.sidebar.slider("Rayon de recherche (km)", 10, 400, 200)
min_pop = st.sidebar.number_input("Population minimale", min_value=0, value=10000)
n = st.sidebar.number_input("Nombre de villes à afficher", min_value=1, max_value=30, value=10)

#
try:
    location = geolocator.geocode(adresse, addressdetails=True, timeout=10)
except GeocoderUnavailable:
    st.error("Le service de géocodage est temporairement indisponible. Réessayez dans quelques minutes.")
    st.stop()
except Exception as e:
    st.error(f"Erreur inattendue lors du géocodage : {e}")
    st.stop()


# Recherche uniquement si l'adresse est renseignée
if adresse:
    # Géocodage
    geolocator = Nominatim(user_agent="carte_distance")
    try:
        location = geolocator.geocode(adresse, addressdetails=True)
    except GeocoderUnavailable:
        st.error("Le service de géocodage est temporairement indisponible. Réessayez dans quelques minutes.")
        st.stop()
        
    except Exception as e:
        st.error(f"Erreur inattendue lors du géocodage : {e}")
        st.stop()
        
    if location is None:
        st.error("Adresse non trouvée")
    else:
        coord_depart = (location.latitude, location.longitude)
        details = location.raw['address']
        REGION = details.get('state')

        # Calcul distance pour chaque ville
        df_temp = df_clean[df_clean['population'] > min_pop].copy()
        df_temp['distance_km'] = df_temp.apply(
            lambda row: geodesic(coord_depart, (row['latitude_mairie'], row['longitude_mairie'])).km, axis=1)
        df_filtre = df_temp[df_temp['distance_km'] <= rayon].sort_values('population', ascending=False).head(n)

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
        folium.Marker(
            location=coord_depart,
            popup="Départ",
            icon=folium.Icon(color='blue')
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

        # Option : tableau des résultats sous la carte
        st.markdown("#### Détail des villes affichées")
        st.dataframe(df_filtre[['nom_standard', 'distance_km', 'population', 'reg_nom']].reset_index(drop=True))
