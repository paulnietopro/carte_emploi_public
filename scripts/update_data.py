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

Particularité de ce jeu de données (constaté le 2026-08-26 sur un run réel) :
il ne contient NI colonne "URL de l'offre" NI colonnes ville/code postal
séparées. La localisation se déduit d'un des 3 champs texte libres
("Lieu d'affectation", "Lieu d'affectation (sans géolocalisation)",
"Localisation du poste", utilisés dans cet ordre de priorité par ligne) que
l'on géocode via l'API Adresse. Faute d'URL directe, un lien de recherche
Google (site:choisirleservicepublic.gouv.fr) est construit à la place — voir
build_fallback_search_url().

Usage:
    python scripts/update_data.py

Variables d'environnement optionnelles:
    MAX_GEOCODE_PER_RUN   nombre max de nouvelles adresses géocodées par run
                           (défaut 4000 ; le premier run peut ne pas tout
                           géocoder d'un coup, le reste se rattrape aux runs
                           suivants grâce au cache)
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OFFERS_OUT = DATA_DIR / "offres.json"
LAST_UPDATE_OUT = DATA_DIR / "last_update.json"
GEOCODE_CACHE_PATH = DATA_DIR / "geocode_cache.json"

DATASET_SLUG = "les-offres-diffusees-sur-choisir-le-service-public"
DATASET_API_URL = f"https://www.data.gouv.fr/api/1/datasets/{DATASET_SLUG}/"

BAN_SEARCH_URL = "https://api-adresse.data.gouv.fr/search/"

MAX_GEOCODE_PER_RUN = int(os.environ.get("MAX_GEOCODE_PER_RUN", "4000"))
HTTP_TIMEOUT = 30
USER_AGENT = "carte-emploi-public/1.0 (github actions data refresh script)"

# Score de confiance minimum (0 à 1) renvoyé par l'API Adresse pour accepter
# un géocodage. En dessous, l'offre est considérée non localisable plutôt que
# risquer un placement trompeur sur la carte (ex : texte de localisation vague
# comme un nom de zone/bassin administratif). Ajustable si trop strict/laxiste.
MIN_BAN_SCORE = float(os.environ.get("MIN_BAN_SCORE", "0.45"))

# ---------------------------------------------------------------------------
# Correspondance colonne logique -> fragments de noms possibles dans le CSV
# (comparaison faite sur des noms de colonnes normalisés : minuscules, sans
# accents, underscores). Le premier fragment trouvé DANS le nom de colonne
# (contains, pas égalité stricte) gagne, dans l'ordre de la liste.
# ---------------------------------------------------------------------------
COLUMN_ALIASES: dict[str, list[str]] = {
    "id": ["id_offre", "reference_offre", "numero_offre", "reference", "id"],
    "title": ["intitule_du_poste", "intitule_offre", "intitule", "titre_offre", "titre"],
    "employer": ["nom_employeur", "employeur", "libelle_employeur", "etablissement", "organisme_de_rattachement", "organisme"],
    "domain": ["domaine_metier", "famille_metier", "domaine", "metier"],
    "contract_type": ["nature_de_contrat", "nature_contrat", "type_contrat", "categorie_contrat", "duree_du_contrat", "contrat"],
    "versant": ["versant", "type_employeur", "fonction_publique"],
    "publish_date": ["date_de_premiere_publication", "date_de_debut_de_publication", "date_publication", "date_creation"],
    "closing_date": ["date_de_fin_de_publication", "date_limite_candidature", "date_cloture", "date_limite"],
    "url": ["url_offre", "lien_offre", "url", "lien"],
    "lat": ["latitude", "lat"],
    "lon": ["longitude", "lng", "lon"],
    # Champs "ville"/"code_postal" gardés en fallback pour d'anciennes variantes
    # du CSV qui les exposeraient sous cette forme classique (voir aussi
    # LOCATION_COLUMN_CANDIDATES ci-dessous pour le format constaté en 2026).
    "city": ["ville", "commune", "lieu_travail_libelle", "lieu_de_travail", "libelle_lieu_travail"],
    "postal_code": ["code_postal_lieu_travail", "code_postal", "cp_lieu_travail", "cp"],
    "department": ["departement_lieu_travail", "code_departement", "departement"],
    "region": ["region_lieu_travail", "region"],
}

REQUIRED_LOGICAL_FIELDS = ["title"]

# Colonnes texte libre décrivant la localisation, par ordre de priorité
# (on prend la première non vide, ligne par ligne). Comparaison en égalité
# stricte sur le nom normalisé (pas de "contains") car ces noms se chevauchent
# ("lieu_d_affectation" est un préfixe de "lieu_d_affectation_sans_...").
LOCATION_COLUMN_CANDIDATES = [
    "lieu_d_affectation",
    "lieu_d_affectation_sans_geolocalisation",
    "localisation_du_poste",
]

# Tentative d'extraction de coordonnées directement présentes dans un champ
# texte (au cas où "Lieu d'affectation" embarquerait un WKT ou un couple
# lat/lon). Bornes approximatives de la France métropolitaine + DROM larges
# pour éviter de confondre avec un code postal ou une référence.
COORD_PATTERNS = [
    re.compile(r"POINT\s*\(\s*(-?\d{1,3}\.\d+)\s+(-?\d{1,3}\.\d+)\s*\)", re.IGNORECASE),  # WKT: lon lat
    re.compile(r"(-?\d{1,2}\.\d{3,})\s*[,;]\s*(-?\d{1,2}\.\d{3,})"),  # "lat,lon" ou "lon,lat" généreux
]


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


def map_columns(df: pd.DataFrame) -> tuple[dict[str, str], list[str]]:
    """Renvoie (mapping colonne logique -> colonne CSV, liste ordonnée des
    colonnes de localisation texte libre réellement présentes)."""
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

    location_columns = [
        normalized_to_original[cand] for cand in LOCATION_COLUMN_CANDIDATES if cand in normalized_to_original
    ]

    print("[info] Correspondance colonne logique -> colonne CSV :")
    for logical in COLUMN_ALIASES:
        print(f"       {logical:15s} -> {mapping.get(logical)}")
    print(f"[info] Colonnes de localisation (texte libre, par priorité) : {location_columns}")

    # Petit échantillon de valeurs brutes pour les colonnes de localisation,
    # utile pour diagnostiquer le format exact si le géocodage échoue en masse.
    if location_columns:
        sample = df[location_columns].head(3).to_dict(orient="records")
        print(f"[info] Échantillon de valeurs de localisation (3 premières lignes) : {sample}")

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
    if not has_latlon and not has_city_or_cp and not location_columns:
        raise RuntimeError(
            "Ni coordonnées GPS, ni ville/code postal, ni champ de localisation "
            "texte libre trouvés dans le CSV : impossible de placer les offres "
            "sur la carte. Voir la liste des colonnes ci-dessus et mettre à jour "
            "COLUMN_ALIASES / LOCATION_COLUMN_CANDIDATES."
        )

    return mapping, location_columns


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


def extract_coords_from_text(text: str | None) -> tuple[float, float] | None:
    if not text:
        return None
    for pattern in COORD_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        a, b = float(m.group(1)), float(m.group(2))
        # Devine l'ordre (lat, lon) à partir des bornes plausibles pour la France.
        for lat, lon in ((a, b), (b, a)):
            if 40 <= lat <= 52 and -6 <= lon <= 10:
                return lat, lon
    return None


def geocode_key(location_text: str) -> str:
    return re.sub(r"\s+", " ", location_text.strip().lower())


def geocode_ban(location_text: str) -> tuple[float, float, float] | None:
    """Interroge l'API Adresse (Base Adresse Nationale), gratuite et sans clé,
    avec le texte de localisation tel quel (recherche libre). Renvoie
    (lat, lon, score) — le score (0 à 1) est conservé dans le cache pour
    pouvoir re-filtrer plus tard sans refaire d'appel réseau si le seuil
    MIN_BAN_SCORE change.

    Un texte de localisation vague (ex : nom d'une zone/bassin administratif
    de l'Éducation nationale plutôt qu'une ville ou une adresse précise) peut
    renvoyer un résultat "le plus proche possible" mais peu fiable — sans ce
    filtre, une offre pouvait se retrouver géolocalisée à des dizaines de km
    de son vrai lieu d'affectation (cas constaté : Mont-de-Marsan -> Pauillac).
    On rejette donc tout résultat sous MIN_BAN_SCORE plutôt que d'afficher un
    point trompeur sur la carte."""
    try:
        resp = requests.get(
            BAN_SEARCH_URL,
            params={"q": location_text, "limit": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        payload = resp.json()
        features = payload.get("features") or []
        if not features:
            return None
        best = features[0]
        score = float((best.get("properties") or {}).get("score") or 0.0)
        if score < MIN_BAN_SCORE:
            return None
        lon, lat = best["geometry"]["coordinates"]
        return float(lat), float(lon), score
    except Exception:  # noqa: BLE001
        return None


def enrich_with_coordinates(records: list[dict], mapping: dict[str, str]) -> list[dict]:
    cache: dict[str, list[float] | None] = load_json(GEOCODE_CACHE_PATH, {})
    print(f"[info] Cache de géocodage chargé : {len(cache)} entrées.")

    has_direct_latlon = "lat" in mapping and "lon" in mapping

    # 1) Coordonnées directes du CSV si présentes et valides.
    # 2) Sinon, coordonnées embarquées dans le texte de localisation (WKT etc.).
    for r in records:
        r["_lat"], r["_lon"] = None, None
        if has_direct_latlon:
            try:
                lat = float(str(r.get("lat")).replace(",", "."))
                lon = float(str(r.get("lon")).replace(",", "."))
                if -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0):
                    r["_lat"], r["_lon"] = lat, lon
                    continue
            except (TypeError, ValueError):
                pass
        coords = extract_coords_from_text(r.get("location_text"))
        if coords:
            r["_lat"], r["_lon"] = coords

    # 3) Pour le reste, géocoder le texte de localisation, avec cache + limite par run.
    # Les entrées de cache d'un ancien format (sans score, [lat, lon] à 2
    # éléments) sont considérées à re-vérifier : elles ont pu être acceptées
    # avant l'introduction du filtre MIN_BAN_SCORE et être en réalité peu
    # fiables (cf. cas Mont-de-Marsan -> Pauillac). Un résultat déjà rejeté
    # (None) n'a pas besoin d'être retenté.
    to_geocode: dict[str, str] = {}
    stale_cache_entries = 0
    for r in records:
        if r["_lat"] is not None:
            continue
        loc = (r.get("location_text") or "").strip()
        if not loc:
            continue
        key = geocode_key(loc)
        if key in cache:
            cached_val = cache[key]
            if cached_val is None or len(cached_val) >= 3:
                continue  # déjà résolu (avec score vérifié) ou déjà connu comme non-géocodable
            stale_cache_entries += 1
        to_geocode.setdefault(key, loc)

    if stale_cache_entries:
        print(f"[info] {stale_cache_entries} entrées de cache d'un ancien format (sans score) seront re-géocodées pour vérification.")
    print(f"[info] Nouvelles localisations à géocoder : {len(to_geocode)} (plafond ce run : {MAX_GEOCODE_PER_RUN}).")

    geocoded_this_run = 0
    for key, loc in to_geocode.items():
        if geocoded_this_run >= MAX_GEOCODE_PER_RUN:
            print("[warn] Plafond de géocodage atteint pour ce run, le reste sera traité au prochain run planifié.")
            break
        result = geocode_ban(loc)
        cache[key] = list(result) if result else None
        geocoded_this_run += 1
        if geocoded_this_run % 200 == 0:
            save_json(GEOCODE_CACHE_PATH, cache)  # sauvegarde incrémentale : rien n'est perdu si le run est interrompu
            print(f"[info] ... {geocoded_this_run} localisations géocodées jusqu'ici (cache sauvegardé).")
        time.sleep(0.08)  # reste largement sous la limite de l'API BAN

    save_json(GEOCODE_CACHE_PATH, cache)
    print(f"[info] {geocoded_this_run} nouvelles adresses géocodées ce run. Cache total : {len(cache)}.")

    # 4) Appliquer le cache aux enregistrements restants.
    unresolved = 0
    for r in records:
        if r["_lat"] is not None:
            continue
        loc = (r.get("location_text") or "").strip()
        cached = cache.get(geocode_key(loc)) if loc else None
        if cached:
            r["_lat"], r["_lon"] = cached[0], cached[1]
        else:
            unresolved += 1

    if unresolved:
        print(f"[warn] {unresolved} offres sans coordonnées exploitables (géocodage échoué, en attente du prochain run, ou localisation manquante) — elles seront exclues de la carte pour l'instant.")

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


def build_records(df: pd.DataFrame, mapping: dict[str, str], location_columns: list[str]) -> list[dict]:
    records = []
    for row in df.to_dict(orient="records"):
        rec = {}
        for logical, col in mapping.items():
            val = row.get(col)
            if val is None or (isinstance(val, float) and pd.isna(val)):
                val = None
            elif isinstance(val, str):
                val = val.strip() or None
            rec[logical] = val

        location_text = None
        for col in location_columns:
            val = row.get(col)
            if isinstance(val, str) and val.strip():
                location_text = val.strip()
                break
        rec["location_text"] = location_text

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


def build_fallback_search_url(title: str | None, employer: str | None) -> str:
    """Faute de colonne URL dans le jeu de données, construit un lien de
    recherche vers le site officiel plutôt qu'un lien direct potentiellement
    faux. Clairement labellisé "Rechercher" côté frontend, pas "Voir l'offre"."""
    parts = [p for p in [title, employer] if p]
    query = " ".join(parts) or "offre"
    q = f'site:choisirleservicepublic.gouv.fr "{query}"'
    return "https://www.google.com/search?q=" + urllib.parse.quote(q)


def finalize_records(records: list[dict]) -> list[dict]:
    out = []
    for i, r in enumerate(records):
        if r.get("_lat") is None or r.get("_lon") is None:
            continue
        title = r.get("title") or "Offre sans titre"
        employer = r.get("employer")
        url = r.get("url") or build_fallback_search_url(title, employer)
        out.append({
            "id": r.get("id") or f"offre-{i}",
            "titre": title,
            "employeur": employer,
            "ville": r.get("city") or r.get("location_text"),
            "code_postal": r.get("postal_code"),
            "departement": r.get("department"),
            "region": r.get("region"),
            "domaine": r.get("domain"),
            "contrat": r.get("contract_type"),
            "versant": r.get("versant"),
            "date_publication": r.get("publish_date"),
            "date_limite": r.get("closing_date"),
            "url": url,
            "url_est_directe": bool(r.get("url")),
            "lat": round(r["_lat"], 5),
            "lon": round(r["_lon"], 5),
        })
    return out


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    try:
        csv_url, resource_title = find_dataset_csv_url()
        df = download_csv(csv_url)
        mapping, location_columns = map_columns(df)
        records = build_records(df, mapping, location_columns)
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
