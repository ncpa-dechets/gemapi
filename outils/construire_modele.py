# -*- coding: utf-8 -*-
"""
Construit le modèle de submersion « calé sur le PPRL » et l'injecte dans index.html.

Usage :  python outils/construire_modele.py [--source ancien_index.html] [--cible index.html]

Entrées
  - grilles déjà embarquées dans la page (gW : seuil statique d'atteinte, gZ : altitude, gC : commune),
    issues du RGE ALTI (voir « Méthode et sources » dans l'outil) ;
  - data/*.geojson : couches du PPRL de l'estuaire de la Dives (approuvé le 10/08/2021) ;
  - niveaux marins des rapports (rapports/*.pdf) : voir PARAM ci-dessous.

Principe
  niveau d'eau local = niveau marin à la côte - perte de charge locale
  La perte de charge est déduite des deux scénarios modélisés du PPRL (5,10 et 5,50 m NGF),
  interpolée entre les deux, prolongée en dessous (translation) et au-dessus (retour progressif
  à la projection statique pour l'événement extrême du TRI, 8,2 m NGF).

Dépendances : numpy, scipy, geopandas, rasterio.
"""
import argparse
import base64
import datetime
import gzip
import json
import re
import sys
import warnings

import numpy as np
from scipy import ndimage as ndi

warnings.filterwarnings("ignore")

PARAM = dict(
    L20=5.10,    # PPRL : niveau marin de référence (centennal 4,90 m + 20 cm) - rapport TRI 2014, ill. 11
    L60=5.50,    # PPRL : échéance 100 ans (centennal 4,90 m + 60 cm)
    LX=8.20,     # TRI : événement extrême 8,21 m = PHMA 4,75 + surcote millénale 2,91 + houle 0,55 (annexe 7)
    LNAT=3.4,    # seuil statique sous lequel le terrain est naturellement maritime (lit, estran, plages)
    LPROT=4.15,  # niveau de protection minimal des digues (étude de dangers de Cabourg : 4,16 m, Xynthia)
    DELTA=0.10,  # terrain sec dans un scénario PPRL : niveau local au moins 0,10 m sous le sol...
    KAPPA=2.0,   # ... et décroissant de 2 m par km en s'éloignant de la zone inondée du scénario
    PMAX=6.0,    # perte de charge maximale (m)
    DECAL_ORNE=0.40,  # TRI : niveaux de référence de la cellule ouest (estuaire de l'Orne) inférieurs de 0,40 m
    NIV0=2.0, NIV1=8.3, PAS=0.05,
)
ALEA = {"Fai": 1, "M": 2, "F": 3, "TF": 4}
PROF = np.array([0, 0.25, 0.75, 1.25, 0], np.float32)   # hauteur d'eau représentative par classe d'aléa
POIDS = np.array([0, 1, 1, 0.5, 0], np.float32)
ZONES = {"Rs": 1, "O": 2, "J": 3, "B1": 4, "B2": 5, "V": 6}
ECHELLE_P = 40                                          # pertes de charge stockées au pas de 2,5 cm


def bloc(html, ident, typ):
    m = re.search(r'<script type="%s" id="%s">(.*?)</script>' % (typ, ident), html, re.S)
    return m.group(1) if m else None


def remplacer(html, ident, typ, contenu):
    motif = re.compile(r'(<script type="%s" id="%s">)(.*?)(</script>)' % (typ, ident), re.S)
    if not motif.search(html):
        sys.exit("bloc <script id=%s> absent de la page cible" % ident)
    return motif.sub(lambda m: m.group(1) + contenu + m.group(3), html, count=1)


def coder(a):
    return base64.b64encode(gzip.compress(np.ascontiguousarray(a).tobytes(), 9, mtime=0)).decode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="index.html", help="page contenant les grilles gW, gZ, gC")
    ap.add_argument("--cible", default="index.html", help="page à mettre à jour")
    ap.add_argument("--donnees", default="data")
    ap.add_argument("--npy", default=None, help="dossier où enregistrer les grilles intermédiaires (contrôle)")
    a = ap.parse_args()
    import geopandas as gpd
    from rasterio import features
    from rasterio.transform import from_origin

    P = PARAM
    src = open(a.source, encoding="utf-8").read()
    D = json.loads(bloc(src, "donnees", "application/json"))
    G = D["grille"]
    NX, NY = G["nx"], G["ny"]

    def dec(ident, dt):
        return np.frombuffer(gzip.decompress(base64.b64decode(bloc(src, ident, "text/plain").strip())), dt).reshape(NY, NX)

    W, Z, C = dec("gW", np.uint16), dec("gZ", np.uint16), dec("gC", np.uint8)
    nivstat = D.get("niveauxStat") or D["niveaux"]        # niveaux de la grille statique gW (pas de 10 cm)
    NS = len(nivstat)
    aire = G["res"] ** 2 / 1e4                            # ha par cellule

    # ---------- couches PPRL sur la grille ----------
    tr = from_origin(G["x0"], G["y1"], G["px"], G["px"])

    def lire(n):
        return gpd.read_file("%s/%s.geojson" % (a.donnees, n))

    def rast(g, col=None, table=None):
        g = g.to_crs(3857)
        vals = [1] * len(g) if col is None else [table.get(v, 0) for v in g[col]]
        sh = sorted([(geom, v) for geom, v in zip(g.geometry, vals) if v], key=lambda s: s[1])  # la classe la plus forte l'emporte
        return features.rasterize(sh, out_shape=(NY, NX), transform=tr, fill=0, dtype="uint8")

    A20 = rast(lire("03_Alea_Submersion_+20cm"), "NIVALEA", ALEA)
    A60 = rast(lire("04_Alea_Submersion_+60cm"), "NIVALEA", ALEA)
    per = lire("01_Perimetre_PPRL")
    PER = rast(per)
    ZON = rast(lire("12_Zonage_Reglementaire"), "CODEZONE", ZONES)
    bp = lire("08_Bande_Precaution")
    BP20, BP60 = rast(bp[bp.ref == 20]), rast(bp[bp.ref == 60])
    rec = lire("07_Alea_Recul_Trait_Cote")
    REC = rast(rec)

    # ---------- grandeurs de base ----------
    hautes = Z == 65535
    z = (Z.astype(np.float32) - 1) / 100 - 10
    z[hautes] = 30
    z[Z == 0] = -5
    mer = W == 0
    land = ~mer
    Wm = np.where(W > NS, 99, np.asarray(nivstat, np.float32)[np.clip(W.astype(int) - 1, 0, NS - 1)])
    nat = land & (Wm <= P["LNAT"])
    L20, L60, LX = P["L20"], P["L60"], P["LX"]
    kap = P["KAPPA"] * G["res"] / 1000.0                  # m par cellule
    wet20 = (A20 > 0) & land
    wet60 = (A60 > 0) & land
    ys, xs = np.where(A60 > 0)
    dom = np.zeros(W.shape, bool)                         # emprise des résultats de la modélisation PPRL
    dom[ys.min():ys.max() + 1, xs.min():xs.max() + 1] = True
    dom |= PER > 0

    # ---------- 1. niveau d'eau local dans chaque scénario PPRL ----------
    def niveau(A, wet):
        v = ((z + PROF[A]) * POIDS[A] * wet).astype(np.float32)
        m = (POIDS[A] * wet).astype(np.float32)
        a1, b1 = ndi.gaussian_filter(v, 10, mode="constant"), ndi.gaussian_filter(m, 10, mode="constant")
        a2, b2 = ndi.gaussian_filter(v, 40, mode="constant"), ndi.gaussian_filter(m, 40, mode="constant")
        H = np.where(b1 > 0.02, a1 / np.maximum(b1, 1e-9), np.where(b2 > 1e-4, a2 / np.maximum(b2, 1e-9), np.nan))
        ii = ndi.distance_transform_edt(~np.isfinite(H), return_distances=False, return_indices=True)
        H = H[ii[0], ii[1]]                               # hors zone renseignée : plus proche valeur
        Hw = np.maximum(H, z + 0.05)                      # inondé au scénario, borné par la classe d'aléa
        Hw = np.where(A == 1, np.minimum(Hw, z + 0.49), Hw)
        Hw = np.where(A == 2, np.minimum(Hw, z + 0.99), Hw)
        d = ndi.distance_transform_edt(~wet).astype(np.float32)   # distance à la zone inondée (cellules)
        Hd = np.minimum(H, z - P["DELTA"]) - kap * d      # sec au scénario
        return np.where(wet, Hw, Hd).astype(np.float32), d

    H20, d20 = niveau(A20, wet20)
    H60, d60 = niveau(A60, wet60)
    H20 = np.minimum(H20, H60 - 0.02)                     # le niveau local ne baisse pas quand la mer monte
    H20 = np.where(wet20, np.maximum(H20, z + 0.03), H20)
    H60 = np.where(wet20, np.maximum(H60, H20 + 0.02), H60)

    # ---------- 2. secs à +20 cm, inondés à +60 cm : progression régulière des surfaces entre les deux ----------
    bB = wet60 & ~wet20
    defi = P["DELTA"] + kap * d20[bB]
    prof = np.maximum(H60[bB] - z[bB], 0.03)
    sc = defi / (defi + prof)                             # d'abord les plus profonds et les plus proches
    rg = np.argsort(np.argsort(sc, kind="stable"), kind="stable") / max(1, sc.size - 1)
    npas = int(round((L60 - L20) / P["PAS"]))
    TB = np.clip(L20 + P["PAS"] * (1 + np.floor(rg * npas)), L20 + P["PAS"], L60)
    fr = np.minimum((TB - P["PAS"] - L20) / (L60 - L20), 0.9)
    H20[bB] = np.minimum(z[bB] - 0.01 - (H60[bB] - z[bB] + 0.01) * fr / (1 - fr), H60[bB] - 0.02)

    P20 = np.clip(L20 - H20, 0, P["PMAX"])
    P60 = np.clip(L60 - H60, 0, P["PMAX"])

    # ---------- 3. hors emprise PPRL : seuil statique d'atteinte sans traverser l'emprise ----------
    Wout = np.full(W.shape, 99, np.float32)
    graine = mer & ~dom
    s8 = np.ones((3, 3), bool)
    for j in range(NS, 0, -1):
        masque = ~dom & (mer | ((W >= 1) & (W <= j)))
        Wout[ndi.binary_propagation(graine, structure=s8, mask=masque) & land] = nivstat[j - 1]
    ouest = np.zeros(W.shape, bool)                       # à l'ouest de l'emprise : cellule « Orne » du TRI
    ouest[:, :xs.min()] = True
    Wout = Wout + np.where(ouest, P["DECAL_ORNE"], 0).astype(np.float32)

    # ---------- 4. seuil T : premier niveau marin pour lequel la cellule est en eau ----------
    niv = np.round(np.arange(P["NIV0"], P["NIV1"] + 1e-6, P["PAS"]), 2)
    NL = len(niv)

    def perte(Lv):
        if Lv <= L20:
            return P20
        if Lv <= L60:
            return P20 + (P60 - P20) * (Lv - L20) / (L60 - L20)
        return P60 * max(0.0, (LX - Lv) / (LX - L60))

    T = np.full(W.shape, 255, np.uint8)
    Tst = np.full(W.shape, 255, np.uint8)
    for i in range(NL - 1, -1, -1):
        Lv = float(niv[i])
        prot = Lv > P["LPROT"] + 1e-6                     # en dessous : seuls le lit et l'estran sont en eau
        ok = land & ~hautes & (Wm <= Lv + 1e-6)
        if prot:
            T[ok & ~nat & (Lv - perte(Lv) > z + 1e-4)] = i + 1
            Tst[~dom & land & ~nat & (Wout <= Lv + 1e-6)] = i + 1
        T[ok & nat] = i + 1
    stat = ~dom & (Tst <= T) & (Tst < 255)                # hors emprise, atteint directement par la mer
    T[stat] = Tst[stat]
    P20[stat] = np.where(ouest[stat], P["DECAL_ORNE"], 0)
    P60[stat] = P20[stat]
    T[mer] = 0
    mode = np.where(dom, 1, np.where(stat, 2, 3)).astype(np.uint16)   # 1 calé PPRL, 2 statique, 3 prolongement
    for Pk in (P20, P60):
        Pk[(T == 255) | mer | nat] = 0                   # lit et estran : hauteur d'eau = niveau marin - sol

    # ---------- 5. contrôles ----------
    cell = np.array(D["bat"]["cell"])
    Tb, Wb = T.ravel()[cell], W.ravel()[cell]
    iL = lambda v: int(np.argmin(np.abs(niv - v))) + 1
    jS = lambda v: int(np.argmin(np.abs(np.asarray(nivstat) - v))) + 1
    ctrl = []
    for Lv, A in ((L20, A20), (L60, A60)):
        mod = (T >= 1) & (T <= iL(Lv))
        ref = (A > 0) & land
        st = (W >= 1) & (W <= jS(Lv))
        ctrl.append(dict(niveau=Lv, pprl_ha=round(float(ref.sum()) * aire), commun_ha=round(float((mod & ref).sum()) * aire),
                         statique_emprise_ha=round(float((st & dom).sum()) * aire),
                         bat_pprl=int((A.ravel()[cell] > 0).sum()),
                         bat_modele_emprise=int(((Tb >= 1) & (Tb <= iL(Lv)) & dom.ravel()[cell]).sum()),
                         bat_modele=int(((Tb >= 1) & (Tb <= iL(Lv))).sum()),
                         bat_statique=int(((Wb >= 1) & (Wb <= jS(Lv))).sum())))
        c = ctrl[-1]
        print("L=%.2f  PPRL %d ha | commun %d ha | PPRL non reproduit %d cellules | bâtiments : PPRL %d, modèle %d (dont emprise %d), statique %d" % (
            Lv, c["pprl_ha"], c["commun_ha"], int((ref & ~mod).sum()), c["bat_pprl"], c["bat_modele"], c["bat_modele_emprise"], c["bat_statique"]))
    print("  L      ha   bât.   (statique :    ha,   bât.)")
    terr = C > 0
    for Lv in (4.0, 4.15, 4.2, 4.6, 4.9, 5.0, 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.8, 6.0, 6.5, 7.0, 7.5, 8.2):
        mod = (T >= 1) & (T <= iL(Lv)) & terr
        print("%5.2f %6d %6d   (%16d, %6d)" % (Lv, mod.sum() * aire, ((Tb >= 1) & (Tb <= iL(Lv))).sum(),
              (((W >= 1) & (W <= jS(Lv))) & terr).sum() * aire, ((Wb >= 1) & (Wb <= jS(Lv))).sum()))

    # ---------- 6. écriture ----------
    u = lambda x: x.astype(np.uint16)
    K = (u(A20) | (u(A60) << 3) | (u(ZON) << 6) | (mode << 9) | (u(BP20) << 11) | (u(BP60) << 12)
         | (u(REC) << 13) | (u(PER > 0) << 14))

    def vect(g, cols=()):
        g = g.copy()
        g["geometry"] = g.geometry.simplify(2)
        fc = json.loads(g.to_crs(4326)[list(cols) + ["geometry"]].to_json())

        def arr(c):
            return [arr(x) for x in c] if isinstance(c[0], list) else [round(c[0], 6), round(c[1], 6)]

        for f in fc["features"]:
            f["geometry"]["coordinates"] = arr(f["geometry"]["coordinates"])
            f.pop("id", None)
        return fc

    D["niveauxStat"] = list(nivstat)
    D["niveaux"] = [float(v) for v in niv]
    D.setdefault("date_donnees", D["date"])
    D["date"] = datetime.datetime.now().strftime("%d/%m/%Y %H:%M")
    D["version"] = "0.2"
    D["modele"] = dict(P, controle=ctrl, echelleP=ECHELLE_P,
                       emprise=[int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1])
    D["pprl"] = dict(perimetre=vect(per, ["LIBELLE"]), bande=vect(bp, ["ID_TRI", "ref"]), recul=vect(rec, ["DESCRIPT"]),
                     communes=sorted(lire("02_Communes_PPRL").NOM_COM.str.title().tolist()))
    out = open(a.cible, encoding="utf-8").read()
    out = remplacer(out, "donnees", "application/json",
                    json.dumps(D, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/"))
    for ident, grille in (("gW", W), ("gZ", Z), ("gC", C), ("gT", T), ("gK", K),
                          ("gP20", np.round(P20 * ECHELLE_P).astype(np.uint8)),
                          ("gP60", np.round(P60 * ECHELLE_P).astype(np.uint8))):
        out = remplacer(out, ident, "text/plain", coder(grille))
    open(a.cible, "w", encoding="utf-8", newline="\n").write(out)
    print("écrit :", a.cible, "%.1f Mo" % (len(out.encode("utf-8")) / 1e6))
    if a.npy:
        for n, v in (("T", T), ("P20", P20), ("P60", P60), ("K", K), ("dom", dom), ("Wout", Wout)):
            np.save("%s/%s.npy" % (a.npy, n), v)
        json.dump({"niv": niv.tolist()}, open(a.npy + "/model.json", "w"))


if __name__ == "__main__":
    main()
