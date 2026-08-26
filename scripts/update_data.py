#!/usr/bin/env python3
"""
Récupère le jeu de données ouvert "Les offres diffusées sur Choisir le Service
Public" publié par la DGAFP sur data.gouv.fr, le nettoie, géocode les offres
qui n'ont pas de coordonnées GPS, et écrit un fichier JSON compact que le
front-end (carte Leaflet) consomme.

Ce script est volontairement défensif sur les noms de colonnes : le format du
CSV publié par l'administration peut changer d'une semaine à l'autre. Toute la
correspondance "colonne logique -> noms possibles dans le CSV" est centralisée
dans COLUMN_ALIASES ci-dessous. Si le script échoue avec une erreur du type
"colonne introuvable", il suffit de regarder la liste des colonnes réelles
affichée dans le log (imprimée systématiquement au démarrage) et de compléter
la liste d'alias correspondante.

Usage:
    python scripts/update_data.py

Variables d'environnement optionnelles:
    MAX_GEOCODE_PER_RUN   nombre max de nouvelles adresses géocodées par run
                           (défaut 2000, pour ne pas taper trop fort sur l'API
                           BAN lors du tout premier run)
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OFFERS_OUT = DATA_DIR / "offres.json"
LAST_UPDATE_OUT = DATA_DIR / "last_update.json"
GEOCODE_CACHE_PATH = DATA_DIR / "geocode_cache.json"
UNMATCHED_LOG_PATH = DATA_DIR / "geocode_unresolved.json"

DATASET_SLUG = "les-offres-diffusees-sur-choisir-le-service-public"
DATASET_API_URL = f"https://www.data.gouv.fr/api/1/datasets/{DATASET_SLUG}/"

BAN_SEARCH_URL = "https://api-adresse.data.gouv.fr/search/"

MAX_GEOCODE_PER_RUN = int(os.environ.get("MAX_GEOCODE_PER_RUN", "2000"))
HTTP_TIMEOUT = 30
USER_AGENT = "carte-emploi-public/1.0 (github actions data refresh script)"

# ---------------------------------------------------------------------------
# Correspondance colonne logique -> fragments de noms possibles dans le CSV
# (comparaison faite sur des noms de colonnes normalisés : minuscules, sans
# accents, underscores). Le premier fragment trouvé DANS le nom de colonne
# (contains, pas égalité stricte) gagne, dans l'ordre de la liste.
# ---------------------------------------------------------------------------
COLUMN_ALIASES: dict[str, list[str]] = {
    "id": ["id_offre", "reference_offre", "numero_offre", "reference", "id"],
    "title": ["intitule_du_poste", "intitule_offre", "intitule_du_poste", "intitule", "titre_offre", "titre"],
    "employer": ["nom_employeur", "employeur", "libelle_employeur", "etablissement", "organisme"],
    "city": ["ville", "commune", "lieu_travail_libelle", "lieu_de_travail", "libelle_lieu_travail", "localisation"],
    "postal_code": ["code_postal_lieu_travail", "code_postal", "cp_lieu_travail", "cp"],
    "department": ["departement_lieu_travail", "code_departement", "departement"],
    "region": ["region_lieu_travail", "region"],
    "domain": ["domaine_metier", "famille_metier", "domaine"],
    "contract_type": ["nature_contrat", "type_contrat", "categorie_contrat", "contrat"],
    "versant": ["versant", "type_employeur", "fonction_publique"],
    "publish_date": ["date_publication", "date_creation", "date_de_publication", "date_debut_publication"],
    "closing_date": ["date_limite_candidature", "date_cloture", "date_fin_publication", "date_limite"],
    "url": ["url_offre", "lien_offre", "url", "lien"],
    "lat": ["latitude", "lat"],
    "lon": ["longitude", "lng", "lon"],
}

REQUIRED_LOGICAL_FIELDS = ["title", "url"]
# On a besoin d'AU MOINS une des deux façons de localiser une offre :
# soit des coordonnées déjà présentes, soit ville/code postal à géocoder.


def normalize_colname(name: str) -> str:
    name = str(name).strip().lower()
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[^a-z0-9]+", "_", name).strip("_")
    return name


def find_dataset_csv_url() -> tuple[str, str]:
    """Interroge l'API data.gouv.fr et renvoie (url, titre) de la ressource
    CSV la plus récente correspondant aux offres (exclut les référentiels et
    la documentation)."""
    print(f"[info] Interrogation de l'API data.gouv.fr : {DATASET_API_URL}")
    resp = requests.get(DATASET_API_URL, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()

    candidates = []
    for res in payload.get("resources", []):
        title = (res.get("title") or "").lower()
        fmt = (res.get("format") or "").lower()
        url = res.get("url")
        if not url or fmt != "csv":
            continue
        if "referentiel" in title or "documentation" in title or "guide" in title:
            continue
        if "offre" not in title and "offres-datagouv" not in title:
            continue
        last_modified = res.get("last_modified") or res.get("created_at") or ""
        candidates.append((last_modified, url, res.get("title", "")))

    if not candidates:
        raise RuntimeError(
            "Aucune ressource CSV d'offres trouvée sur le dataset data.gouv.fr. "
            "Le dataset a peut-être été renommé ou déplacé — vérifier "
            f"{DATASET_API_URL} manuellement."
        )

    candidates.sort(key=lambda c: c[0], reverse=True)
    _, url, title = candidates[0]
    print(f"[info] Ressource CSV retenue : {title} -> {url}")
    return url, title


def download_csv(url: str) -> pd.DataFrame:
    print(f"[info] Téléchargement du CSV depuis {url} ...")
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=180, stream=True)
    resp.raise_for_status()
    raw = resp.content
    print(f"[info] {len(raw) / 1_000_000:.1f} Mo téléchargés.")

    # Le format exact (séparateur, encodage) des CSV data.gouv.fr varie.
    # On tente plusieurs combinaisons raisonnables avant d'abandonner.
    last_error = None
    for encoding in ("utf-8", "latin-1"):
        for sep in (";", ",", "\t"):
            try:
                df = pd.read_csv(
                    io.BytesIO(raw),
                    sep=sep,
                    encoding=encoding,
                    dtype=str,
                    low_memory=False,
                    on_bad_lines="skip",
                )
                # Heuristique : un bon parsing donne plusieurs colonnes.
                if df.shape[1] >= 5:
                    print(f"[info] CSV lu avec sep={sep!r} encoding={encoding!r} -> {df.shape[0]} lignes, {df.shape[1]} colonnes.")
                    return df
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue

    raise RuntimeError(f"Impossible de parser le CSV avec les combinaisons connues. Dernière erreur : {last_error}")


def map_columns(df: pd.DataFrame) -> dict[str, str]:
    normalized_to_original = {normalize_colname(c): c for c in df.columns}
    print("[info] Colonnes détectées dans le CSV source :")
    for norm, orig in normalized_to_original.items():
        print(f"       {orig!r}  (normalisé: {norm})")

    mapping: dict[str, str] = {}
    for logical, fragments in COLUMN_ALIASES.items():
        found = None
        for frag in fragments:
            for norm, orig in normalized_to_original.items():
                if frag in norm:
                    found = orig
                    break
            if found:
                break
        if found:
            mapping[logical] = found

    print("[info] Correspondance colonne logique -> colonne CSV :")
    for logical in COLUMN_ALIASES:
        print(f"       {logical:15s} -> {mapping.get(logical)}")

    missing_required = [f for f in REQUIRED_LOGICAL_FIELDS if f not in mapping]
    if missing_required:
        raise RuntimeError(
            f"Colonnes obligatoires introuvables dans le CSV : {missing_required}. "
            "Le format du fichier source a probablement changé. Voir la liste des "
            "colonnes réelles ci-dessus et mettre à jour COLUMN_ALIASES dans "
            "scripts/update_data.py en conséquence."
        )

    has_latlon = "lat" in mapping and "lon" in mapping
    has_city_or_cp = "city" in mapping or "postal_code" in mapping
    if not has_latlon and not has_city_or_cp:
        raise RuntimeError(
            "Ni coordonnées GPS ni ville/code postal trouvées dans le CSV : "
            "impossible de placer les offres sur la carte. Voir la liste des "
            "colonnes ci-dessus et mettre à jour COLUMN_ALIASES."
        )

    return mapping


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return default
    return default


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=None, separators=(",", ":")), encoding="utf-8")


def geocode_key(city: str | None, postal_code: str | None) -> str:
    city = (city or "").strip()
    postal_code = (postal_code or "").strip()
    return f"{postal_code}|{city}".lower()


def geocode_ban(city: str, postal_code: str) -> tuple[float, float] | None:
    """Interroge l'API Adresse (Base Adresse Nationale), gratuite et sans clé."""
    params = {"limit": 1}
    query_parts = [p for p in [city] if p]
    if not query_parts:
        query_parts = [postal_code]
    params["q"] = " ".join(query_parts)
    if postal_code:
        params["postcode"] = postal_code

    try:
        resp = requests.get(BAN_SEARCH_URL, params=params, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            return None
        payload = resp.json()
        features = payload.get("features") or []
        if not features:
            return None
        lon, lat = features[0]["geometry"]["coordinates"]
        return float(lat), float(lon)
    except Exception:  # noqa: BLE001
        return None


def enrich_with_coordinates(records: list[dict], mapping: dict[str, str]) -> list[dict]:
    cache: dict[str, list[float] | None] = load_json(GEOCODE_CACHE_PATH, {})
    print(f"[info] Cache de géocodage chargé : {len(cache)} entrées.")

    has_direct_latlon = "lat" in mapping and "lon" in mapping

    # 1) Utiliser les coordonnées directes du CSV quand elles existent et sont valides.
    for r in records:
        if has_direct_latlon:
            try:
                lat = float(str(r.get("lat")).replace(",", "."))
                lon = float(str(r.get("lon")).replace(",", "."))
                if -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0):
                    r["_lat"], r["_lon"] = lat, lon
                    continue
            except (TypeError, ValueError):
                pass
        r["_lat"], r["_lon"] = None, None

    # 2) Pour le reste, géocoder par (ville, code postal), avec cache + limite par run.
    to_geocode_keys: dict[str, tuple[str, str]] = {}
    for r in records:
        if r["_lat"] is not None:
            continue
        key = geocode_key(r.get("city"), r.get("postal_code"))
        if key == "|":
            continue
        if key in cache:
            continue
        to_geocode_keys.setdefault(key, (r.get("city") or "", r.get("postal_code") or ""))

    print(f"[info] Nouvelles localisations à géocoder : {len(to_geocode_keys)} (plafond ce run : {MAX_GEOCODE_PER_RUN}).")

    geocoded_this_run = 0
    for key, (city, cp) in to_geocode_keys.items():
        if geocoded_this_run >= MAX_GEOCODE_PER_RUN:
            print("[warn] Plafond de géocodage atteint pour ce run, le reste sera traité au prochain run planifié.")
            break
        result = geocode_ban(city, cp)
        cache[key] = list(result) if result else None
        geocoded_this_run += 1
        time.sleep(0.08)  # reste largement sous la limite de l'API BAN

    save_json(GEOCODE_CACHE_PATH, cache)
    print(f"[info] {geocoded_this_run} nouvelles adresses géocodées ce run. Cache total : {len(cache)}.")

    # 3) Appliquer le cache aux enregistrements restants.
    unresolved = 0
    for r in records:
        if r["_lat"] is not None:
            continue
        key = geocode_key(r.get("city"), r.get("postal_code"))
        cached = cache.get(key)
        if cached:
            r["_lat"], r["_lon"] = cached[0], cached[1]
        else:
            unresolved += 1

    if unresolved:
        print(f"[warn] {unresolved} offres sans coordonnées exploitables (géocodage échoué ou ville/CP manquants) — elles seront exclues de la carte.")

    return records


def parse_date_safe(value: str | None):
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S", "%d-%m-%Y"):
        try:
            return datetime.strptime(value[:19] if "T" in fmt else value[:10], fmt)
        except ValueError:
            continue
    return None


def build_records(df: pd.DataFrame, mapping: dict[str, str]) -> list[dict]:
    records = []
    for _, row in df.iterrows():
        rec = {}
        for logical, col in mapping.items():
            val = row.get(col)
            if pd.isna(val):
                val = None
            elif isinstance(val, str):
                val = val.strip()
            rec[logical] = val
        records.append(rec)
    return records


def filter_expired(records: list[dict]) -> list[dict]:
    if not records or "closing_date" not in records[0]:
        return records
    today = datetime.now(timezone.utc).replace(tzinfo=None)
    kept = []
    dropped = 0
    for r in records:
        d = parse_date_safe(r.get("closing_date"))
        if d is not None and d < today:
            dropped += 1
            continue
        kept.append(r)
    print(f"[info] {dropped} offres avec date limite de candidature dépassée exclues.")
    return kept


def finalize_records(records: list[dict]) -> list[dict]:
    out = []
    for i, r in enumerate(records):
        if r.get("_lat") is None or r.get("_lon") is None:
            continue
        out.append({
            "id": r.get("id") or f"offre-{i}",
            "titre": r.get("title") or "Offre sans titre",
            "employeur": r.get("employer"),
            "ville": r.get("city"),
            "code_postal": r.get("postal_code"),
            "departement": r.get("department"),
            "region": r.get("region"),
            "domaine": r.get("domain"),
            "contrat": r.get("contract_type"),
            "versant": r.get("versant"),
            "date_publication": r.get("publish_date"),
            "date_limite": r.get("closing_date"),
            "url": r.get("url"),
            "lat": round(r["_lat"], 5),
            "lon": round(r["_lon"], 5),
        })
    return out


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    try:
        csv_url, resource_title = find_dataset_csv_url()
        df = download_csv(csv_url)
        mapping = map_columns(df)
        records = build_records(df, mapping)
        records = filter_expired(records)
        records = enrich_with_coordinates(records, mapping)
        final_records = finalize_records(records)
    except Exception as exc:  # noqa: BLE001
        print(f"[error] Échec de la mise à jour des données : {exc}", file=sys.stderr)
        return 1

    if not final_records:
        print("[error] Aucune offre exploitable après traitement — on n'écrase pas le fichier existant.", file=sys.stderr)
        return 1

    save_json(OFFERS_OUT, final_records)
    save_json(LAST_UPDATE_OUT, {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_resource": resource_title,
        "source_url": csv_url,
        "total_offres": len(final_records),
    })

    print(f"[ok] {len(final_records)} offres géolocalisées écrites dans {OFFERS_OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
