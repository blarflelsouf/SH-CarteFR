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

# ========== CONFIGURATION & DATA ==========

OC_API_KEY = st.secrets["OPENCAGE_API_KEY"]
PARQUET_FILE = "DS_DIST_TPS.parquet"

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

@st.cache_data
def load_drive_times():
    if os.path.exists(PARQUET_FILE):
        st.write("Chargement du fichier en cours")
        return pd.read_parquet(PARQUET_FILE)
    return None

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

def geocode_ville_nom(ville, api_key=OC_API_KEY):
    geocoder = OpenCageGeocode(api_key)
    try:
        results = geocoder.geocode(ville, language='fr', no_annotations=1)
        if results and len(results) > 0:
            latitude = results[0]['geometry']['lat']
            longitude = results[0]['geometry']['lng']
            region = results[0]['components'].get('state', '')
            country = results[0]['components'].get('country_code', '').upper()
            nom_norm = results[0]['components'].get('city') or results[0]['components'].get('town') or results[0]['components'].get('village') or results[0]['components'].get('municipality') or ville
            return latitude, longitude, region, country, nom_norm
        else:
            return None, None, None, None, None
    except Exception as e:
        return None, None, None, None, None

@st.cache_data
def villes_dans_rayon_km(df_clean, coord_depart, rayon):
    df_all_in_radius = df_clean.copy()
    df_all_in_radius['distance_km'] = df_all_in_radius.apply(
        lambda row: geodesic(coord_depart, (row['latitude_mairie'], row['longitude_mairie'])).km, axis=1)
    return df_all_in_radius[df_all_in_radius['distance_km'] <= rayon].reset_index(drop=True)

# ========== INTERFACE STREAMLIT ==========

df_clean = load_data()
drive_times = load_drive_times()
st.title("Stanhome Regional Explorer")
st.sidebar.title("Paramètres de la recherche")
adresse = st.sidebar.text_input("Adresse de départ (séparées par virgule)", value="Paris")

mode_recherche = st.sidebar.radio(
    "Mode de recherche",
    options=["Rayon (km)", "Temps de trajet (minutes)"]
)
if mode_recherche == "Rayon (km)":
    rayon = st.sidebar.slider("Rayon de recherche (km)", 10, 400, 200)
else:
    temps_min = st.sidebar.slider("Temps de trajet (minutes)", 5, 120, 60)
min_pop = st.sidebar.number_input("Population minimale", min_value=0, value=10000)
n = st.sidebar.number_input("Nombre d'agglomérations à afficher", min_value=1, max_value=30, value=10)

# ---------- MULTI-VILLES GESTION ----------

if adresse:
    villes_saisies = [v.strip() for v in adresse.split(',')]
    villes_valides = []
    coords_list = []
    noms_normalises = []
    villes_manquantes = []
    regions_list = []

    # Géocodage et vérification des villes saisies
    for ville in villes_saisies:
        lat, lon, region, country, nom_norm = geocode_ville_nom(ville)
        if (lat is not None) and (country == "FR"):
            villes_valides.append(ville)
            coords_list.append((lat, lon))
            noms_normalises.append(nom_norm)
            regions_list.append(region)
        else:
            villes_manquantes.append(ville)

    if villes_manquantes:
        st.warning(f"Certaines villes n'ont pas été trouvées ou ne sont pas en France : {', '.join(villes_manquantes)}.")

    if not villes_valides:
        st.stop("Aucune ville valide en entrée, vérifiez votre saisie.")

    # ----- OPTIMISATION : CHARGEMENT PARQUET (pour toutes les villes saisies) -----
    dfs = []
    rayons = []
    used_parquet = True

    for i, ville_norm in enumerate(noms_normalises):
        if drive_times is not None and ville_norm:
            villes_parquet = drive_times['ville1'].str.upper().unique()
            if ville_norm.upper() in villes_parquet:
                df_rel = drive_times[drive_times['ville1'].str.upper() == ville_norm.upper()]
                df_rel = df_rel.merge(df_clean, left_on='ville2', right_on='nom_standard')
                if mode_recherche == "Rayon (km)":
                    df_rel = df_rel[df_rel['distance_km'] <= rayon]
                    df_rel = df_rel[df_rel['population'] > min_pop]
                    df_rel = df_rel.sort_values("population", ascending=False).head(n)
                    if not df_rel.empty:
                        rayon_local = df_rel['distance_km'].max()
                        rayons.append(rayon_local)
                else:
                    df_rel = df_rel[df_rel['temps_min'] <= temps_min]
                    df_rel = df_rel[df_rel['population'] > min_pop]
                    df_rel = df_rel.sort_values("population", ascending=False).head(n)
                    if not df_rel.empty:
                        rayon_local = df_rel.apply(
                            lambda row: geodesic(coords_list[i], (row['latitude_mairie'], row['longitude_mairie'])).km, axis=1).max()
                        rayons.append(rayon_local)
                df_rel = df_rel.rename(columns={
                    "ville2": "Ville",
                    "latitude_mairie": "Latitude",
                    "longitude_mairie": "Longitude",
                    "population": "Population",
                    "reg_nom": "Région",
                    "temps_min": "Temps (min)",
                    "distance_km": "Distance (en km)"
                })
                dfs.append(df_rel[['Ville', 'Latitude', 'Longitude', 'Population', 'Région', 'Temps (min)', 'Distance (en km)']].reset_index(drop=True))
            else:
                used_parquet = False
        else:
            used_parquet = False

    # ---------- Fallback calcul dynamique si besoin ----------
    if not dfs or not used_parquet:
        st.warning("Aucun résultat pré-calculé trouvé pour au moins une ville, calcul dynamique en cours (peut être lent).")
        dfs = []
        rayons = []
        for i, ville in enumerate(villes_valides):
            coord_depart = coords_list[i]
            if mode_recherche == "Rayon (km)":
                df_all_in_radius = villes_dans_rayon_km(df_clean, coord_depart, rayon)
                df_rel = df_all_in_radius[df_all_in_radius['population'] > min_pop].copy()
                df_rel['Distance (en km)'] = df_rel['distance_km']
                df_rel['Temps (min)'] = None
                df_rel = df_rel.sort_values("population", ascending=False).head(n)
                rayon_local = df_rel['Distance (en km)'].max() if not df_rel.empty else rayon
                rayons.append(rayon_local)
            else:
                st.stop("Le calcul dynamique du mode 'Temps de trajet' multi-ville n'est pas encore implémenté dans cette version (évitez les quotas d'API).")
            df_rel = df_rel.rename(columns={
                "nom_standard": "Ville",
                "latitude_mairie": "Latitude",
                "longitude_mairie": "Longitude",
                "population": "Population",
                "reg_nom": "Région"
            })
            dfs.append(df_rel[['Ville', 'Latitude', 'Longitude', 'Population', 'Région', 'Temps (min)', 'Distance (en km)']].reset_index(drop=True))

    # ---------- Fusion, nettoyage et suppression doublons ----------
    if dfs:
        df_concat = pd.concat(dfs, ignore_index=True)
        if 'Ville' in df_concat.columns:
            df_final = df_concat.drop_duplicates(subset=['Ville'])
        else:
            st.stop("Erreur : colonne 'Ville' absente.")
        rayon_max = max(rayons) if rayons else (rayon if mode_recherche=="Rayon (km)" else None)
    else:
        st.stop("Aucune donnée trouvée pour les villes saisies.")

    # ---------- Affichage Carte Folium ----------
    m = folium.Map(location=coords_list[0], zoom_start=7)
    # Affiche un cercle autour de chaque ville saisie
    for i, coord in enumerate(coords_list):
        folium.Circle(
            radius=rayons[i] * 1000,
            location=coord,
            color="purple",
            fill=True,
            fill_opacity=0.1,
            popup=f"{rayons[i]:.1f} km autour de {villes_valides[i]}"
        ).add_to(m)

    # Marker des villes de départ
    big_icon_url = "https://raw.githubusercontent.com/blarflelsouf/SH-CarteFR/master/logopng.png"
    custom_icon = folium.CustomIcon(
        big_icon_url, icon_size=(60, 60), icon_anchor=(30, 60)
    )
    for coord, ville in zip(coords_list, villes_valides):
        folium.Marker(
            location=coord,
            popup=f"Départ : {ville}",
            icon=custom_icon
        ).add_to(m)

    # Marqueurs villes sélectionnées
    for idx, row in df_final.iterrows():
        couleur = couleur_par_distance(row['Distance (en km)']) if mode_recherche == "Rayon (km)" else couleur_par_trajet(row['Temps (min)'])
        popup_info = (
            f"{row['Ville']} ({row['Distance (en km)']:.1f} km, {row['Population']} hab)" if mode_recherche == "Rayon (km)"
            else f"{row['Ville']} ({row['Temps (min)']:.0f} min, {row['Population']} hab)"
        )
        folium.Marker(
            location=[row['Latitude'], row['Longitude']],
            popup=popup_info,
            icon=folium.Icon(color=couleur)
        ).add_to(m)

    st.markdown(f"### Carte des {len(df_final)} plus grandes agglomérations autour de {', '.join(villes_valides)}")
    st_data = st_folium(m, width=900, height=600)

    # ---------- Tableau HTML coloré ----------
    if mode_recherche == "Rayon (km)":
        df_display = df_final[['Ville', 'Distance (en km)', 'Population', 'Région']].copy()
        df_display["Distance (en km)"] = df_display["Distance (en km)"].apply(lambda x: f"{x:.2f}".replace(".", ",") if x is not None else "-")
        df_display["Population"] = df_display["Population"].apply(lambda x: f"{int(x):,}".replace(",", "."))
    else:
        df_display = df_final[['Ville', 'Temps (min)', 'Population', 'Région']].copy()
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

    # ---------- Tableau synthèse ----------
    nombre_total_villes = len(df_final)
    population_totale = int(df_final['Population'].sum())
    population_totale_str = f"{population_totale:,}".replace(",", ".")
    st.markdown("#### Synthèse dans le périmètre (par villes affichées)")
    st.dataframe(pd.DataFrame({
        "Indicateur": ["Nombre de villes affichées", "Population totale des grandes villes"],
        "Valeur": [nombre_total_villes, population_totale_str]
    }), hide_index=True)

    # ---------- Légende adaptée ----------
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
