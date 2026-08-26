/* Carte des offres d'emploi du service public — filtrage par rayon.
 * Données : data/offres.json (généré par scripts/update_data.py, rafraîchi
 * toutes les 12h par une GitHub Action). Géocodage d'adresse : API Adresse
 * (Base Adresse Nationale, gratuite, sans clé).
 */

const BAN_SEARCH_URL = "https://api-adresse.data.gouv.fr/search/";
const DEFAULT_CENTER = [46.6, 2.4]; // France
const DEFAULT_ZOOM = 6;

let allOffers = [];
let searchCenter = null; // {lat, lon, label}
let markerClusterGroup = null;
let centerMarker = null;
let radiusCircle = null;
let markersById = new Map();
let activeCardId = null;

const map = L.map("map", { zoomControl: true }).setView(DEFAULT_CENTER, DEFAULT_ZOOM);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; <a href=\"https://www.openstreetmap.org/copyright\">OpenStreetMap</a> contributors",
}).addTo(map);

markerClusterGroup = L.markerClusterGroup({ maxClusterRadius: 45 });
map.addLayer(markerClusterGroup);

// ---------------------------------------------------------------------
// Utilitaires
// ---------------------------------------------------------------------

function haversineKm(lat1, lon1, lat2, lon2) {
  const R = 6371;
  const dLat = ((lat2 - lat1) * Math.PI) / 180;
  const dLon = ((lon2 - lon1) * Math.PI) / 180;
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos((lat1 * Math.PI) / 180) * Math.cos((lat2 * Math.PI) / 180) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

function debounce(fn, delay) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), delay);
  };
}

function el(id) { return document.getElementById(id); }

// ---------------------------------------------------------------------
// Chargement des données
// ---------------------------------------------------------------------

async function loadOffers() {
  try {
    const resp = await fetch("data/offres.json", { cache: "no-store" });
    allOffers = await resp.json();
  } catch (e) {
    console.error("Impossible de charger data/offres.json", e);
    allOffers = [];
  }
  populateFilterOptions();
  render();

  if (allOffers.length === 0) {
    el("first-run-banner").style.display = "block";
  }

  try {
    const resp2 = await fetch("data/last_update.json", { cache: "no-store" });
    const info = await resp2.json();
    const d = new Date(info.updated_at);
    el("last-update").textContent =
      `Dernière mise à jour des données : ${d.toLocaleString("fr-FR")} (${info.total_offres} offres au total)`;
  } catch (e) {
    // pas grave si absent (premier déploiement avant le premier run de l'Action)
    el("last-update").textContent = "Données pas encore générées — voir le message ci-dessus.";
  }
}

function populateFilterOptions() {
  const domains = new Set();
  const contracts = new Set();
  for (const o of allOffers) {
    if (o.domaine) domains.add(o.domaine);
    if (o.contrat) contracts.add(o.contrat);
  }
  fillSelect(el("domain-filter"), domains);
  fillSelect(el("contract-filter"), contracts);
}

function fillSelect(selectEl, values) {
  const sorted = Array.from(values).sort((a, b) => a.localeCompare(b, "fr"));
  for (const v of sorted) {
    const opt = document.createElement("option");
    opt.value = v;
    opt.textContent = v;
    selectEl.appendChild(opt);
  }
}

// ---------------------------------------------------------------------
// Autocomplete d'adresse (API Adresse / BAN)
// ---------------------------------------------------------------------

const addressInput = el("address-input");
const autocompleteList = el("autocomplete-list");

const doAutocomplete = debounce(async (query) => {
  if (!query || query.trim().length < 3) {
    autocompleteList.style.display = "none";
    return;
  }
  try {
    const url = `${BAN_SEARCH_URL}?q=${encodeURIComponent(query)}&limit=6`;
    const resp = await fetch(url);
    const data = await resp.json();
    renderAutocomplete(data.features || []);
  } catch (e) {
    console.error("Erreur autocomplete BAN", e);
  }
}, 300);

function renderAutocomplete(features) {
  autocompleteList.innerHTML = "";
  if (!features.length) {
    autocompleteList.style.display = "none";
    return;
  }
  for (const f of features) {
    const div = document.createElement("div");
    div.textContent = f.properties.label;
    div.addEventListener("click", () => {
      const [lon, lat] = f.geometry.coordinates;
      setSearchCenter(lat, lon, f.properties.label);
      addressInput.value = f.properties.label;
      autocompleteList.style.display = "none";
    });
    autocompleteList.appendChild(div);
  }
  autocompleteList.style.display = "block";
}

addressInput.addEventListener("input", (e) => doAutocomplete(e.target.value));
document.addEventListener("click", (e) => {
  if (!e.target.closest(".search-wrap")) autocompleteList.style.display = "none";
});

// ---------------------------------------------------------------------
// Géolocalisation navigateur
// ---------------------------------------------------------------------

el("geoloc-btn").addEventListener("click", () => {
  if (!navigator.geolocation) {
    alert("La géolocalisation n'est pas disponible dans ce navigateur.");
    return;
  }
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      setSearchCenter(pos.coords.latitude, pos.coords.longitude, "Ma position");
      addressInput.value = "Ma position actuelle";
    },
    () => alert("Impossible d'obtenir votre position (autorisation refusée ou indisponible)."),
    { enableHighAccuracy: false, timeout: 8000 }
  );
});

// ---------------------------------------------------------------------
// Rayon
// ---------------------------------------------------------------------

const radiusInput = el("radius-input");
const radiusValue = el("radius-value");
radiusInput.addEventListener("input", () => {
  radiusValue.textContent = `${radiusInput.value} km`;
  render();
});

// Filtres additionnels
el("domain-filter").addEventListener("change", render);
el("contract-filter").addEventListener("change", render);
el("keyword-input").addEventListener("input", debounce(render, 200));

// ---------------------------------------------------------------------
// Centre de recherche
// ---------------------------------------------------------------------

function setSearchCenter(lat, lon, label) {
  searchCenter = { lat, lon, label };

  if (centerMarker) map.removeLayer(centerMarker);
  centerMarker = L.marker([lat, lon], {
    icon: L.divIcon({
      className: "",
      html: '<div style="background:#1f5fa8;width:14px;height:14px;border-radius:50%;border:3px solid white;box-shadow:0 0 4px rgba(0,0,0,0.5);"></div>',
      iconSize: [20, 20],
      iconAnchor: [10, 10],
    }),
  }).addTo(map);

  map.setView([lat, lon], computeZoomForRadius(parseFloat(radiusInput.value)));
  render();
}

function computeZoomForRadius(km) {
  if (km <= 5) return 12;
  if (km <= 15) return 10;
  if (km <= 40) return 9;
  if (km <= 80) return 8;
  if (km <= 150) return 7;
  return 6;
}

// ---------------------------------------------------------------------
// Rendu principal : filtrage + carte + liste
// ---------------------------------------------------------------------

function render() {
  const radiusKm = parseFloat(radiusInput.value);
  const domainFilter = el("domain-filter").value;
  const contractFilter = el("contract-filter").value;
  const keyword = el("keyword-input").value.trim().toLowerCase();

  if (radiusCircle) map.removeLayer(radiusCircle);
  markerClusterGroup.clearLayers();
  markersById.clear();

  const resultsList = el("results-list");
  resultsList.innerHTML = "";

  if (!searchCenter) {
    resultsList.innerHTML = '<div id="empty-state">Choisissez un centre de recherche (adresse ou position actuelle) pour afficher les offres à proximité.</div>';
    el("results-count").textContent = "0";
    return;
  }

  radiusCircle = L.circle([searchCenter.lat, searchCenter.lon], {
    radius: radiusKm * 1000,
    color: "#1f5fa8",
    weight: 1,
    fillOpacity: 0.06,
  }).addTo(map);

  let matches = [];
  for (const o of allOffers) {
    if (o.lat == null || o.lon == null) continue;
    const dist = haversineKm(searchCenter.lat, searchCenter.lon, o.lat, o.lon);
    if (dist > radiusKm) continue;
    if (domainFilter && o.domaine !== domainFilter) continue;
    if (contractFilter && o.contrat !== contractFilter) continue;
    if (keyword) {
      const hay = `${o.titre || ""} ${o.employeur || ""}`.toLowerCase();
      if (!hay.includes(keyword)) continue;
    }
    matches.push({ ...o, _dist: dist });
  }
  matches.sort((a, b) => a._dist - b._dist);

  el("results-count").textContent = matches.length;

  if (matches.length === 0) {
    resultsList.innerHTML = '<div id="empty-state">Aucune offre dans ce rayon avec ces filtres. Essayez d\'élargir le rayon ou de retirer un filtre.</div>';
    return;
  }

  for (const o of matches) {
    const marker = L.marker([o.lat, o.lon]);
    marker.bindPopup(buildPopupHtml(o));
    marker.on("click", () => setActiveCard(o.id));
    markerClusterGroup.addLayer(marker);
    markersById.set(o.id, marker);

    resultsList.appendChild(buildResultCard(o));
  }
}

function buildPopupHtml(o) {
  const parts = [];
  parts.push(`<div class="popup-title">${escapeHtml(o.titre || "Offre")}</div>`);
  if (o.employeur) parts.push(`<div class="popup-employer">${escapeHtml(o.employeur)}</div>`);
  if (o.ville) parts.push(`<div>${escapeHtml(o.ville)}${o.code_postal ? " (" + o.code_postal + ")" : ""}</div>`);
  if (o.contrat) parts.push(`<div>${escapeHtml(o.contrat)}</div>`);
  if (o.url) parts.push(`<a class="popup-link" href="${escapeAttr(o.url)}" target="_blank" rel="noopener">Voir l'offre &rarr;</a>`);
  return parts.join("");
}

function buildResultCard(o) {
  const card = document.createElement("div");
  card.className = "result-card";
  card.dataset.id = o.id;
  card.innerHTML = `
    <div class="title">${escapeHtml(o.titre || "Offre")}</div>
    <div class="employer">${escapeHtml(o.employeur || "")}</div>
    <div class="meta">
      <span>${escapeHtml(o.ville || "")} · ${o._dist.toFixed(1)} km</span>
      ${o.contrat ? `<span class="badge">${escapeHtml(o.contrat)}</span>` : ""}
    </div>
  `;
  card.addEventListener("click", () => {
    map.panTo([o.lat, o.lon]);
    const marker = markersById.get(o.id);
    if (marker) {
      markerClusterGroup.zoomToShowLayer(marker, () => marker.openPopup());
    }
    setActiveCard(o.id);
  });
  return card;
}

function setActiveCard(id) {
  activeCardId = id;
  document.querySelectorAll(".result-card").forEach((c) => {
    c.classList.toggle("active", c.dataset.id === String(id));
  });
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function escapeAttr(str) { return escapeHtml(str); }

// ---------------------------------------------------------------------
// Sidebar mobile toggle
// ---------------------------------------------------------------------

function setupMobileToggle() {
  const toggle = el("sidebar-toggle");
  const sidebar = el("sidebar");
  const applyVisibility = () => {
    if (window.innerWidth <= 760) {
      toggle.style.display = "block";
    } else {
      toggle.style.display = "none";
      sidebar.classList.remove("open");
    }
  };
  toggle.addEventListener("click", () => sidebar.classList.toggle("open"));
  window.addEventListener("resize", applyVisibility);
  applyVisibility();
}

setupMobileToggle();
loadOffers();
