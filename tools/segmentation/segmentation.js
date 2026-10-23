/* =====================================================================================
   DECOUPAGE D'UNE PHOTO DE PIECES — portage JS de segmentation_finale.py

   API :  segBoxes(rgba, w, h) -> [{x, y, w, h, area, edge}, ...]  (px de travail, trie y,x)

   `rgba` : Uint8ClampedArray RGBA de longueur w*h*4, DEJA reduite a la largeur de travail
   (~1100 px) par l'appelant. Fonction PURE : ni document, ni canvas, ni window.

   Tout est prefixe `seg`. ES5, aucun module, aucune dependance.

   Regle sur 6 photos reelles du set 42226. Sur la photo dense (47 pieces) : 45 decoupes,
   36 reconnues au rang 1 par Brickognize, 8 repechees par l'inventaire, 1 signalee.

   POURQUOI CHAQUE SEUIL EXISTE — ce sont des echecs mesures, pas des reglages de gout :
   * Les seuils de luminance sont ASYMETRIQUES parce qu'une ombre ne va QUE vers le bas
     (-20 a -35 %) et que rien n'eclaircit le fond. Symetrique a 0,34, la plaque Light
     Bluish Gray (+25 %) etait invisible.
   * L'ecart de teinte se mesure CONTRE LE FOND LOCAL, jamais en absolu : le gris de la
     table sous une lumiere chaude est teinte, et un seuil de saturation absolu mettait
     21,6 % de l'image en « couleur » — toute la zone eclairee devenait une seule piece.
   * La separation de deux pieces collees juge sur une DYNAMIQUE relative a l'epaisseur de
     la piece, jamais sur un nombre de pixels : en absolu, le meme reglage coupait une roue
     en gros plan et laissait collees deux plaques vues de loin.
   ===================================================================================== */

var SEG_CELL = 55;          /* cote d'une cellule de la grille de fond */
var SEG_MIN_FRAC = 0.20;    /* part de fond exigee dans une cellule pour qu'elle compte */
var SEG_UP = 0.14;          /* une piece CLAIRE depasse le fond local de 14 % */
var SEG_DOWN = 0.34;        /* une piece SOMBRE descend de 34 % sous le fond local */
var SEG_DC = 0.115;         /* ecart de teinte au fond local */
var SEG_OPEN_K = 3;         /* ouverture : tue le bruit */
var SEG_CLOSE_K = 11;       /* fermeture : recolle tenons, bords et trous traversants */
var SEG_BETA = 0.30;        /* dynamique exigee pour couper, RELATIVE a l'epaisseur */
var SEG_H_MIN = 3;
var SEG_MIN_CHILD = 0.15;   /* un enfant sous 15 % du parent est un eclat : reabsorbe */
var SEG_MIN_AREA = 0.00030; /* fraction de l'image */
var SEG_MAX_AREA = 0.25;

/* --- mediane exacte par selection (meme definition que numpy : moyenne des deux du
       milieu quand le compte est pair). Destructif sur `v[0..n-1]`. ------------------- */
function segSelect(v, lo, hi, k) {
    while (lo < hi) {
        var p = v[(lo + hi) >> 1], i = lo, j = hi, t;
        while (i <= j) {
            while (v[i] < p) i++;
            while (v[j] > p) j--;
            if (i <= j) { t = v[i]; v[i] = v[j]; v[j] = t; i++; j--; }
        }
        if (k <= j) hi = j; else if (k >= i) lo = i; else return v[k];
    }
    return v[lo];
}

function segMedian(v, n) {
    if (n <= 0) return NaN;
    var mid = n >> 1;
    var hi = segSelect(v, 0, n - 1, mid);
    if (n & 1) return hi;
    var lo = -Infinity;
    for (var i = 0; i < mid; i++) if (v[i] > lo) lo = v[i];
    return (lo + hi) / 2;
}

/* --- bornes de cellules : np.linspace(0, n, g+1).astype(int) ---------------------- */
function segEdges(n, g) {
    var e = new Int32Array(g + 1);
    for (var i = 0; i <= g; i++) e[i] = Math.floor(i * n / g);
    return e;
}

/* --- agrandissement bilineaire, formule de cv2.resize INTER_LINEAR ---------------- */
function segUpsample(grid, gw, gh, w, h) {
    var out = new Float32Array(w * h);
    var sx = gw / w, sy = gh / h;
    var xi = new Int32Array(w), xf = new Float32Array(w);
    var i, j;
    for (j = 0; j < w; j++) {
        var fx = (j + 0.5) * sx - 0.5, ix = Math.floor(fx), fr = fx - ix;
        if (ix < 0) { ix = 0; fr = 0; }
        if (ix >= gw - 1) { ix = gw - 2 < 0 ? 0 : gw - 2; fr = gw > 1 ? 1 : 0; }
        xi[j] = ix; xf[j] = fr;
    }
    for (i = 0; i < h; i++) {
        var fy = (i + 0.5) * sy - 0.5, iy = Math.floor(fy), fry = fy - iy;
        if (iy < 0) { iy = 0; fry = 0; }
        if (iy >= gh - 1) { iy = gh - 2 < 0 ? 0 : gh - 2; fry = gh > 1 ? 1 : 0; }
        var r0 = iy * gw, r1 = (iy + (gh > 1 ? 1 : 0)) * gw, o = i * w;
        for (j = 0; j < w; j++) {
            var a = xi[j], b = a + (gw > 1 ? 1 : 0), t = xf[j];
            var top = grid[r0 + a] + (grid[r0 + b] - grid[r0 + a]) * t;
            var bot = grid[r1 + a] + (grid[r1 + b] - grid[r1 + a]) * t;
            out[o + j] = top + (bot - top) * fry;
        }
    }
    return out;
}

/* --- fond local : mediane par cellule, trous combles par diffusion ---------------- */
function segGridMedian(arr, w, h, keep, cell, minFrac) {
    var gh = Math.max(2, Math.floor(h / cell)), gw = Math.max(2, Math.floor(w / cell));
    var ys = segEdges(h, gh), xs = segEdges(w, gw);
    var grid = new Float32Array(gw * gh);
    var nan = new Uint8Array(gw * gh);
    var buf = new Float64Array(cell * cell * 4 + 16);
    var i, j, y, x, n, anyNan = 0;
    for (i = 0; i < gh; i++) {
        for (j = 0; j < gw; j++) {
            n = 0;
            if (buf.length < (ys[i + 1] - ys[i]) * (xs[j + 1] - xs[j]))
                buf = new Float64Array((ys[i + 1] - ys[i]) * (xs[j + 1] - xs[j]));
            var size = 0, kept = 0;
            for (y = ys[i]; y < ys[i + 1]; y++) {
                var row = y * w;
                for (x = xs[j]; x < xs[j + 1]; x++) {
                    size++;
                    if (keep === null) buf[n++] = arr[row + x];
                    else if (keep[row + x]) { buf[n++] = arr[row + x]; kept++; }
                }
            }
            if (keep !== null && !(kept > minFrac * size)) {
                nan[i * gw + j] = 1; anyNan = 1;
            } else {
                grid[i * gw + j] = segMedian(buf, n);
            }
        }
    }
    /* diffusion depuis les cellules sures : une cellule noyee par une piece prendrait
       sinon la couleur de la piece pour celle du fond, et la piece disparaitrait */
    if (anyNan) {
        var rounds = Math.max(gh, gw), g2 = new Float32Array(gw * gh), n2 = new Uint8Array(gw * gh);
        for (var r = 0; r < rounds; r++) {
            var left = 0;
            for (i = 0; i < gh; i++) for (j = 0; j < gw; j++) {
                var id = i * gw + j;
                g2[id] = grid[id]; n2[id] = nan[id];
                if (!nan[id]) continue;
                var s = 0, c = 0, ii, jj, nb;
                /* np.pad(mode="edge") : hors grille, on relit le bord */
                ii = i - 1 < 0 ? 0 : i - 1; nb = ii * gw + j; if (!nan[nb]) { s += grid[nb]; c++; }
                ii = i + 1 >= gh ? gh - 1 : i + 1; nb = ii * gw + j; if (!nan[nb]) { s += grid[nb]; c++; }
                jj = j - 1 < 0 ? 0 : j - 1; nb = i * gw + jj; if (!nan[nb]) { s += grid[nb]; c++; }
                jj = j + 1 >= gw ? gw - 1 : j + 1; nb = i * gw + jj; if (!nan[nb]) { s += grid[nb]; c++; }
                if (c) { g2[id] = s / c; n2[id] = 0; } else left++;
            }
            grid.set(g2); nan.set(n2);
            if (!left) break;
        }
        /* repli : la mediane de TOUT ce qui reste, jamais zero — une cellule a zero
           donnerait un fond noir et toute la zone passerait pour une piece */
        var still = 0;
        for (i = 0; i < gw * gh; i++) if (nan[i]) still++;
        if (still) {
            var all = new Float64Array(w * h), m = 0;
            for (i = 0; i < w * h; i++) if (keep === null || keep[i]) all[m++] = arr[i];
            if (!m) { for (i = 0; i < w * h; i++) all[m++] = arr[i]; }
            var fb = segMedian(all, m);
            for (i = 0; i < gw * gh; i++) if (nan[i]) grid[i] = fb;
        }
    }
    return segUpsample(grid, gw, gh, w, h);
}

/* --- ecarts au fond : luminance (relative) et teinte ------------------------------ */
function segDeviation(ch, lum, w, h, keep) {
    var np = w * h;
    var bgl = segGridMedian(lum, w, h, keep, SEG_CELL, SEG_MIN_FRAC);
    var bgc = [segGridMedian(ch[0], w, h, keep, SEG_CELL, SEG_MIN_FRAC),
               segGridMedian(ch[1], w, h, keep, SEG_CELL, SEG_MIN_FRAC),
               segGridMedian(ch[2], w, h, keep, SEG_CELL, SEG_MIN_FRAC)];
    var dl = new Float32Array(np), dc = new Float32Array(np);
    /* hissees hors de la boucle : un acces ch[c][i] par pixel et par canal coute cher en JS
       (indirection sur un tableau de tableaux), et ce sont 6 acces x 900 000 pixels */
    var c0 = ch[0], c1 = ch[1], c2 = ch[2];
    var g0 = bgc[0], g1 = bgc[1], g2 = bgc[2];
    for (var i = 0; i < np; i++) {
        var b = bgl[i];
        dl[i] = (lum[i] - b) / (b > 8 ? b : 8);
        /* une ombre divise les trois canaux par le meme facteur : normaliser par la
           luminance de part et d'autre la rend donc invisible ici, et c'est le but */
        var ls = lum[i] > 12 ? lum[i] : 12, bl = b > 12 ? b : 12;
        var s = Math.abs(c0[i] / ls - g0[i] / bl)
              + Math.abs(c1[i] / ls - g1[i] / bl)
              + Math.abs(c2[i] / ls - g2[i] / bl);
        dc[i] = s / 3;
    }
    return { dl: dl, dc: dc };
}

/* --- morphologie binaire carree, separable, en O(n) par ligne --------------------- */
function segMorph1D(src, dst, n, stride, off, k, dilate) {
    /* dilate : hors image = 0 (ne contribue pas). erode : hors image = 1 (n'erode pas).
       C'est exactement le bord par defaut de cv2.morphologyEx. */
    var r = k >> 1, i, cnt = 0;
    for (i = 0; i <= r && i < n; i++) cnt += src[off + i * stride];
    for (i = 0; i < n; i++) {
        var lo = i - r, hi = i + r;
        var span = (hi < n ? hi : n - 1) - (lo > 0 ? lo : 0) + 1;
        dst[off + i * stride] = dilate ? (cnt > 0 ? 1 : 0) : (cnt === span ? 1 : 0);
        var out = i - r, inn = i + r + 1;
        if (out >= 0) cnt -= src[off + out * stride];
        if (inn < n) cnt += src[off + inn * stride];
    }
}

function segMorph(mask, w, h, k, dilate) {
    var tmp = new Uint8Array(w * h), out = new Uint8Array(w * h), i;
    for (i = 0; i < h; i++) segMorph1D(mask, tmp, w, 1, i * w, k, dilate);
    for (i = 0; i < w; i++) segMorph1D(tmp, out, h, w, i, k, dilate);
    return out;
}

function segOpen(m, w, h, k) { return segMorph(segMorph(m, w, h, k, false), w, h, k, true); }
function segClose(m, w, h, k) { return segMorph(segMorph(m, w, h, k, true), w, h, k, false); }

/* --- remplir les trous fermes (trous Technic, axes) ------------------------------- */
function segFillHoles(mask, w, h) {
    var flood = new Uint8Array(w * h), i;
    for (i = 0; i < w * h; i++) flood[i] = mask[i] ? 0 : 1;
    var stack = new Int32Array(w * h), sp = 0;
    var seeds = [0, w - 1, (h - 1) * w, h * w - 1];
    for (i = 0; i < 4; i++) if (flood[seeds[i]]) { flood[seeds[i]] = 0; stack[sp++] = seeds[i]; }
    while (sp > 0) {
        var p = stack[--sp], x = p % w, y = (p - x) / w;   /* 4-connexite, comme cv2 */
        if (x > 0 && flood[p - 1]) { flood[p - 1] = 0; stack[sp++] = p - 1; }
        if (x < w - 1 && flood[p + 1]) { flood[p + 1] = 0; stack[sp++] = p + 1; }
        if (y > 0 && flood[p - w]) { flood[p - w] = 0; stack[sp++] = p - w; }
        if (y < h - 1 && flood[p + w]) { flood[p + w] = 0; stack[sp++] = p + w; }
    }
    var out = new Uint8Array(w * h);
    for (i = 0; i < w * h; i++) out[i] = (mask[i] || flood[i]) ? 1 : 0;
    return out;
}

/* --- composantes connexes 8-voisins, avec boite et aire --------------------------- */
function segComponents(mask, w, h) {
    var lab = new Int32Array(w * h), stack = new Int32Array(w * h), n = 0, stats = [];
    for (var p0 = 0; p0 < w * h; p0++) {
        if (!mask[p0] || lab[p0]) continue;
        n++;
        var sp = 0; stack[sp++] = p0; lab[p0] = n;
        var minx = w, maxx = -1, miny = h, maxy = -1, area = 0;
        while (sp > 0) {
            var p = stack[--sp], x = p % w, y = (p - x) / w;
            area++;
            if (x < minx) minx = x; if (x > maxx) maxx = x;
            if (y < miny) miny = y; if (y > maxy) maxy = y;
            for (var dy = -1; dy <= 1; dy++) {
                var ny = y + dy; if (ny < 0 || ny >= h) continue;
                for (var dx = -1; dx <= 1; dx++) {
                    var nx = x + dx; if (nx < 0 || nx >= w) continue;
                    var q = ny * w + nx;
                    if (mask[q] && !lab[q]) { lab[q] = n; stack[sp++] = q; }
                }
            }
        }
        stats.push({ x: minx, y: miny, w: maxx - minx + 1, h: maxy - miny + 1, area: area });
    }
    return { labels: lab, n: n, stats: stats };
}

/* --- distance au fond : chamfer 5x5, les poids de cv2 DIST_L2 / DIST_MASK_5 -------- */
function segDistance(mask, w, h) {
    var A = 1.0, B = 1.4, C = 2.1969, INF = 1e10;
    var d = new Float64Array(w * h), i, x, y;
    for (i = 0; i < w * h; i++) d[i] = mask[i] ? INF : 0;
    /* `at` etait une fermeture appelee 8 fois par pixel : inlinee ici, c'est le gros du
       gain sur une photo dense */
    function at(xx, yy) { return (xx < 0 || yy < 0 || xx >= w || yy >= h) ? INF : d[yy * w + xx]; }
    for (y = 0; y < h; y++) for (x = 0; x < w; x++) {
        i = y * w + x; if (d[i] === 0) continue;
        var v = d[i];
        if (at(x - 1, y) + A < v) v = at(x - 1, y) + A;
        if (at(x, y - 1) + A < v) v = at(x, y - 1) + A;
        if (at(x - 1, y - 1) + B < v) v = at(x - 1, y - 1) + B;
        if (at(x + 1, y - 1) + B < v) v = at(x + 1, y - 1) + B;
        if (at(x - 1, y - 2) + C < v) v = at(x - 1, y - 2) + C;
        if (at(x + 1, y - 2) + C < v) v = at(x + 1, y - 2) + C;
        if (at(x - 2, y - 1) + C < v) v = at(x - 2, y - 1) + C;
        if (at(x + 2, y - 1) + C < v) v = at(x + 2, y - 1) + C;
        d[i] = v;
    }
    for (y = h - 1; y >= 0; y--) for (x = w - 1; x >= 0; x--) {
        i = y * w + x; if (d[i] === 0) continue;
        var u = d[i];
        if (at(x + 1, y) + A < u) u = at(x + 1, y) + A;
        if (at(x, y + 1) + A < u) u = at(x, y + 1) + A;
        if (at(x + 1, y + 1) + B < u) u = at(x + 1, y + 1) + B;
        if (at(x - 1, y + 1) + B < u) u = at(x - 1, y + 1) + B;
        if (at(x + 1, y + 2) + C < u) u = at(x + 1, y + 2) + C;
        if (at(x - 1, y + 2) + C < u) u = at(x - 1, y + 2) + C;
        if (at(x + 2, y + 1) + C < u) u = at(x + 2, y + 1) + C;
        if (at(x - 2, y + 1) + C < u) u = at(x - 2, y + 1) + C;
        d[i] = u;
    }
    var out = new Int32Array(w * h);
    for (i = 0; i < w * h; i++) out[i] = Math.round(d[i]);   /* numpy round, puis uint16 */
    return out;
}

/* --- reconstruction morphologique par dilatation (algorithme sequentiel de Vincent)
       La version naive fait jusqu'a 600 dilatations pleine image ; celle-ci donne le MEME
       resultat en deux balayages plus une file. 8-connexite, comme un noyau 3x3 plein. -- */
function segReconstruct(marker, mask, w, h) {
    var m = new Int32Array(marker.length), i, x, y, p, q, v;
    for (i = 0; i < m.length; i++) m[i] = marker[i] < mask[i] ? marker[i] : mask[i];
    for (y = 0; y < h; y++) for (x = 0; x < w; x++) {
        p = y * w + x; v = m[p];
        for (var dy = -1; dy <= 0; dy++) {
            var ny = y + dy; if (ny < 0) continue;
            for (var dx = -1; dx <= 1; dx++) {
                if (dy === 0 && dx >= 0) continue;
                var nx = x + dx; if (nx < 0 || nx >= w) continue;
                q = ny * w + nx; if (m[q] > v) v = m[q];
            }
        }
        m[p] = v < mask[p] ? v : mask[p];
    }
    var queue = new Int32Array(w * h), qh = 0, qt = 0;
    for (y = h - 1; y >= 0; y--) for (x = w - 1; x >= 0; x--) {
        p = y * w + x; v = m[p];
        for (var dy2 = 0; dy2 <= 1; dy2++) {
            var ny2 = y + dy2; if (ny2 >= h) continue;
            for (var dx2 = -1; dx2 <= 1; dx2++) {
                if (dy2 === 0 && dx2 <= 0) continue;
                var nx2 = x + dx2; if (nx2 < 0 || nx2 >= w) continue;
                q = ny2 * w + nx2; if (m[q] > v) v = m[q];
            }
        }
        m[p] = v < mask[p] ? v : mask[p];
        for (var dy3 = 0; dy3 <= 1; dy3++) {
            var ny3 = y + dy3; if (ny3 >= h) continue;
            for (var dx3 = -1; dx3 <= 1; dx3++) {
                if (dy3 === 0 && dx3 <= 0) continue;
                var nx3 = x + dx3; if (nx3 < 0 || nx3 >= w) continue;
                q = ny3 * w + nx3;
                if (m[q] < m[p] && m[q] < mask[q]) { queue[qt++] = p; dy3 = 2; break; }
            }
        }
    }
    while (qh < qt) {
        p = queue[qh++];
        x = p % w; y = (p - x) / w;
        for (var ey = -1; ey <= 1; ey++) {
            var yy = y + ey; if (yy < 0 || yy >= h) continue;
            for (var ex = -1; ex <= 1; ex++) {
                if (!ex && !ey) continue;
                var xx = x + ex; if (xx < 0 || xx >= w) continue;
                q = yy * w + xx;
                if (m[q] < m[p] && mask[q] !== m[q]) {
                    m[q] = m[p] < mask[q] ? m[p] : mask[q];
                    if (qt >= queue.length) { var bigger = new Int32Array(queue.length * 2); bigger.set(queue); queue = bigger; }
                    queue[qt++] = q;
                }
            }
        }
    }
    return m;
}

function segRegionalMaxima(f, w, h) {
    var lower = new Int32Array(f.length), i;
    for (i = 0; i < f.length; i++) lower[i] = f[i] > 0 ? f[i] - 1 : 0;
    var rec = segReconstruct(lower, f, w, h);
    var out = new Uint8Array(f.length);
    for (i = 0; i < f.length; i++) out[i] = (f[i] - rec[i]) > 0 ? 1 : 0;
    return out;
}

/* --- croissance des marqueurs, a egalite de vitesse : la frontiere tombe sur le col -- */
function segGrow(markers, mask, w, h, rounds) {
    var lab = new Int32Array(markers), i, k;   /* copie ; .from() serait de l'ES6 */
    /* On ne repasse que sur les pixels ENCORE sans etiquette : la frontiere fond a chaque
       tour, alors qu'un balayage complet par tour coutait le meme prix jusqu'au bout. Le
       resultat est identique — seuls ces pixels-la peuvent changer. */
    var pend = [];
    for (i = 0; i < lab.length; i++) if (!lab[i] && mask[i]) pend.push(i);
    var vals = new Int32Array(pend.length);
    for (var r = 0; r < rounds && pend.length; r++) {
        for (k = 0; k < pend.length; k++) {
            var p = pend[k], x = p % w, y = (p - x) / w, v = 0;
            var y0 = y > 0 ? y - 1 : 0, y1 = y < h - 1 ? y + 1 : h - 1;
            var x0 = x > 0 ? x - 1 : 0, x1 = x < w - 1 ? x + 1 : w - 1;
            for (var yy = y0; yy <= y1; yy++) {
                var row = yy * w;
                for (var xx = x0; xx <= x1; xx++) if (lab[row + xx] > v) v = lab[row + xx];
            }
            vals[k] = v;
        }
        var next = [];
        for (k = 0; k < pend.length; k++) {
            if (vals[k]) lab[pend[k]] = vals[k]; else next.push(pend[k]);
        }
        if (next.length === pend.length) break;   /* plus aucun progres possible */
        pend = next;
    }
    return lab;
}

/* --- couper UNE composante : h-maxima sur la carte de distance -------------------- */
function segSplitOne(sub, w, h) {
    var d = segDistance(sub, w, h), i, dmax = 0;
    for (i = 0; i < d.length; i++) if (d[i] > dmax) dmax = d[i];
    var hh = Math.max(SEG_H_MIN, Math.round(SEG_BETA * dmax));
    var out;
    if (dmax <= hh + 1) {
        out = new Int32Array(d.length);
        for (i = 0; i < out.length; i++) out[i] = sub[i];
        return { labels: out, n: 1 };
    }
    var lowered = new Int32Array(d.length);
    for (i = 0; i < d.length; i++) { var v = d[i] - hh; lowered[i] = v > 0 ? v : 0; }
    var hmax = segReconstruct(lowered, d, w, h);
    var rm = segRegionalMaxima(hmax, w, h);
    for (i = 0; i < rm.length; i++) if (!sub[i]) rm[i] = 0;
    var cc = segComponents(rm, w, h);
    if (cc.n <= 1) {                       /* un seul sommet : une seule piece */
        out = new Int32Array(d.length);
        for (i = 0; i < out.length; i++) out[i] = sub[i];
        return { labels: out, n: 1 };
    }
    var lab = segGrow(cc.labels, sub, w, h, dmax * 2 + 8);

    /* reabsorber les eclats : un enfant trop petit rejoint son plus gros voisin.
       `min_child` se mesure contre CETTE piece — le mesurer globalement rendait zero boite */
    var parent = 0;
    for (i = 0; i < sub.length; i++) if (sub[i]) parent++;
    var areas = new Int32Array(cc.n + 1);
    for (i = 0; i < lab.length; i++) if (lab[i] > 0) areas[lab[i]]++;
    for (var k = 1; k <= cc.n; k++) {
        if (!(areas[k] > 0 && areas[k] < SEG_MIN_CHILD * parent)) continue;
        var tally = new Int32Array(cc.n + 1), best = 0, bestN = 0;
        for (i = 0; i < lab.length; i++) {
            if (lab[i] !== k) continue;
            var x = i % w, y = (i - x) / w;
            for (var dy = -1; dy <= 1; dy++) {
                var ny = y + dy; if (ny < 0 || ny >= h) continue;
                for (var dx = -1; dx <= 1; dx++) {
                    var nx = x + dx; if (nx < 0 || nx >= w) continue;
                    var q = ny * w + nx;
                    if (lab[q] > 0 && lab[q] !== k) tally[lab[q]]++;
                }
            }
        }
        for (i = 1; i <= cc.n; i++) if (tally[i] > bestN) { bestN = tally[i]; best = i; }
        for (i = 0; i < lab.length; i++) if (lab[i] === k) lab[i] = best;
    }
    return { labels: lab, n: cc.n };
}

/* --- API ------------------------------------------------------------------------- */
function segBoxes(rgba, w, h) {
    var np = w * h, i;
    var R = new Float32Array(np), G = new Float32Array(np), B = new Float32Array(np);
    var lum = new Float32Array(np);
    for (i = 0; i < np; i++) {
        var r = rgba[i * 4], g = rgba[i * 4 + 1], b = rgba[i * 4 + 2];
        R[i] = r; G[i] = g; B[i] = b;
        lum[i] = 0.299 * r + 0.587 * g + 0.114 * b;   /* RGBA ici, BGR cote OpenCV */
    }
    var ch = [R, G, B];
    var dev = segDeviation(ch, lum, w, h, null);          /* passe 1 : fond sur tout */
    var keep = new Uint8Array(np);
    for (i = 0; i < np; i++)
        keep[i] = (dev.dl[i] < SEG_UP && dev.dl[i] > -SEG_DOWN && dev.dc[i] < SEG_DC) ? 1 : 0;
    dev = segDeviation(ch, lum, w, h, keep);              /* passe 2 : fond sans les pieces */

    var mask = new Uint8Array(np);
    for (i = 0; i < np; i++)
        mask[i] = (dev.dl[i] > SEG_UP || dev.dl[i] < -SEG_DOWN || dev.dc[i] > SEG_DC) ? 1 : 0;
    mask = segOpen(mask, w, h, SEG_OPEN_K);
    mask = segClose(mask, w, h, SEG_CLOSE_K);
    mask = segFillHoles(mask, w, h);

    /* separation, composante par composante, sur sa propre sous-image */
    var comps = segComponents(mask, w, h);
    var boxes = [], pad = 3;
    for (var c = 0; c < comps.n; c++) {
        var st = comps.stats[c];
        var x0 = Math.max(0, st.x - pad), y0 = Math.max(0, st.y - pad);
        var x1 = Math.min(w, st.x + st.w + pad), y1 = Math.min(h, st.y + st.h + pad);
        var sw = x1 - x0, sh = y1 - y0;
        var sub = new Uint8Array(sw * sh);
        for (var yy = 0; yy < sh; yy++) {
            var src = (y0 + yy) * w + x0, dst = yy * sw;
            for (var xx = 0; xx < sw; xx++)
                sub[dst + xx] = comps.labels[src + xx] === c + 1 ? 1 : 0;
        }
        var res = segSplitOne(sub, sw, sh);
        var seen = {};
        for (i = 0; i < res.labels.length; i++) {
            var v = res.labels[i]; if (!v) continue;
            var px = i % sw, py = (i - px) / sw, gx = x0 + px, gy = y0 + py;
            var e = seen[v];
            if (!e) { seen[v] = { x0: gx, y0: gy, x1: gx, y1: gy, a: 1 }; continue; }
            if (gx < e.x0) e.x0 = gx; if (gx > e.x1) e.x1 = gx;
            if (gy < e.y0) e.y0 = gy; if (gy > e.y1) e.y1 = gy;
            e.a++;
        }
        for (var key in seen) if (seen.hasOwnProperty(key)) {
            var s = seen[key];
            if (s.a < SEG_MIN_AREA * np || s.a > SEG_MAX_AREA * np) continue;
            var bw = s.x1 - s.x0 + 1, bh = s.y1 - s.y0 + 1;
            boxes.push({
                x: s.x0, y: s.y0, w: bw, h: bh, area: s.a,
                /* ce qui touche le bord est SIGNALE, pas jete : bord de table, main, ou
                   piece coupee par le cadre — ca demande l'oeil, pas une decision auto */
                edge: s.x0 <= 1 || s.y0 <= 1 || s.x0 + bw >= w - 1 || s.y0 + bh >= h - 1
            });
        }
    }
    boxes.sort(function (p, q) { return p.y - q.y || p.x - q.x; });
    return boxes;
}

if (typeof module !== "undefined" && module.exports) module.exports = { segBoxes: segBoxes };
