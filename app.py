import streamlit as st
import pandas as pd
from opencage.geocoder import OpenCageGeocode
from geopy.distance import geodesic
import folium
from streamlit_folium import st_folium
import urllib.request
import os
import openrouteservice
from shapely.geometry import shape, Point

# CONFIGURATION & DATA
OC_API_KEY = st.secrets["OPENCAGE_API_KEY"]
ORS_API_KEY = st.secrets["ORS_API_KEY"]
ors_client = openrouteservice.Client(key=ORS_API_KEY)
PARQUET_FILE = "DS_DISt_TPS.parquet"

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
    df = pd.read_csv(CSV_FICHIER)
    df_clean = df.loc[:, ['nom_standard', 'reg_nom', 'population', 'latitude_mairie', 'longitude_mairie']]
    return df_clean

@st.cache_data
def load_drive_times():
    if os.path.exists(PARQUET_FILE):
        return pd.read_parquet(PARQUET_FILE)
    return None

def couleur_par_distance(distance):
    if distance is None:
        return "gray"
    if distance < 50: return 'green'
    elif distance < 120: return 'orange'
    else: return 'red'

def couleur_par_trajet(temps):
    if temps is None:
        return "gray"
    if temps < 30: return 'green'
    elif temps < 80: return 'orange'
    else: return 'red'

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
            ville_norm = (details.get('city') or details.get('town') or
                          details.get('village') or details.get('municipality'))
            return latitude, longitude, region, country, ville_norm
        else:
            return None, None, None, None, None
    except Exception as e:
        st.error(f"Erreur géocodage OpenCage : {e}")
        st.stop()

@st.cache_data
def villes_dans_rayon_km(df_clean, coord_depart, rayon):
    df_all_in_radius = df_clean.copy()
    df_all_in_radius['distance_km'] = df_all_in_radius.apply(
        lambda row: geodesic(coord_depart, (row['latitude_mairie'], row['longitude_mairie'])).km, axis=1)
    return df_all_in_radius[df_all_in_radius['distance_km'] <= rayon].reset_index(drop=True)

# -------- NOUVEAU: MULTI-VILLES ENTRÉE -------------
def parse_ville_list(adresse_str):
    villes = [a.strip() for a in adresse_str.split(",") if a.strip()]
    return villes

def find_coords_for_villes(villes, df_clean):
    """Retourne une liste de tuples (lat, lon, nom, région, pays, ville_norm)"""
    coords = []
    for ville in villes:
        # On tente d'abord par matching exact CSV (pour éviter le geocoding)
        match = df_clean[df_clean['nom_standard'].str.upper() == ville.upper()]
        if not match.empty:
            row = match.iloc[0]
            coords.append((row['latitude_mairie'], row['longitude_mairie'], row['reg_nom'], 'FR', row['nom_standard']))
        else:
            lat, lon, reg, pays, ville_norm = geocode_adresse(ville)
            if lat is not None:
                coords.append((lat, lon, reg, pays, ville_norm if ville_norm else ville))
    return coords

# =======================
# INTERFACE STREAMLIT
# =======================

df_clean = load_data()
drive_times = load_drive_times()
st.title("Stanhome Regional Explorer")
st.sidebar.title("Paramètres de la recherche")
adresse = st.sidebar.text_input("Adresse de départ (séparer par virgules pour plusieurs villes)", value="Paris")
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

# -- MAIN CODE
if adresse:
    villes_input = parse_ville_list(adresse)
    coords_list = find_coords_for_villes(villes_input, df_clean)
    if not coords_list:
        st.error("Aucune ville de départ valide.")
        st.stop()
    # On peut avoir plusieurs points de départ
    used_parquet = False
    df_final = pd.DataFrame()
    rayon_max = 0

    # 1. On essaye d'utiliser le Parquet si possible pour TOUTES les villes
    if drive_times is not None:
        dfs = []
        rayons = []
        for (lat, lon, reg, pays, ville_norm) in coords_list:
            villes_parquet = drive_times['ville1'].str.upper().unique()
            if ville_norm and ville_norm.upper() in villes_parquet:
                used_parquet = True
                df_rel = drive_times[drive_times['ville1'].str.upper() == ville_norm.upper()]
                df_rel = df_rel.merge(df_clean, left_on='ville2', right_on='nom_standard')
                if mode_recherche == "Rayon (km)":
                    df_rel = df_rel[df_rel['distance_km'] <= rayon]
                    df_rel = df_rel[df_rel['population'] > min_pop]
                    rayons.append(df_rel['distance_km'].max() if not df_rel.empty else 0)
                else:
                    df_rel = df_rel[df_rel['temps_min'] <= temps_min]
                    df_rel = df_rel[df_rel['population'] > min_pop]
                    rayons.append(df_rel.apply(lambda row: geodesic((lat, lon), (row['latitude_mairie'], row['longitude_mairie'])).km, axis=1).max() if not df_rel.empty else 0)
                df_rel = df_rel.sort_values("population", ascending=False).head(n)
                df_rel = df_rel.rename(columns={
                    "ville2": "Ville",
                    "latitude_mairie": "Latitude",
                    "longitude_mairie": "Longitude",
                    "population": "Population",
                    "reg_nom": "Région",
                    "temps_min": "Temps (min)",
                    "distance_km": "Distance (en km)"
                })[['Ville', 'Latitude', 'Longitude', 'Population', 'Région', 'Temps (min)', 'Distance (en km)']]
                dfs.append(df_rel)
        if dfs:
            df_final = pd.concat(dfs, ignore_index=True).drop_duplicates(subset=['Ville'])
            rayon_max = max(rayons) if rayons else rayon
    # 2. Fallback dynamique si aucune donnée Parquet (au moins pour une des villes)
    if df_final.empty or not used_parquet:
        st.warning("Aucun résultat pré-calculé trouvé pour au moins une ville, calcul dynamique en cours (peut être lent).")
        dfs = []
        rayons = []
        for (lat, lon, reg, pays, ville_norm) in coords_list:
            coord_depart = (lat, lon)
            if mode_recherche == "Rayon (km)":
                df_all_in_radius = villes_dans_rayon_km(df_clean, coord_depart, rayon)
                df_rel = df_all_in_radius[df_all_in_radius['population'] > min_pop].copy()
                df_rel['Distance (en km)'] = df_rel.apply(lambda row: geodesic(coord_depart, (row['latitude_mairie'], row['longitude_mairie'])).km, axis=1)
                df_rel = df_rel.sort_values("population", ascending=False).head(n)
                rayons.append(df_rel['Distance (en km)'].max() if not df_rel.empty else 0)
            else:
                # Isochrone ou fallback simple (ici on fait un cercle pour rapidité)
                df_all_in_radius = villes_dans_rayon_km(df_clean, coord_depart, 200)
                df_rel = df_all_in_radius[df_all_in_radius['population'] > min_pop].copy()
                df_rel['Temps (min)'] = np.nan # Placeholder si pas d'appel API
                # Ici, tu peux plugger ton batch ORS si tu veux
                df_rel = df_rel.sort_values("population", ascending=False).head(n)
                rayons.append(df_rel.apply(lambda row: geodesic(coord_depart, (row['latitude_mairie'], row['longitude_mairie'])).km, axis=1).max() if not df_rel.empty else 0)
            dfs.append(df_rel)
        if dfs:
            df_final = pd.concat(dfs, ignore_index=True).drop_duplicates(subset=['Ville'])
            rayon_max = max(rayons) if rayons else rayon

    # --- Affichage de la carte Folium ---
    if not df_final.empty:
        # Calcul centre "moyen" pour la carte
        mean_lat = np.mean([lat for (lat, lon, *_ ) in coords_list])
        mean_lon = np.mean([lon for (lat, lon, *_ ) in coords_list])
        m = folium.Map(location=(mean_lat, mean_lon), zoom_start=7)

        # Cercles autour de chaque ville de départ
        for (lat, lon, reg, pays, ville_norm) in coords_list:
            folium.Circle(
                radius=rayon_max * 1000,
                location=(lat, lon),
                color="purple",
                fill=True,
                fill_opacity=0.10,
                popup=f"{rayon_max:.1f} km autour de {ville_norm}"
            ).add_to(m)
            folium.Marker(
                location=(lat, lon),
                popup=f"Départ: {ville_norm}",
                icon=folium.Icon(color="purple")
            ).add_to(m)

        # Marqueurs principales
        for idx, row in df_final.iterrows():
            couleur = couleur_par_distance(row['Distance (en km)']) if mode_recherche == "Rayon (km)" else couleur_par_trajet(row.get('Temps (min)', None))
            popup_info = (
                f"{row['Ville']} ({row['Distance (en km)']:.1f} km, {row['Population']} hab)" if mode_recherche == "Rayon (km)"
                else f"{row['Ville']} ({row.get('Temps (min)', 0):.0f} min, {row['Population']} hab)"
            )
            folium.Marker(
                location=[row['Latitude'], row['Longitude']],
                popup=popup_info,
                icon=folium.Icon(color=couleur)
            ).add_to(m)

        st.markdown(f"### Carte des {len(df_final)} plus grandes agglomérations autour de {', '.join(villes_input)}")
        st_folium(m, width=900, height=600)

        # --- Tableau HTML coloré ---
        if mode_recherche == "Rayon (km)":
            df_display = df_final[['Ville', 'Distance (en km)', 'Population', 'Région']].copy()
            df_display["Distance (en km)"] = df_display["Distance (en km)"].apply(lambda x: f"{x:.2f}".replace(".", ",") if x is not None else "-")
        else:
            df_display = df_final[['Ville', 'Temps (min)', 'Population', 'Région']].copy()
            df_display["Temps (min)"] = df_display["Temps (min)"].apply(lambda x: f"{x:.0f}" if pd.notnull(x) else "-")
        df_display["Population"] = df_display["Population"].apply(lambda x: f"{int(x):,}".replace(",", "."))

        rows = []
        for _, row in df_display.iterrows():
            couleur = couleur_par_distance(float(row["Distance (en km)"].replace(",", "."))) if mode_recherche == "Rayon (km)" else couleur_par_trajet(float(row["Temps (min)"])) if row["Temps (min)"] != "-" else "gray"
            ville_html = f'<span style="color:{couleur}; font-weight:bold">{row["Ville"]}</span>'
            if mode_recherche == "Rayon (km)":
                rows.append(f"<tr><td>{ville_html}</td><td>{row['Distance (en km)']} km</td><td>{row['Population']}</td><td>{row['Région']}</td></tr>")
            else:
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

        # --- Synthèse sur l'union des villes trouvées (et pas tout le CSV, cohérent pour plusieurs points) ---
        st.markdown("#### Synthèse (périmètre cumulé des villes affichées)")
        nombre_total_villes = len(df_final)
        population_totale = int(df_final['Population'].replace({np.nan: 0}).sum())
        population_totale_str = f"{population_totale:,}".replace(",", ".")
        st.dataframe(pd.DataFrame({
            "Indicateur": ["Nombre de villes affichées", "Population totale des grandes villes"],
            "Valeur": [nombre_total_villes, population_totale_str]
        }), hide_index=True)

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
