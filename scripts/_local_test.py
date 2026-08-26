"""Test local (non commité) : valide la logique de update_data.py avec des
données synthétiques, sans appeler data.gouv.fr ni l'API BAN."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import update_data as ud

# --- Simule un CSV avec des noms de colonnes plausibles (variante 1) ---
df = pd.DataFrame([
    {
        "Id_offre": "1", "Intitule_du_poste": "Développeur back-end",
        "Nom_employeur": "Ministère du Numérique", "Ville": "Paris",
        "Code_postal_lieu_travail": "75001", "Departement_lieu_travail": "75",
        "Region_lieu_travail": "Ile-de-France", "Domaine_metier": "Numérique",
        "Nature_contrat": "CDD", "Date_publication": "2026-08-01",
        "Date_limite_candidature": "2099-01-01", "Url_offre": "https://example.gouv.fr/1",
    },
    {
        "Id_offre": "2", "Intitule_du_poste": "Infirmier", "Nom_employeur": "CHU Lyon",
        "Ville": "Lyon", "Code_postal_lieu_travail": "69003", "Departement_lieu_travail": "69",
        "Region_lieu_travail": "Auvergne-Rhone-Alpes", "Domaine_metier": "Santé",
        "Nature_contrat": "Titulaire", "Date_publication": "2026-08-10",
        "Date_limite_candidature": "2020-01-01",  # expirée -> doit être filtrée
        "Url_offre": "https://example.gouv.fr/2",
    },
    {
        "Id_offre": "3", "Intitule_du_poste": "Chef de projet SI", "Nom_employeur": "Conseil régional",
        "Ville": "Marseille", "Code_postal_lieu_travail": "13001", "Departement_lieu_travail": "13",
        "Region_lieu_travail": "PACA", "Domaine_metier": "Numérique",
        "Nature_contrat": "CDI", "Date_publication": "2026-08-15",
        "Date_limite_candidature": "2099-01-01", "Url_offre": "https://example.gouv.fr/3",
        "Latitude": "43.2965", "Longitude": "5.3698",
    },
])

mapping = ud.map_columns(df)
assert mapping["title"] == "Intitule_du_poste", mapping
assert mapping["url"] == "Url_offre", mapping
assert mapping["lat"] == "Latitude" and mapping["lon"] == "Longitude", mapping
print("OK map_columns")

records = ud.build_records(df, mapping)
assert len(records) == 3
records = ud.filter_expired(records)
assert len(records) == 2, f"attendu 2 offres non expirées, obtenu {len(records)}"
print("OK filter_expired (offre expirée exclue)")

# monkeypatch le géocodage réseau pour le test
def fake_geocode_ban(city, postal_code):
    fake_coords = {
        "paris": (48.8566, 2.3522),
        "lyon": (45.7640, 4.8357),
    }
    return fake_coords.get((city or "").strip().lower())

ud.geocode_ban = fake_geocode_ban
ud.GEOCODE_CACHE_PATH = Path("/tmp/geocode_cache_test.json")
if ud.GEOCODE_CACHE_PATH.exists():
    ud.GEOCODE_CACHE_PATH.unlink()

enriched = ud.enrich_with_coordinates(records, mapping)
final = ud.finalize_records(enriched)
assert len(final) == 2, final
by_id = {r["id"]: r for r in final}
assert abs(by_id["1"]["lat"] - 48.8566) < 0.01, "Paris devrait être géocodé via BAN (mock)"
assert abs(by_id["3"]["lat"] - 43.2965) < 0.001, "Marseille devrait utiliser les coordonnées directes du CSV"
print("OK enrich_with_coordinates + finalize_records")

# --- Vérifie la robustesse du mapping avec des noms de colonnes différents (variante 2) ---
df2 = pd.DataFrame([
    {"reference": "9", "titre": "Juriste", "employeur": "Prefecture", "commune": "Nantes",
     "cp": "44000", "url": "https://example.gouv.fr/9", "type_contrat": "CDD",
     "date_creation": "2026-08-01", "date_cloture": "2099-01-01"},
])
mapping2 = ud.map_columns(df2)
assert mapping2["title"] == "titre"
assert mapping2["city"] == "commune"
assert mapping2["postal_code"] == "cp"
print("OK map_columns variante 2 (noms de colonnes alternatifs)")

print("\nTous les tests locaux ont réussi.")
