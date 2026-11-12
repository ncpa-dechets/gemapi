// =====================================================================================================
// BÂTIMENTS 3D DÉTAILLÉS (modèles avec toits) pour la vue 3D
//
// Ce fichier lit le jeu « 3D Tiles » du dossier tuiles/batiments et le dessine dans la carte MapLibre.
// Il ne calcule rien sur la submersion : il reçoit l'état de chaque bâtiment (hors d'eau, touché...) et
// se contente de choisir la couleur. Les résultats du modèle ne dépendent donc pas de lui.
//
// Les grandes étapes :
//   1. listerTuiles  : lit tileset.json et retrouve les fichiers .glb et leur emprise ;
//   2. lireGlb       : ouvre un fichier .glb (format glTF compressé) et en sort les sommets des bâtiments ;
//   3. versCarte     : convertit ces sommets en coordonnées de carte (longitude, latitude, altitude) ;
//   4. creer         : fabrique une « couche personnalisée » MapLibre qui dessine les tuiles proches.
//
// Dépendance : MeshoptDecoder (décompression des .glb), chargé par la page avant ce fichier.
// =====================================================================================================
window.BatimentsDetail = (function(){
  "use strict";
  const RAD = Math.PI/180;

  // ---------- 1. liste des tuiles ----------
  // tileset.json décrit un arbre ; seules les « feuilles » portent un fichier .glb. Certaines branches
  // renvoient vers un autre fichier .json, qu'on lit à son tour.
  async function listerTuiles(urlTileset){
    const tuiles = [];
    let transform = null;                                  // matrice qui place tout le jeu sur la Terre
    async function parcourir(noeud, base){
      if (noeud.transform){
        if (transform) throw new Error("plusieurs transformations dans le jeu de tuiles : cas non prévu");
        transform = noeud.transform;
      }
      const uri = noeud.content && noeud.content.uri;
      if (uri && uri.endsWith(".json")){
        const url = new URL(uri, base).href;
        await parcourir((await (await fetch(url)).json()).root, url);
      } else if (uri){
        const r = noeud.boundingVolume.region;             // ouest, sud, est, nord (radians)
        tuiles.push({url:new URL(uri, base).href, ouest:r[0]/RAD, sud:r[1]/RAD, est:r[2]/RAD, nord:r[3]/RAD});
      }
      for (const enfant of noeud.children || []) await parcourir(enfant, base);
    }
    const url = new URL(urlTileset, location.href).href;
    await parcourir((await (await fetch(url)).json()).root, url);
    if (!transform) throw new Error("transformation absente du jeu de tuiles");
    return {tuiles, transform};
  }

  // ---------- 2. lecture d'un fichier .glb ----------
  const TYPES = {5120:Int8Array, 5121:Uint8Array, 5122:Int16Array, 5123:Uint16Array, 5125:Uint32Array, 5126:Float32Array};
  const NB_COMPOSANTES = {SCALAR:1, VEC2:2, VEC3:3, VEC4:4};
  async function lireGlb(url){
    const octets = new Uint8Array(await (await fetch(url)).arrayBuffer());
    const vue = new DataView(octets.buffer);
    if (vue.getUint32(0, true) !== 0x46546C67) throw new Error("fichier .glb invalide : " + url);
    const tailleJson = vue.getUint32(12, true);
    const json = JSON.parse(new TextDecoder().decode(octets.subarray(20, 20 + tailleJson)));
    const bin = octets.subarray(20 + tailleJson + 8);      // la partie binaire suit la description JSON
    await MeshoptDecoder.ready;
    // octets d'une « bufferView », décompressés si besoin
    function octetsDe(k){
      const bv = json.bufferViews[k], c = bv.extensions && bv.extensions.EXT_meshopt_compression;
      if (!c) return {donnees:bin.subarray(bv.byteOffset || 0, (bv.byteOffset || 0) + bv.byteLength), pas:bv.byteStride};
      const cible = new Uint8Array(c.count * c.byteStride);
      MeshoptDecoder.decodeGltfBuffer(cible, c.count, c.byteStride, bin.subarray(c.byteOffset || 0, (c.byteOffset || 0) + c.byteLength), c.mode, c.filter || "NONE");
      return {donnees:cible, pas:c.byteStride};
    }
    // valeurs d'un « accessor », sous forme de tableau de nombres (sans les octets de remplissage)
    function valeurs(k){
      const a = json.accessors[k], T = TYPES[a.componentType], n = NB_COMPOSANTES[a.type];
      const {donnees, pas} = octetsDe(a.bufferView), taille = T.BYTES_PER_ELEMENT;
      const saut = pas || n*taille, v = new DataView(donnees.buffer, donnees.byteOffset + (a.byteOffset || 0));
      const lire = {5120:"getInt8", 5121:"getUint8", 5122:"getInt16", 5123:"getUint16", 5125:"getUint32", 5126:"getFloat32"}[a.componentType];
      const sortie = new T(a.count*n);
      for (let i=0;i<a.count;i++) for (let c=0;c<n;c++) sortie[i*n+c] = v[lire](i*saut + c*taille, true);
      return {v:sortie, n:a.count, normalise:!!a.normalized, max:T === Int16Array ? 32767 : T === Int8Array ? 127 : 1};
    }
    if (json.meshes.length !== 1 || json.meshes[0].primitives.length !== 1 || json.nodes.length !== 1)
      throw new Error("structure de .glb non prévue : " + url);
    const prim = json.meshes[0].primitives[0];
    const pos = valeurs(prim.attributes.POSITION), nor = valeurs(prim.attributes.NORMAL), ind = valeurs(prim.indices);
    const idBat = valeurs(prim.attributes._FEATURE_ID_0).v;                // pour chaque sommet : numéro du bâtiment dans la tuile
    // identifiants BD TOPO (cleabs) des bâtiments de la tuile : des textes de 24 caractères mis bout à bout
    const table = json.extensions.EXT_structural_metadata.propertyTables[0];
    const texte = new TextDecoder().decode(octetsDe(table.properties.cleabs.values).donnees);
    const cleabs = [];
    for (let i=0;i<table.count;i++) cleabs.push(texte.substr(i*24, 24));
    return {pos, nor, indices:ind.v, idBat, cleabs, matrice:json.nodes[0].matrix || [1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]};
  }

  // ---------- 3. des coordonnées du fichier aux coordonnées de la carte ----------
  // Chaîne : sommet du fichier -> repère local du jeu (mètres) -> repère centré sur la Terre (ECEF)
  //          -> longitude, latitude, altitude -> position sur la carte (projection Web Mercator).
  // On passe par la Terre « ronde » parce que le jeu couvre 30 km : sur un plan, l'erreur atteindrait plusieurs mètres.
  const A = 6378137, F = 1/298.257223563, B_ = A*(1-F), E2 = F*(2-F), EP2 = (A*A-B_*B_)/(B_*B_);
  const mercX = lon => (lon + 180)/360;
  const mercY = lat => (1 - Math.log(Math.tan(lat*RAD) + 1/Math.cos(lat*RAD))/Math.PI)/2;
  function versCarte(glb, T, tuile){
    const M = glb.matrice, n = glb.pos.n, p = glb.pos.v, q = glb.nor.v, kp = glb.pos.normalise ? 1/glb.pos.max : 1;
    const latC = (tuile.sud + tuile.nord)/2;
    tuile.cx = mercX((tuile.ouest + tuile.est)/2); tuile.cy = mercY(latC);          // centre de la tuile sur la carte
    tuile.echelle = maplibregl.MercatorCoordinate.fromLngLat([0, latC], 0).meterInMercatorCoordinateUnits();   // 1 m en unités de carte
    const xyz = new Float32Array(n*3), normales = new Int8Array(n*3);
    for (let i=0;i<n;i++){
      const x = p[i*3]*kp, y = p[i*3+1]*kp, z = p[i*3+2]*kp;
      // matrice du fichier (repère glTF, « y vers le haut »), puis passage en « z vers le haut »
      const gx = M[0]*x + M[4]*y + M[8]*z + M[12], gy = M[1]*x + M[5]*y + M[9]*z + M[13], gz = M[2]*x + M[6]*y + M[10]*z + M[14];
      const lx = gx, ly = -gz, lz = gy;
      // repère centré sur la Terre
      const X = T[0]*lx + T[4]*ly + T[8]*lz + T[12], Y = T[1]*lx + T[5]*ly + T[9]*lz + T[13], Z = T[2]*lx + T[6]*ly + T[10]*lz + T[14];
      // longitude, latitude, altitude (formules de Bowring)
      const r = Math.hypot(X, Y), th = Math.atan2(Z*A, r*B_), st = Math.sin(th), ct = Math.cos(th);
      const lat = Math.atan2(Z + EP2*B_*st*st*st, r - E2*A*ct*ct*ct), sl = Math.sin(lat);
      const alt = r/Math.cos(lat) - A/Math.sqrt(1 - E2*sl*sl);
      xyz[i*3]   = (mercX(Math.atan2(Y, X)/RAD) - tuile.cx)/tuile.echelle;         // mètres vers l'est depuis le centre de la tuile
      xyz[i*3+1] = (mercY(lat/RAD) - tuile.cy)/tuile.echelle;                      // mètres vers le sud
      xyz[i*3+2] = alt;                                                            // altitude (m)
      // normale (orientation de la face, pour l'éclairage) : mêmes rotations, sans les translations
      const a = q[i*3], b = q[i*3+1], c = q[i*3+2];
      const ngx = M[0]*a + M[4]*b + M[8]*c, ngy = M[1]*a + M[5]*b + M[9]*c, ngz = M[2]*a + M[6]*b + M[10]*c;
      const l = Math.hypot(ngx, ngy, ngz) || 1;
      normales[i*3] = Math.round(127*ngx/l); normales[i*3+1] = Math.round(127*ngz/l); normales[i*3+2] = Math.round(127*ngy/l);   // est, sud, haut
    }
    return {xyz, normales};
  }

  // ---------- 4. la couche MapLibre ----------
  const VERTEX = `
    attribute vec3 a_pos; attribute vec3 a_nor; attribute float a_etat;
    uniform mat4 u_mat; uniform vec3 u_coul[4];
    varying vec3 v_coul;
    void main(){
      gl_Position = u_mat * vec4(a_pos, 1.0);
      float lumiere = 0.58 + 0.42*dot(normalize(a_nor), normalize(vec3(-0.45, 0.35, 0.82)));   // faces tournées vers la lumière plus claires
      v_coul = u_coul[int(a_etat + 0.5)] * lumiere;
    }`;
  const FRAGMENT = `
    precision mediump float;
    varying vec3 v_coul;
    void main(){ gl_FragColor = vec4(v_coul, 1.0); }`;
  const rvb = hex => [1,3,5].map(k => parseInt(hex.substr(k,2), 16)/255);

  // options :
  //   urlTileset   adresse de tileset.json
  //   rangDe       fonction : identifiant BD TOPO -> rang j du bâtiment dans le modèle (ou -1 s'il n'en fait pas partie)
  //   etats        tableau : etats[j] = 0 hors d'eau, 1 touché, 2 plus d'1 m d'eau, 3 sous le niveau marin non atteint
  //   couleurs     les 4 couleurs correspondantes ("#rrggbb")
  //   exageration  fonction : exagération verticale en cours (la même que le relief)
  //   zoomMin      zoom à partir duquel les modèles détaillés sont chargés
  //   surChange    fonction appelée quand une tuile est chargée ou libérée (rangs j concernés, présent ou non)
  async function creer(options){
    const {tuiles, transform} = await listerTuiles(options.urlTileset);
    const MAX_TUILES = 30, MAX_SIMULTANE = 3;
    let carte = null, gl = null, prog = null, lieux = null, visible = true, enCours = 0, horloge = 0;

    function compiler(type, source){
      const s = gl.createShader(type); gl.shaderSource(s, source); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      return s;
    }
    // couleur de chaque sommet = état du bâtiment auquel il appartient
    function ecrireEtats(t){
      const e = t.etatSommet, id = t.idBat, rang = t.rang, etats = options.etats;
      for (let i=0;i<e.length;i++){ const j = rang[id[i]]; e[i] = j < 0 ? 0 : etats[j]; }
      gl.bindBuffer(gl.ARRAY_BUFFER, t.tEtat); gl.bufferData(gl.ARRAY_BUFFER, e, gl.DYNAMIC_DRAW);
    }
    async function charger(t){
      t.etat = "chargement"; enCours++;
      try {
        const glb = await lireGlb(t.url), {xyz, normales} = versCarte(glb, transform, t);
        if (!gl) return;
        t.idBat = glb.idBat; t.rang = Int32Array.from(glb.cleabs, options.rangDe);
        t.etatSommet = new Uint8Array(glb.pos.n); t.nIndices = glb.indices.length;
        t.typeIndices = glb.indices instanceof Uint32Array ? gl.UNSIGNED_INT : gl.UNSIGNED_SHORT;
        t.tPos = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, t.tPos); gl.bufferData(gl.ARRAY_BUFFER, xyz, gl.STATIC_DRAW);
        t.tNor = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, t.tNor); gl.bufferData(gl.ARRAY_BUFFER, normales, gl.STATIC_DRAW);
        t.tEtat = gl.createBuffer(); ecrireEtats(t);
        t.tInd = gl.createBuffer(); gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, t.tInd); gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, glb.indices, gl.STATIC_DRAW);
        gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, null);
        t.etat = "pret";
        if (options.surChange) options.surChange(t.rang, true);
        carte.triggerRepaint();
      } catch (err){ t.etat = "erreur"; console.error("bâtiments détaillés :", err); }
      finally { enCours--; choisir(); }
    }
    function liberer(t){
      [t.tPos, t.tNor, t.tEtat, t.tInd].forEach(b => gl.deleteBuffer(b));
      if (options.surChange) options.surChange(t.rang, false);
      t.etat = undefined; t.idBat = t.rang = t.etatSommet = null;
    }
    const dansVue = (t, b) => t.est > b.getWest() && t.ouest < b.getEast() && t.nord > b.getSouth() && t.sud < b.getNorth();
    // charge les tuiles visibles (les plus proches du centre d'abord) et libère les plus anciennes au-delà de MAX_TUILES
    function choisir(){
      if (!carte || !visible || carte.getZoom() < options.zoomMin) return;
      const b = carte.getBounds(), c = carte.getCenter();
      const dist = t => Math.hypot((t.ouest + t.est)/2 - c.lng, ((t.sud + t.nord)/2 - c.lat)*1.5);
      const voulues = tuiles.filter(t => dansVue(t, b)).sort((u, v) => dist(u) - dist(v));
      voulues.forEach(t => t.vu = ++horloge);
      for (const t of voulues){ if (enCours >= MAX_SIMULTANE) break; if (!t.etat) charger(t); }
      const pretes = tuiles.filter(t => t.etat === "pret").sort((u, v) => u.vu - v.vu);
      while (pretes.length > MAX_TUILES) liberer(pretes.shift());
    }

    const couche = {
      id:options.id || "batiments-detail", type:"custom", renderingMode:"3d",
      onAdd(c, contexte){
        carte = c; gl = contexte;
        prog = gl.createProgram();
        gl.attachShader(prog, compiler(gl.VERTEX_SHADER, VERTEX)); gl.attachShader(prog, compiler(gl.FRAGMENT_SHADER, FRAGMENT));
        gl.linkProgram(prog);
        if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
        lieux = {pos:gl.getAttribLocation(prog, "a_pos"), nor:gl.getAttribLocation(prog, "a_nor"), etat:gl.getAttribLocation(prog, "a_etat"),
                 mat:gl.getUniformLocation(prog, "u_mat"), coul:gl.getUniformLocation(prog, "u_coul")};
        if (!gl.createVertexArray) gl.getExtension("OES_element_index_uint");
        carte.on("moveend", choisir); choisir();
      },
      // MapLibre appelle render à chaque image ; « matrice » transforme les coordonnées de carte en position à l'écran
      render(contexte, matrice){
        if (!visible || carte.getZoom() < options.zoomMin) return;
        const pretes = tuiles.filter(t => t.etat === "pret");
        if (!pretes.length) return;
        const b = carte.getBounds(), ex = options.exageration();
        // Avec le relief, MapLibre place l'altitude zéro de cette matrice au niveau du sol au centre de l'écran :
        // on retranche donc cette altitude (déjà exagérée) pour que les bâtiments restent posés sur le terrain.
        const solCentre = carte.getTerrain() ? carte.transform.elevation : 0;
        if (gl.bindVertexArray) gl.bindVertexArray(null);
        gl.useProgram(prog);
        gl.enable(gl.DEPTH_TEST); gl.depthFunc(gl.LEQUAL); gl.depthMask(true);
        gl.disable(gl.BLEND); gl.disable(gl.CULL_FACE); gl.disable(gl.STENCIL_TEST);
        gl.uniform3fv(lieux.coul, options.couleurs.flatMap(rvb));
        gl.enableVertexAttribArray(lieux.pos); gl.enableVertexAttribArray(lieux.nor); gl.enableVertexAttribArray(lieux.etat);
        const m = new Float32Array(16);
        for (const t of pretes){
          if (!dansVue(t, b)) continue;
          // matrice de la tuile = matrice de la carte x (déplacement au centre de la tuile, puis mise à l'échelle des mètres)
          const s = t.echelle, tz = -solCentre*s, sz = s*ex;
          for (let k=0;k<4;k++){
            m[k] = matrice[k]*s; m[4+k] = matrice[4+k]*s; m[8+k] = matrice[8+k]*sz;
            m[12+k] = matrice[k]*t.cx + matrice[4+k]*t.cy + matrice[8+k]*tz + matrice[12+k];
          }
          gl.uniformMatrix4fv(lieux.mat, false, m);
          gl.bindBuffer(gl.ARRAY_BUFFER, t.tPos); gl.vertexAttribPointer(lieux.pos, 3, gl.FLOAT, false, 0, 0);
          gl.bindBuffer(gl.ARRAY_BUFFER, t.tNor); gl.vertexAttribPointer(lieux.nor, 3, gl.BYTE, true, 0, 0);
          gl.bindBuffer(gl.ARRAY_BUFFER, t.tEtat); gl.vertexAttribPointer(lieux.etat, 1, gl.UNSIGNED_BYTE, false, 0, 0);
          gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, t.tInd);
          gl.drawElements(gl.TRIANGLES, t.nIndices, t.typeIndices, 0);
        }
        gl.disableVertexAttribArray(lieux.pos); gl.disableVertexAttribArray(lieux.nor); gl.disableVertexAttribArray(lieux.etat);
        gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, null); gl.bindBuffer(gl.ARRAY_BUFFER, null);
      },
      onRemove(){ carte.off("moveend", choisir); tuiles.forEach(t => { if (t.etat === "pret") liberer(t); }); gl = null; }
    };

    return {
      couche, nombreTuiles:tuiles.length,
      // à appeler quand les états des bâtiments ont changé (nouveau niveau marin, case cochée...)
      majEtats(){ if (!gl) return; tuiles.forEach(t => { if (t.etat === "pret") ecrireEtats(t); }); carte.triggerRepaint(); },
      afficher(oui){ visible = oui; if (carte){ choisir(); carte.triggerRepaint(); } },
      // contrôle : nombre de sommets dont la couleur ne correspond pas à l'état du bâtiment dans le modèle
      verifier(){
        let sommets = 0, ecarts = 0, batiments = 0;
        tuiles.forEach(t => { if (t.etat !== "pret") return;
          batiments += t.rang.filter(j => j >= 0).length;
          for (let i=0;i<t.etatSommet.length;i++){ const j = t.rang[t.idBat[i]]; sommets++; if (t.etatSommet[i] !== (j < 0 ? 0 : options.etats[j])) ecarts++; } });
        return {tuilesChargees:tuiles.filter(t => t.etat === "pret").length, batimentsDuModele:batiments, sommets, ecarts};
      }
    };
  }

  return {creer, listerTuiles, lireGlb};
})();
