"""Test local (non commité) : valide la logique de update_data.py avec des
données synthétiques reproduisant le VRAI format de colonnes du CSV constaté
le 2026-08-26 (30 colonnes, sans URL ni ville/code postal séparés — voir le
commentaire en tête de update_data.py). Ne fait aucun appel réseau."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import update_data as ud

# --- Reproduit les vraies colonnes du CSV (sous-ensemble pertinent) ---
df = pd.DataFrame([
    {
        "Référence": "REF-1", "Intitulé du poste": "Développeur back-end",
        "Employeur": "Ministère du Numérique", "Organisme de rattachement": "État",
        "Versant": "FPE", "Métier": "Numérique", "Nature de contrat": "CDD",
        "Durée du contrat": "12 mois",
        "Lieu d'affectation": "20 Avenue de Ségur, 75007 Paris",
        "Lieu d'affectation (sans géolocalisation)": "Paris",
        "Localisation du poste": "Île-de-France",
        "Date de première publication": "2026-08-01",
        "Date de fin de publication par défaut": "2099-01-01",
    },
    {
        "Référence": "REF-2", "Intitulé du poste": "Infirmier",
        "Employeur": "CHU Lyon", "Organisme de rattachement": "Hospitalière",
        "Versant": "FPH", "Métier": "Santé", "Nature de contrat": "Titulaire",
        "Durée du contrat": "",
        "Lieu d'affectation": "", "Lieu d'affectation (sans géolocalisation)": "Lyon",
        "Localisation du poste": "Auvergne-Rhône-Alpes",
        "Date de première publication": "2026-08-10",
        "Date de fin de publication par défaut": "2020-01-01",  # expirée -> filtrée
    },
    {
        "Référence": "REF-3", "Intitulé du poste": "Chef de projet SI",
        "Employeur": "Conseil régional PACA", "Organisme de rattachement": "Territoriale",
        "Versant": "FPT", "Métier": "Numérique", "Nature de contrat": "CDI",
        "Durée du contrat": "",
        "Lieu d'affectation": "", "Lieu d'affectation (sans géolocalisation)": "",
        "Localisation du poste": "Marseille",
        "Date de première publication": "2026-08-15",
        "Date de fin de publication par défaut": "2099-01-01",
    },
])

mapping, location_columns = ud.map_columns(df)
assert mapping["title"] == "Intitulé du poste", mapping
assert mapping["employer"] == "Employeur", mapping
assert mapping["domain"] == "Métier", mapping
assert mapping["contract_type"] == "Nature de contrat", f"attendu 'Nature de contrat', obtenu {mapping.get('contract_type')}"
assert mapping["publish_date"] == "Date de première publication", mapping
assert mapping["closing_date"] == "Date de fin de publication par défaut", mapping
assert "url" not in mapping, "pas de colonne URL dans ce jeu de données réel"
assert location_columns == ["Lieu d'affectation", "Lieu d'affectation (sans géolocalisation)", "Localisation du poste"], location_columns
print("OK map_columns (vrai schéma 2026)")

records = ud.build_records(df, mapping, location_columns)
assert records[0]["location_text"] == "20 Avenue de Ségur, 75007 Paris"  # priorité 1
assert records[1]["location_text"] == "Lyon"  # priorité 2 (priorité 1 vide)
assert records[2]["location_text"] == "Marseille"  # priorité 3 (1 et 2 vides)
print("OK build_records : priorité des colonnes de localisation respectée")

records = ud.filter_expired(records)
assert len(records) == 2, f"attendu 2 offres non expirées, obtenu {len(records)}"
print("OK filter_expired (offre expirée exclue)")

# monkeypatch le géocodage réseau pour le test (renvoie désormais (lat, lon, score))
def fake_geocode_ban(location_text):
    fake_coords = {
        "paris": (48.8566, 2.3522, 0.9),      # bon score -> accepté
        "marseille": (43.2965, 5.3698, 0.2),  # score trop bas -> doit être rejeté
    }
    text = location_text.strip().lower()
    for key, coords in fake_coords.items():
        if key in text:
            return coords if coords[2] >= ud.MIN_BAN_SCORE else None
    return None

ud.geocode_ban = fake_geocode_ban
ud.GEOCODE_CACHE_PATH = Path("/tmp/geocode_cache_test.json")
if ud.GEOCODE_CACHE_PATH.exists():
    ud.GEOCODE_CACHE_PATH.unlink()

enriched = ud.enrich_with_coordinates(records, mapping)
final = ud.finalize_records(enriched)
# REF-3 (Marseille) doit être exclu : score de géocodage trop bas (0.2 < MIN_BAN_SCORE)
assert len(final) == 1, f"attendu 1 seule offre géolocalisable (Marseille rejetée pour score bas), obtenu {final}"
by_id = {r["id"]: r for r in final}
assert "REF-3" not in by_id, "REF-3 (score bas) n'aurait pas dû être géolocalisée"
print("OK filtre MIN_BAN_SCORE : résultat de géocodage peu fiable rejeté")

# REF-1 a une adresse complète dans "Lieu d'affectation" -> geocodée via le mock "paris"
assert abs(by_id["REF-1"]["lat"] - 48.8566) < 0.01, by_id["REF-1"]
# Pas de colonne URL -> lien de recherche construit, et marqué comme non direct
assert by_id["REF-1"]["url_est_directe"] is False
assert "google.com/search" in by_id["REF-1"]["url"]
assert "Développeur" in by_id["REF-1"]["url"] or "back-end" in by_id["REF-1"]["url"]
print("OK enrich_with_coordinates + finalize_records + lien de recherche fallback")

# --- Vérifie la migration du cache : une ancienne entrée [lat, lon] sans score
# doit être automatiquement re-géocodée (donc re-vérifiée) au run suivant. ---
old_format_cache = {"marseille": [43.0, 5.0]}  # ancien format, 2 éléments, pas de score
ud.save_json(ud.GEOCODE_CACHE_PATH, old_format_cache)
records2 = ud.build_records(df, mapping, location_columns)
records2 = ud.filter_expired(records2)
enriched2 = ud.enrich_with_coordinates(records2, mapping)
cache_after = ud.load_json(ud.GEOCODE_CACHE_PATH, {})
assert cache_after.get("marseille") is None, (
    f"l'entrée de cache sans score aurait dû être re-géocodée puis rejetée (score bas), obtenu {cache_after.get('marseille')}"
)
print("OK migration du cache : ancienne entrée sans score re-vérifiée automatiquement")

print("\nTous les tests locaux ont réussi (schéma réel du 2026-08-26).")
