#!/usr/bin/env python3
"""
Sonde du stockage Box — l'identite generique voit-elle ce qu'on partage avec elle ?

STATUT: COMPLET
VERSION: 2026-09-06 - v1.0
LOT: L1.1

CE QU'ON CHERCHE A SAVOIR
--------------------------
Le modele de Colaig est celui du Bnum : on partage un dossier avec lui, il apparait a
la racine de son stockage, et s'il y est editeur il y depose son `.colaig/`.
`run_auto_discover` fait exactement cela — il liste la racine toutes les deux minutes,
prend chaque dossier non cache, et cree le scaffold s'il manque. Rien de tout cela ne
depend du backend : la boucle ne parle qu'a `StorageProtocol`.

Reste une question que le code ne peut pas trancher : **quelle racine Colaig voit-il ?**

`box.py` n'obtient ses jetons que par JWT, et le comportement bascule sur une variable :

    BOX_USER_ID vide       -> Colaig agit comme le SERVICE ACCOUNT de l'application.
                              Sa racine n'est pas celle du compte generique ; les
                              dossiers partages avec ce dernier lui sont invisibles.
    BOX_USER_ID renseigne  -> Colaig agit AU NOM de ce compte, et voit ce qu'il voit.

La seconde voie est celle que la doctrine demande — principe 5 du CLAUDE.md : l'identite
de l'instance est portee par ses identifiants de connexion, une adresse dediee qui ancre
Matrix, le LLM, GitHub, et desormais Box. Un utilisateur qui partage un dossier doit
lire « Assistant Colaig » dans ses collaborateurs, pas `AutomationUser_…@boxdevedition`.

Mais Box n'autorise pas toujours une application a agir au nom d'un compte : cela depend
de la nature du compte et de l'entreprise qui declare l'application. Cette sonde repond
par l'observation plutot que par la documentation.

CE QU'ELLE MESURE
-------------------
  - l'authentification aboutit, et sous quelle identite
  - ce que la RACINE contient — le test qui decide
  - la latence d'un listing, celle d'une lecture
  - l'aller-retour ecriture : creer `.colaig/`, y ecrire, relire, effacer
  - la presence d'un `.colaig/config.yaml` par dossier — quels espaces existent deja

RIEN N'EST DETRUIT : la sonde n'ecrit que sous un nom qui lui est propre, et efface ce
qu'elle a ecrit. Elle ne touche a aucun document.

Usage :
    BOX_CLIENT_ID=… BOX_CLIENT_SECRET=… BOX_ENTERPRISE_ID=… \\
    BOX_PUBLIC_KEY_ID=… BOX_PRIVATE_KEY=… BOX_PASSPHRASE=… \\
    BOX_USER_ID=…  python _chantier/scripts/probe_box.py

    # ou, plus simple, le JSON telecharge depuis la Dev Console :
    BOX_CONFIG_FILE=/chemin/vers/config.json  BOX_USER_ID=…  python …/probe_box.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

RACINE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RACINE))

TEMOIN = ".colaig-sonde-box"


def _config() -> dict:
    """Lit les identifiants, du JSON de la Dev Console ou des variables d'env."""
    chemin = os.environ.get("BOX_CONFIG_FILE", "")
    if chemin:
        brut = json.loads(Path(chemin).read_text(encoding="utf-8"))
        app = brut["boxAppSettings"]
        cle = app["appAuth"]
        return {
            "client_id": app["clientID"],
            "client_secret": app["clientSecret"],
            "enterprise_id": brut.get("enterpriseID", ""),
            "public_key_id": cle["publicKeyID"],
            "private_key": cle["privateKey"],
            "passphrase": cle["passphrase"],
        }
    return {
        "client_id": os.environ.get("BOX_CLIENT_ID", ""),
        "client_secret": os.environ.get("BOX_CLIENT_SECRET", ""),
        "enterprise_id": os.environ.get("BOX_ENTERPRISE_ID", ""),
        "public_key_id": os.environ.get("BOX_PUBLIC_KEY_ID", ""),
        "private_key": os.environ.get("BOX_PRIVATE_KEY", "").replace("\\n", "\n"),
        "passphrase": os.environ.get("BOX_PASSPHRASE", ""),
    }


async def _chronometre(libelle: str, coroutine):
    debut = time.monotonic()
    try:
        valeur = await coroutine
    except Exception as e:  # noqa: BLE001
        print(f"  {libelle:34s} ECHEC  {type(e).__name__}: {str(e)[:110]}")
        return None
    print(f"  {libelle:34s} {1000 * (time.monotonic() - debut):7.0f} ms")
    return valeur


async def principal() -> int:
    from colaig.integrations.storage.box import BoxStorage

    cfg = _config()
    if not cfg["client_id"]:
        print(__doc__)
        return 2

    user_id = os.environ.get("BOX_USER_ID", "")
    racine = os.environ.get("BOX_ROOT_FOLDER_ID", "0")
    print("=" * 72)
    print("identite        :", f"au nom de l'utilisateur {user_id}" if user_id
          else "SERVICE ACCOUNT de l'application (BOX_USER_ID vide)")
    print("dossier racine  :", racine)
    print("=" * 72)

    stockage = BoxStorage(root_folder_id=racine, user_id=user_id, **cfg)

    # 1. LA RACINE — c'est ce listing qui decide.
    print("\nracine")
    entrees = await _chronometre("list_files('/')", stockage.list_files("/"))
    if entrees is None:
        print("\nL'authentification ou le listing a echoue. Trois causes usuelles :")
        print("  - l'application n'est pas autorisee par l'administrateur Box ;")
        print("  - le scope « Write all files and folders » n'est pas coche ;")
        print("  - BOX_USER_ID designe un compte que l'application ne peut pas imiter.")
        return 1

    dossiers = [e for e in entrees if e.is_directory]
    print(f"    {len(entrees)} entrees, dont {len(dossiers)} dossiers")
    for e in entrees[:20]:
        print(f"      {'dossier' if e.is_directory else 'fichier'}  {e.name}")
    if not dossiers:
        print("\n    AUCUN DOSSIER. Si des dossiers ont ete partages avec le compte")
        print("    generique, c'est que Colaig ne regarde pas la meme racine que lui.")

    # 2. Ce que l'auto-decouverte ferait de chaque dossier.
    if dossiers:
        from colaig import paths
        print("\nespaces (ce que run_auto_discover verrait)")
        for e in dossiers[:20]:
            chemin = e.path.rstrip("/")
            try:
                deja = await stockage.exists(paths.config_file(chemin))
                ignore = await stockage.exists(paths.ignore_file(chemin))
            except Exception:  # noqa: BLE001
                deja = ignore = False
            etat = ("ignore (.colaig-ignore)" if ignore
                    else "espace existant" if deja else "serait cree au prochain tour")
            print(f"      {e.name:44s} {etat}")

    # 3. L'aller-retour d'ecriture — le droit editeur suffit-il ?
    print("\necriture (dans un dossier temoin, efface ensuite)")
    dossier = f"/{TEMOIN}/"
    fichier = f"{dossier}temoin.txt"
    contenu = "sonde colaig - ce fichier est supprime aussitot".encode("utf-8")
    await _chronometre("mkdir", stockage.mkdir(dossier))
    ecrit = await _chronometre("upload", stockage.upload(fichier, contenu))
    if ecrit is not None or await stockage.exists(fichier):
        relu = await _chronometre("download", stockage.download(fichier))
        if relu is not None and relu != contenu:
            print("      [!] le contenu relu differe de ce qui a ete ecrit")
        await _chronometre("delete fichier", stockage.delete(fichier))
    await _chronometre("delete dossier", stockage.delete(dossier))

    print("\nCE QUE CE RELEVE DIT")
    print("  - si la racine porte les dossiers partages, le modele du Bnum tient :")
    print("    on partage, Colaig voit, et l'auto-decouverte fait le reste ;")
    print("  - si l'ecriture aboutit, le droit editeur suffit a deposer le .colaig/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(principal()))
