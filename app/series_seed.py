"""Données de référence des séries (noms officiels + taille du set), fournies par l'utilisateur.

Évite de dépendre d'un appel réseau pour afficher des noms corrects et le bon
dénominateur de complétion dès le premier rendu.
"""

SERIES_SEED: dict[str, dict] = {
    "ancient-civilizations": {"name": "Lost Empires",        "size": 200},
    "architects-of-worlds":  {"name": "Architects of Worlds", "size": 199},
    "famous-women":          {"name": "Trailblazers",        "size": 200},
    "full-throttle":         {"name": "Full Throttle",       "size": 200},
    "masterworks":           {"name": "Masterworks",         "size": 200},
    "mystery":               {"name": "???",                 "size": 50},
    "myths-legends":         {"name": "Myths & Legends",     "size": 200},
    "natural-world":         {"name": "Untamed Wilds",       "size": 200},
    "primeval":              {"name": "Primeval",            "size": 200},
    "science-discovery":     {"name": "Eureka Archives",     "size": 200},
    "space-cosmos":          {"name": "Celestial Vault",     "size": 200},
    "world-culture":         {"name": "Living Heritage",     "size": 168},
}

# Palette de repli par série (couleurs réelles écrasées dès la 1re ouverture via /api/packs/open).
DEFAULT_PRIMARY = "#8b6914"
DEFAULT_ACCENT = "#d4a843"
