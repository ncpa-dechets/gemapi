# -*- coding: utf-8 -*-
"""
Prépare le relief de la vue 3D : télécharge le MNT LiDAR HD de l'IGN et le découpe en « tuiles » PNG.

Usage :  python outils/preparer_mnt.py            (depuis le dossier du projet)
         python outils/preparer_mnt.py --zmax 15  (relief deux fois plus fin, environ quatre fois plus lourd)

IMPORTANT : ce relief sert UNIQUEMENT à l'affichage en 3D. Le modèle de submersion continue d'utiliser
ses propres altitudes (grille gZ, issue du RGE ALTI). Ce script ne modifie ni index.html ni le modèle.

Ce que fait le script
  1. il lit l'emprise de la grille du modèle dans index.html ;
  2. il demande les altitudes au service WMS de l'IGN, par blocs, directement dans la projection des
     cartes web (Web Mercator), ce qui évite toute reprojection ;
  3. il fabrique les zooms moins détaillés en moyennant les pixels 2 par 2 ;
  4. il écrit chaque tuile en PNG au format « Terrarium », que MapLibre sait lire :
         altitude (m) = (rouge x 256 + vert + bleu / 256) - 32768
  5. il écrit tuiles/mnt/mnt.json, la fiche d'identité du relief (emprise, zooms, source, date).

GitHub Pages ne sert que des fichiers : il suffit de publier le dossier tuiles/ avec le reste du site.

Dépendances : numpy, pillow.
"""
import argparse
import datetime
import json
import math
import os
import re
import time
import urllib.parse
import urllib.request

import numpy as np
from PIL import Image

WMS = "https://data.geopf.fr/wms-r/wms"
COUCHE = "IGNF_LIDAR-HD_MNT_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93"   # MNT issu du LiDAR HD (IGN)
TUILE = 512          # côté d'une tuile, en pixels
BLOC = 4             # on télécharge 4 x 4 tuiles par requête (2048 x 2048 pixels)
PAS_ALT = 0.1        # altitudes arrondies à 10 cm : fichiers beaucoup plus légers, sans effet visible
DEMI_TOUR = math.pi * 6378137     # demi-circonférence de la Terre en Web Mercator (m)


def tuile_de(lat, lon, z):
    """Numéro (colonne, ligne) de la tuile de zoom z qui contient le point."""
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def emprise_tuiles(x0, y0, nx, ny, z):
    """Emprise Web Mercator (xmin, ymin, xmax, ymax) d'un rectangle de nx x ny tuiles."""
    cote = 2 * DEMI_TOUR / 2 ** z
    return (-DEMI_TOUR + x0 * cote, DEMI_TOUR - (y0 + ny) * cote, -DEMI_TOUR + (x0 + nx) * cote, DEMI_TOUR - y0 * cote)


def telecharger(emprise, largeur, hauteur):
    """Altitudes (m NGF) d'un rectangle, sous forme de tableau hauteur x largeur. Réessaie en cas d'échec."""
    q = dict(SERVICE="WMS", VERSION="1.3.0", REQUEST="GetMap", LAYERS=COUCHE, STYLES="", CRS="EPSG:3857",
             BBOX=",".join("%.3f" % v for v in emprise), WIDTH=largeur, HEIGHT=hauteur, FORMAT="image/x-bil;bits=32")
    url = WMS + "?" + urllib.parse.urlencode(q)
    for essai in range(6):
        try:
            d = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "python-urllib"}), timeout=180).read()
            if len(d) == largeur * hauteur * 4:
                return np.frombuffer(d, "<f4").reshape(hauteur, largeur)
            erreur = "réponse inattendue : %r" % d[:120]
        except Exception as e:                              # coupure réseau, serveur occupé...
            erreur = str(e)
        print("   nouvel essai dans %d s (%s)" % (5 * (essai + 1), erreur))
        time.sleep(5 * (essai + 1))
    raise SystemExit("téléchargement impossible : " + erreur)


def ecrire_tuile(alt, chemin):
    """Écrit un carré d'altitudes en PNG Terrarium."""
    v = np.round(alt / PAS_ALT) * PAS_ALT + 32768.0
    ent = np.floor(v)
    rvb = np.dstack([ent // 256, ent % 256, np.floor((v - ent) * 256)]).astype(np.uint8)
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    Image.fromarray(rvb, "RGB").save(chemin, optimize=True)
    return os.path.getsize(chemin)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", default="index.html", help="page contenant l'emprise de la grille du modèle")
    ap.add_argument("--sortie", default="tuiles/mnt")
    ap.add_argument("--cache", default="outils/cache_mnt", help="blocs téléchargés (réutilisés si on relance le script)")
    ap.add_argument("--zmax", type=int, default=14, help="zoom le plus détaillé (14 : 1 pixel = environ 3 m sur le terrain)")
    ap.add_argument("--zmin", type=int, default=9)
    a = ap.parse_args()

    # ---------- 1. emprise de la grille du modèle ----------
    html = open(a.page, encoding="utf-8").read()
    m = re.search(r'"bounds":\[\[([-\d.]+),([-\d.]+)\],\[([-\d.]+),([-\d.]+)\]\]', html)
    if not m:
        raise SystemExit("emprise (bounds) introuvable dans " + a.page)
    sud, ouest, nord, est = map(float, m.groups())
    z = a.zmax
    x0, y0 = tuile_de(nord, ouest, z)
    x1, y1 = tuile_de(sud, est, z)
    nx, ny = x1 - x0 + 1, y1 - y0 + 1
    pixel_m = 2 * DEMI_TOUR / 2 ** z / TUILE * math.cos(math.radians((sud + nord) / 2))
    print("zoom %d : %d x %d tuiles, 1 pixel = %.1f m sur le terrain" % (z, nx, ny, pixel_m))

    # ---------- 2. téléchargement par blocs ----------
    os.makedirs(a.cache, exist_ok=True)
    A = np.zeros((ny * TUILE, nx * TUILE), np.float32)
    blocs = [(bx, by) for by in range(0, ny, BLOC) for bx in range(0, nx, BLOC)]
    for k, (bx, by) in enumerate(blocs):
        w, h = min(BLOC, nx - bx), min(BLOC, ny - by)
        fichier = "%s/z%d_%d_%d_%dx%d.npy" % (a.cache, z, x0 + bx, y0 + by, w, h)
        if os.path.exists(fichier):
            bloc = np.load(fichier)
        else:
            print("bloc %d/%d" % (k + 1, len(blocs)))
            bloc = telecharger(emprise_tuiles(x0 + bx, y0 + by, w, h, z), w * TUILE, h * TUILE)
            np.save(fichier, bloc)
        A[by * TUILE:(by + h) * TUILE, bx * TUILE:(bx + w) * TUILE] = bloc
    sans_donnee = A < -9000                                  # en mer, le service renvoie -9999
    for _ in range(4):                                       # on élargit la zone de 4 pixels dans toutes les directions :
        e = sans_donnee.copy()                               # au bord, le service mélange -9999 et vraies altitudes
        e[1:] |= sans_donnee[:-1]; e[:-1] |= sans_donnee[1:]            # (valeurs aberrantes)
        f = e.copy()
        f[:, 1:] |= e[:, :-1]; f[:, :-1] |= e[:, 1:]
        sans_donnee = f
    A[sans_donnee] = 0                                       # la mer est représentée à 0 m NGF
    print("altitudes : %.1f à %.1f m NGF, %.0f %% sans donnée (mer)" % (A.min(), A.max(), 100 * sans_donnee.mean()))

    # ---------- 3 et 4. tuiles de chaque zoom, du plus détaillé au moins détaillé ----------
    total, nombre = 0, 0
    for zz in range(z, a.zmin - 1, -1):
        for ty in range(A.shape[0] // TUILE):
            for tx in range(A.shape[1] // TUILE):
                carre = A[ty * TUILE:(ty + 1) * TUILE, tx * TUILE:(tx + 1) * TUILE]
                total += ecrire_tuile(carre, "%s/%d/%d/%d.png" % (a.sortie, zz, x0 + tx, y0 + ty))
                nombre += 1
        # zoom suivant : on complète pour avoir un nombre pair de tuiles alignées, puis on moyenne 2 par 2
        g, h_ = (x0 % 2) * TUILE, (y0 % 2) * TUILE
        A = np.pad(A, ((h_, 0), (g, 0)))
        A = np.pad(A, ((0, A.shape[0] % (2 * TUILE)), (0, A.shape[1] % (2 * TUILE))))
        A = A.reshape(A.shape[0] // 2, 2, A.shape[1] // 2, 2).mean(axis=(1, 3))
        x0, y0 = x0 // 2, y0 // 2

    # ---------- 5. fiche d'identité ----------
    fiche = dict(source="MNT LiDAR HD - IGN (Géoplateforme, couche %s)" % COUCHE, licence="Licence Ouverte Etalab 2.0",
                 date=datetime.date.today().strftime("%d/%m/%Y"), encodage="terrarium", tuile=TUILE,
                 zmin=a.zmin, zmax=a.zmax, emprise=[ouest, sud, est, nord], pixel_m=round(pixel_m, 1), pas_altitude_m=PAS_ALT,
                 usage="Affichage du relief en 3D uniquement. Le modèle de submersion utilise ses propres altitudes (RGE ALTI).")
    json.dump(fiche, open(a.sortie + "/mnt.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("écrit : %d tuiles dans %s, %.1f Mo" % (nombre, a.sortie, total / 1e6))


if __name__ == "__main__":
    main()
