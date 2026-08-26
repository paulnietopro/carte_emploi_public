# Carte des offres — Service public (rayon de recherche)

Application web statique qui affiche sur une carte interactive les offres
d'emploi du service public français, avec un filtre par **rayon de recherche
en kilomètres** autour d'une adresse choisie — une fonctionnalité absente du
site officiel [choisirleservicepublic.gouv.fr](https://choisirleservicepublic.gouv.fr).

⚠️ **Projet indépendant, non affilié à l'État ni à la DGAFP.** Il réutilise
uniquement les données ouvertes publiées sous Licence Ouverte 2.0.

## Comment ça marche

- **Source des données** : le jeu de données ouvert officiel [*"Les offres
  diffusées sur Choisir le Service Public"*](https://www.data.gouv.fr/datasets/les-offres-diffusees-sur-choisir-le-service-public)
  publié par la DGAFP sur data.gouv.fr (CSV, mis à jour régulièrement par
  l'administration).
- **Rafraîchissement automatique** : une GitHub Action (`.github/workflows/update-data.yml`)
  tourne toutes les 12h, télécharge le dernier CSV, géocode les offres qui
  n'ont pas de coordonnées GPS via l'[API Adresse](https://adresse.data.gouv.fr/)
  (Base Adresse Nationale, gratuite), et commit le résultat dans `data/offres.json`.
- **Frontend** : une page HTML/JS statique (Leaflet.js pour la carte) qui lit
  `data/offres.json` et calcule, entièrement côté navigateur, les offres
  situées dans le rayon choisi autour du point de recherche (formule de
  Haversine).
- **Hébergement** : GitHub Pages, gratuit, aucun serveur à gérer.

Aucune clé d'API n'est nécessaire (data.gouv.fr et la Base Adresse Nationale
sont en accès libre).

### Particularité constatée du CSV (2026)

Le CSV réellement publié ne contient ni colonne "URL de l'offre" ni colonnes
ville/code postal séparées. Le script déduit donc la localisation d'un champ
texte libre (`Lieu d'affectation`, avec repli sur `Lieu d'affectation (sans
géolocalisation)` puis `Localisation du poste`) qu'il géocode via l'API
Adresse. Faute d'URL directe dans les données, chaque offre est accompagnée
d'un lien de **recherche** vers le site officiel (`site:choisirleservicepublic.gouv.fr`
sur Google) plutôt que d'un lien direct potentiellement erroné — le lien est
étiqueté différemment dans l'interface selon les cas ("Voir l'offre" si une
vraie URL existe un jour dans le jeu de données, "Rechercher cette offre"
sinon).

## Mise en ligne (une seule fois)

1. Crée un nouveau dépôt GitHub (public, sinon GitHub Pages gratuit n'est pas
   disponible sur un dépôt privé sauf compte payant) et pousse le contenu de
   ce dossier :

   ```bash
   cd carte-emploi-public
   git remote add origin https://github.com/<ton-compte>/<ton-repo>.git
   git add -A
   git commit -m "Première version"
   git branch -M main
   git push -u origin main
   ```

2. Active **GitHub Pages** : Settings → Pages → *Build and deployment* →
   Source : `Deploy from a branch` → Branch : `main` / `/(root)` → Save.
   Le site sera disponible à `https://<ton-compte>.github.io/<ton-repo>/`
   après une ou deux minutes.

3. Vérifie que les **Actions** sont activées : onglet *Actions* du dépôt →
   si un bandeau demande de les activer, clique dessus.

4. Déclenche le premier import de données manuellement (pas besoin d'attendre
   12h) : onglet *Actions* → workflow **"Mise à jour des offres (toutes les
   12h)"** → bouton *Run workflow*. Ça prend en général 2 à 10 minutes selon
   le nombre d'offres à géocoder (le tout premier run est le plus long ; les
   suivants sont rapides car les villes déjà géocodées sont mises en cache
   dans `data/geocode_cache.json`).

5. Recharge la page du site : les offres doivent apparaître dès que tu
   sélectionnes une adresse et un rayon.

Ensuite, plus rien à faire : le workflow tourne tout seul toutes les 12h et
committe les mises à jour, ce qui redéploie automatiquement GitHub Pages.

## Développement local

```bash
python3 -m http.server 8000
# puis ouvrir http://localhost:8000
```

Pour tester le script de mise à jour des données en local :

```bash
cd scripts
pip install -r requirements.txt
python update_data.py
```

## Si le script de mise à jour échoue

Le format du CSV publié par l'administration peut changer. Le script est
conçu pour être facile à corriger dans ce cas :

1. Regarde les logs du run échoué dans l'onglet *Actions* du dépôt.
2. Le script affiche systématiquement la liste des colonnes réelles trouvées
   dans le CSV, ainsi que la correspondance qu'il a déduite.
3. Ajuste le dictionnaire `COLUMN_ALIASES` en haut de `scripts/update_data.py`
   pour ajouter le(s) nouveau(x) nom(s) de colonne, puis commit/push — le
   workflow se redéclenche automatiquement sur les changements de ce fichier.

## Structure du projet

```
index.html                        Page principale (carte + filtres)
assets/style.css                  Styles
assets/app.js                     Logique carte, recherche, filtre par rayon
scripts/update_data.py            Récupération + géocodage des offres
scripts/requirements.txt          Dépendances Python du script
scripts/_local_test.py            Tests locaux du script (données synthétiques)
data/offres.json                  Données consommées par le frontend (générées)
data/last_update.json             Horodatage de la dernière mise à jour (généré)
data/geocode_cache.json           Cache des géocodages déjà effectués (généré)
.github/workflows/update-data.yml Workflow GitHub Actions (cron 12h)
```

## Pistes d'amélioration possibles

- Ajouter un partage d'URL avec les paramètres de recherche (centre + rayon) en query string.
- Ajouter un export CSV des résultats filtrés.
- Ajouter d'autres filtres (versant de la fonction publique, catégorie A/B/C).
- Passer le géocodage en traitement incrémental plus fin si le volume d'offres augmente beaucoup.
