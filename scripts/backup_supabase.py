#!/usr/bin/env python3
"""
Sauvegarde quotidienne de Supabase (Bar A Textile).

Exporte les tables `produits` et `categories` en JSON, et mirrore le bucket
Storage `images`. Lecture seule : ce script n'ecrit jamais dans Supabase.

Utilise uniquement la bibliotheque standard (pas de pip install) pour rester
simple a maintenir dans le temps.

Usage : python backup_supabase.py <dossier_de_sortie>
Variables d'environnement requises :
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY  (la "secret key" du projet, jamais l'anon/publishable key)
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
# .strip() : un copier-coller depuis un dashboard ajoute parfois un retour a
# la ligne invisible en fin de valeur, ce qui suffit a invalider la cle.
SERVICE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"].strip()
BUCKET = "images"

OUT_DIR = sys.argv[1] if len(sys.argv) > 1 else "backup_output"
IMAGES_DIR = os.path.join(OUT_DIR, "images")

HEADERS = {
    "apikey": SERVICE_KEY,
    "Content-Type": "application/json",
}
# Important : avec le nouveau systeme de cles Supabase (sb_secret_...), la cle
# se transmet UNIQUEMENT via l'en-tete "apikey". L'envoyer aussi dans
# "Authorization: Bearer" fait que Supabase tente de la lire comme un JWT et
# renvoie 401 Unauthorized (c'est l'ancien systeme service_role qui exigeait
# les deux en-tetes).


def http_request(url, method="GET", data=None, headers=None):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers=headers or HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        # Supabase renvoie un message JSON utile (ex: "Invalid API key") dans
        # le corps de la reponse d'erreur -- urllib ne l'affiche pas par
        # defaut, on va le chercher explicitement pour un vrai diagnostic.
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} sur {url}\nReponse de Supabase : {detail}") from None


def fetch_table(table):
    """Recupere toutes les lignes d'une table via l'API REST (pagination par pages de 1000)."""
    rows = []
    page_size = 1000
    offset = 0
    while True:
        url = f"{SUPABASE_URL}/rest/v1/{table}?select=*&order=id"
        headers = dict(HEADERS)
        headers["Range-Unit"] = "items"
        headers["Range"] = f"{offset}-{offset + page_size - 1}"
        page = json.loads(http_request(url, headers=headers))
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return rows


def list_bucket_files():
    """Liste tous les fichiers du bucket (pagination par pages de 1000)."""
    files = []
    limit = 1000
    offset = 0
    while True:
        url = f"{SUPABASE_URL}/storage/v1/object/list/{BUCKET}"
        payload = {
            "prefix": "",
            "limit": limit,
            "offset": offset,
            "sortBy": {"column": "name", "order": "asc"},
        }
        page = json.loads(http_request(url, method="POST", data=payload))
        page = [f for f in page if f.get("id")]  # ignore les entrees "dossier"
        files.extend(page)
        if len(page) < limit:
            break
        offset += limit
    return files


def download_file(name, dest_path):
    # Le bucket est public en lecture : pas besoin d'auth pour le telechargement.
    url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{name}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        data = resp.read()
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "wb") as f:
        f.write(data)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(IMAGES_DIR, exist_ok=True)

    # Diagnostic sans danger : juste de quoi verifier la forme de la cle,
    # jamais la cle entiere.
    prefix = SERVICE_KEY[:11] if SERVICE_KEY else "(VIDE)"
    print(f"Cle recue : {prefix}... (longueur {len(SERVICE_KEY)})")

    print("Export table produits...")
    produits = fetch_table("produits")
    with open(os.path.join(OUT_DIR, "produits.json"), "w", encoding="utf-8") as f:
        json.dump(produits, f, ensure_ascii=False, indent=2)
    print(f"  {len(produits)} produits exportes.")

    print("Export table categories...")
    categories = fetch_table("categories")
    with open(os.path.join(OUT_DIR, "categories.json"), "w", encoding="utf-8") as f:
        json.dump(categories, f, ensure_ascii=False, indent=2)
    print(f"  {len(categories)} categories exportees.")

    print("Liste des photos dans le bucket...")
    files = list_bucket_files()
    print(f"  {len(files)} fichiers a mirrorer.")

    errors = []
    for i, entry in enumerate(files, 1):
        name = entry["name"]
        dest = os.path.join(IMAGES_DIR, name)
        try:
            download_file(name, dest)
        except Exception as exc:
            errors.append((name, str(exc)))
        if i % 50 == 0:
            print(f"  ... {i}/{len(files)}")

    with open(os.path.join(OUT_DIR, "last_backup.txt"), "w", encoding="utf-8") as f:
        f.write(f"Derniere sauvegarde : {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"Produits : {len(produits)}\n")
        f.write(f"Categories : {len(categories)}\n")
        f.write(f"Photos : {len(files)} ({len(errors)} erreur(s))\n")
        if errors:
            f.write("\nErreurs de telechargement :\n")
            for name, err in errors:
                f.write(f"  - {name}: {err}\n")

    ok_count = len(files) - len(errors)
    print(f"Termine. {len(produits)} produits, {len(categories)} categories, "
          f"{ok_count}/{len(files)} photos.")

    if errors:
        print(f"::warning::{len(errors)} photo(s) n'ont pas pu etre telechargees "
              f"(detail dans last_backup.txt).")
    if files and len(errors) == len(files):
        print("::error::Aucune photo n'a pu etre telechargee, la sauvegarde "
              "est probablement cassee.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
