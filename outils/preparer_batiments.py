# -*- coding: utf-8 -*-
"""
Prépare les bâtiments « extrudés » de la vue 3D : une emprise au sol et une hauteur par bâtiment du modèle.

Usage :  python outils/preparer_batiments.py      (depuis le dossier du projet)

IMPORTANT : ce fichier sert UNIQUEMENT à l'affichage en 3D. Il ne change ni la liste des bâtiments du
modèle, ni leur décompte : un bâtiment reste « touché » quand son centre est dans une cellule en eau.

Ce que fait le script
  1. il lit dans index.html la liste des bâtiments du modèle (un point par bâtiment, rang j) ;
  2. il télécharge les emprises des bâtiments de la BD TOPO (IGN) sur l'emprise de la grille ;
  3. il rattache chaque bâtiment du modèle à l'emprise qui contient son point (à défaut, à l'emprise
     la plus proche, à moins de 10 m) ;
  4. il écrit tuiles/batiments_extrudes/batiments.json : une emprise par bâtiment rattaché, avec
       id = rang j du bâtiment dans le modèle (le lien avec les résultats),
       h  = hauteur en mètres (BD TOPO), hd = 1 si la hauteur est inconnue et remplacée par 3 m,
       c  = identifiant BD TOPO (cleabs), qui permettra de faire le lien avec des modèles 3D détaillés.
     Les bâtiments non rattachés restent affichés sous forme de points dans la vue 3D.

Dépendances : geopandas (et donc shapely, pandas).
"""
import argparse
import datetime
import json
import os
import re
import time
import urllib.parse
import urllib.request

import geopandas as gpd
import pandas as pd
from shapely.geometry import shape, mapping

WFS = "https://data.geopf.fr/wfs/ows"
COUCHE = "BDTOPO_V3:batiment"
PAGE = 5000              # nombre d'emprises par requête
HAUTEUR_DEFAUT = 3.0     # m, quand la BD TOPO ne donne pas de hauteur
DISTANCE_MAX = 10.0      # m, pour rattacher un point qui tombe juste à côté d'une emprise


def telecharger(emprise, debut):
    """Une page d'emprises (GeoJSON) dans le rectangle (ouest, sud, est, nord)."""
    q = dict(SERVICE="WFS", VERSION="2.0.0", REQUEST="GetFeature", TYPENAMES=COUCHE, SRSNAME="EPSG:4326",
             BBOX="%f,%f,%f,%f,EPSG:4326" % emprise, COUNT=PAGE, STARTINDEX=debut, SORTBY="cleabs",
             OUTPUTFORMAT="application/json")
    url = WFS + "?" + urllib.parse.urlencode(q)
    for essai in range(6):
        try:
            d = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "python-urllib"}), timeout=180).read()
            return json.loads(d)["features"]
        except Exception as e:                              # coupure réseau, serveur occupé...
            print("   nouvel essai dans %d s (%s)" % (5 * (essai + 1), e))
            time.sleep(5 * (essai + 1))
    raise SystemExit("téléchargement impossible")


def a_plat(coords):
    """Retire l'altitude des sommets et arrondit à 6 décimales (environ 10 cm)."""
    if isinstance(coords[0], (int, float)):
        return [round(coords[0], 6), round(coords[1], 6)]
    return [a_plat(c) for c in coords]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", default="index.html", help="page contenant les bâtiments du modèle")
    ap.add_argument("--sortie", default="tuiles/batiments_extrudes")
    ap.add_argument("--cache", default="outils/cache_batiments/bdtopo.json", help="emprises téléchargées (réutilisées si on relance)")
    a = ap.parse_args()

    # ---------- 1. bâtiments du modèle ----------
    html = open(a.page, encoding="utf-8").read()
    D = json.loads(re.search(r'<script type="application/json" id="donnees">(.*?)</script>', html, re.S).group(1))
    B = D["bat"]
    nb = len(B["cell"])
    (sud, ouest), (nord, est) = D["bounds"]
    print("%d bâtiments dans le modèle" % nb)

    # ---------- 2. emprises de la BD TOPO, par rectangles de 6 x 4 ----------
    if os.path.exists(a.cache):
        brut = json.load(open(a.cache, encoding="utf-8"))
    else:
        vus = {}
        for iy in range(4):
            for ix in range(6):
                rect = (ouest + (est - ouest) * ix / 6, sud + (nord - sud) * iy / 4,
                        ouest + (est - ouest) * (ix + 1) / 6, sud + (nord - sud) * (iy + 1) / 4)
                debut = 0
                while True:
                    lot = telecharger(rect, debut)
                    for f in lot:
                        p = f["properties"]
                        vus[p["cleabs"]] = dict(c=p["cleabs"], h=p.get("hauteur"), zs=p.get("altitude_minimale_sol"),
                                                zt=p.get("altitude_maximale_toit"), log=p.get("nombre_de_logements"),
                                                usage=p.get("usage_1"), etat=p.get("etat_de_l_objet"), g=a_plat(f["geometry"]["coordinates"]),
                                                t=f["geometry"]["type"])
                    debut += len(lot)
                    if len(lot) < PAGE:
                        break
                print("rectangle %d/24 : %d emprises au total" % (iy * 6 + ix + 1, len(vus)))
        brut = list(vus.values())
        os.makedirs(os.path.dirname(a.cache), exist_ok=True)
        json.dump(brut, open(a.cache, "w", encoding="utf-8"), ensure_ascii=False)
    print("%d emprises BD TOPO sur l'emprise de la grille" % len(brut))

    # ---------- 3. rattachement point du modèle -> emprise ----------
    emprises = gpd.GeoDataFrame(pd.DataFrame([{k: v for k, v in b.items() if k not in ("g", "t")} for b in brut]),
                                geometry=[shape(dict(type=b["t"], coordinates=b["g"])) for b in brut], crs=4326).to_crs(2154)
    emprises["geometry"] = emprises.geometry.buffer(0)       # répare les rares polygones mal formés
    emprises["aire"] = emprises.geometry.area
    pts = gpd.GeoDataFrame(dict(j=range(nb)), geometry=gpd.points_from_xy(B["lon"], B["lat"]), crs=4326).to_crs(2154)
    dedans = gpd.sjoin(pts, emprises[["aire", "geometry"]], predicate="within").sort_values("aire").drop_duplicates("j")
    lien = dict(zip(dedans.j, dedans.index_right))           # si plusieurs emprises contiennent le point : la plus petite
    reste = pts[~pts.j.isin(lien)]
    proches = gpd.sjoin_nearest(reste, emprises[["geometry"]], max_distance=DISTANCE_MAX, distance_col="d").drop_duplicates("j")
    n_proches = len(proches)
    lien.update(zip(proches.j, proches.index_right))
    # une emprise ne représente qu'un seul bâtiment du modèle
    prise, final = set(), {}
    for j in sorted(lien):
        if lien[j] not in prise:
            prise.add(lien[j])
            final[j] = lien[j]
    print("rattachés : %d sur %d (dont %d par proximité) ; %d restent en points" % (len(final), nb, n_proches, nb - len(final)))

    # contrôle de vraisemblance : même nombre de logements des deux côtés ?
    logs = [(B["log"][j], emprises.at[i, "log"]) for j, i in final.items() if pd.notna(emprises.at[i, "log"])]
    meme_log = sum(1 for x, y in logs if x == y) / max(1, len(logs))
    print("contrôle : même nombre de logements pour %.1f %% des bâtiments rattachés" % (100 * meme_log))

    # ---------- 4. écriture ----------
    wgs = emprises.geometry.to_crs(4326)
    features, sans_hauteur = [], 0
    for j, i in sorted(final.items()):
        e = emprises.loc[i]
        h = e.h
        if pd.isna(h) or h <= 0:
            h = (e.zt - e.zs) if pd.notna(e.zt) and pd.notna(e.zs) and e.zt > e.zs else None
        props = dict(c=e.c, h=round(float(h), 1) if h else HAUTEUR_DEFAUT)
        if not h:
            props["hd"] = 1
            sans_hauteur += 1
        geom = mapping(wgs.loc[i])
        features.append(dict(type="Feature", id=int(j), properties=props,
                             geometry=dict(type=geom["type"], coordinates=a_plat(json.loads(json.dumps(geom["coordinates"]))))))
    os.makedirs(a.sortie, exist_ok=True)
    fichier = a.sortie + "/batiments.json"
    json.dump(dict(type="FeatureCollection", features=features), open(fichier, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    fiche = dict(source="BD TOPO - IGN (Géoplateforme, couche %s)" % COUCHE, licence="Licence Ouverte Etalab 2.0",
                 date=datetime.date.today().strftime("%d/%m/%Y"), batiments_modele=nb, rattaches=len(final),
                 dont_par_proximite=int(n_proches), en_points=nb - len(final), hauteur_inconnue=sans_hauteur,
                 hauteur_defaut_m=HAUTEUR_DEFAUT, meme_nombre_logements_pct=round(100 * meme_log, 1),
                 usage="Affichage en 3D uniquement. Le décompte des bâtiments touchés reste celui du modèle (centre du bâtiment).")
    json.dump(fiche, open(a.sortie + "/fiche.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("hauteur inconnue (remplacée par %.0f m) : %d" % (HAUTEUR_DEFAUT, sans_hauteur))
    print("écrit : %s, %.1f Mo" % (fichier, os.path.getsize(fichier) / 1e6))


if __name__ == "__main__":
    main()
