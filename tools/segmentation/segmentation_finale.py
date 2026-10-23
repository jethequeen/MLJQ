# -*- coding: utf-8 -*-
"""SEGMENTATION RETENUE — version consolidee, autosuffisante, a porter en JS canvas.

Regle sur 6 photos reelles de Jeremie (set 42226 BMW M4 GT3 EVO), dont une photo dense de
47 pieces qui est son mode de travail prefere. Mesure finale sur cette photo :

    45 decoupes          (2 pieces manquees, aucune fausse, aucun bord)
    36 reconnues au rang 1 de Brickognize
     8 repechees par l'inventaire du set
     1 sans correspondance, signalee

ATTENTION AU PORTAGE — ce que ce fichier N'EST PAS :
  * Il n'utilise PAS de surface quadratique. Elle a ete essayee (fond ajuste par moindres
    carres) et REJETEE : elle repare les gros plans mais ne suit pas une ombre portee, et
    sur la photo dense elle faisait passer de grandes zones de fond pour des pieces (4 blobs,
    rattrapes seulement par la regle du bord). Le fond retenu est la GRILLE MEDIANE.
  * Il n'y a aucune algebre lineaire, aucun filtre non entier, aucune dependance : tout est
    local et portable. La seule vraie piece de travail au portage est la reconstruction
    morphologique (voir `reconstruct`).

Ce que chaque constante corrige est note a cote d'elle.
"""
import numpy as np
import cv2

# --- constantes reglees ------------------------------------------------------
W = 1100          # largeur de travail. Le decoupage se fait ici, les DECOUPES se prennent
                  # dans la photo PLEINE RESOLUTION (Brickognize a besoin des details).
CELL = 55         # cote d'une cellule de la grille de fond, en px de travail.
MIN_FRAC = 0.20   # part de fond exigee dans une cellule pour qu'elle compte ; en dessous la
                  # cellule est declaree noyee et reprend la valeur de ses voisines.
UP = 0.14         # une piece CLAIRE depasse le fond local de 14 %.
DOWN = 0.34       # une piece SOMBRE descend de 34 % sous le fond local.
                  #   L'asymetrie est le point : une ombre ne va QUE vers le bas (-20 a -35 %)
                  #   et rien n'eclaircit le fond. On peut donc etre beaucoup plus permissif
                  #   du cote clair sans ramasser une ombre. Symetrique a 0,34, la plaque
                  #   Light Bluish Gray (+25 %) etait invisible.
DC = 0.115        # ecart de TEINTE (rgb normalise par la luminance) au fond local.
                  #   Mesure contre le fond, jamais en absolu : le gris de la table sous une
                  #   lumiere chaude est legerement teinte, et un seuil de saturation absolu
                  #   mettait 21,6 % de l'image en « couleur » — toute la zone eclairee
                  #   devenait une seule piece geante.
OPEN_K = 3        # ouverture : tue le bruit poivre-et-sel.
CLOSE_K = 11      # fermeture : recolle les tenons, les bords et les trous traversants.
BETA = 0.30       # dynamique exigee pour couper deux pieces collees, RELATIVE a l'epaisseur
                  # de la piece. En pixels absolus le meme algo coupait une roue en gros plan
                  # et laissait collees deux plaques vues de loin.
H_MIN = 3         # plancher de BETA, en px.
MIN_CHILD = 0.15  # un enfant de coupe sous 15 % du parent est un eclat : il est reabsorbe.
MIN_AREA = 0.00030   # fraction de l'image : sous ca, ce n'est pas une piece.
MAX_AREA = 0.25
MARGIN = 0.12     # marge autour de la boite avant de decouper, en fraction de son grand cote.
LONG_SIDE = 420   # cote long de la decoupe envoyee a Brickognize.

_K3 = np.ones((3, 3), np.uint8)


# --- 1. canaux ---------------------------------------------------------------

def channels(bgr):
    f = bgr.astype(np.float32)
    l = 0.114 * f[:, :, 0] + 0.587 * f[:, :, 1] + 0.299 * f[:, :, 2]
    return f, l


# --- 2. fond local : mediane par cellule, 2 passes ---------------------------

def grid_median(arr, cell=CELL, keep=None, min_frac=MIN_FRAC):
    """Mediane par cellule -> carte grossiere, puis interpolation bilineaire.

    La mediane resiste aux pieces tant qu'elles ne dominent pas la cellule. La 2e passe
    recalcule chaque cellule en EXCLUANT ce que la 1re a juge « piece ». Une cellule noyee
    est laissee vide et comblee par diffusion depuis ses voisines sures — sans ca elle
    prendrait la couleur de la piece pour celle du fond, et la piece disparaitrait.
    """
    h, w = arr.shape[:2]
    gh, gw = max(2, h // cell), max(2, w // cell)
    ys = np.linspace(0, h, gh + 1).astype(int)
    xs = np.linspace(0, w, gw + 1).astype(int)
    out = np.full((gh, gw), np.nan, np.float32)
    for i in range(gh):
        for j in range(gw):
            sub = arr[ys[i]:ys[i + 1], xs[j]:xs[j + 1]]
            if keep is None:
                out[i, j] = np.median(sub)
            else:
                m = keep[ys[i]:ys[i + 1], xs[j]:xs[j + 1]]
                if m.sum() > min_frac * m.size:
                    out[i, j] = np.median(sub[m])
    if np.isnan(out).any():
        for _ in range(max(gh, gw)):
            if not np.isnan(out).any():
                break
            pad = np.pad(out, 1, mode="edge")
            st = np.stack([pad[:-2, 1:-1], pad[2:, 1:-1], pad[1:-1, :-2], pad[1:-1, 2:]])
            with np.errstate(invalid="ignore"):
                out = np.where(np.isnan(out), np.nanmean(st, axis=0), out)
        # Repli : la mediane de TOUT ce qui reste, jamais zero. Une cellule qui finit a zero
        # donnerait un fond noir, et toute la zone passerait pour une piece.
        if np.isnan(out).any():
            fallback = np.nanmedian(arr[keep] if keep is not None and keep.any() else arr)
            out = np.nan_to_num(out, nan=float(fallback))
    return cv2.resize(out, (w, h), interpolation=cv2.INTER_LINEAR)


def deviation(f, l, keep=None):
    """(dL, dC) : ecart de luminance relatif, et ecart de teinte, contre le fond local.

    Separer les deux est ce qui distingue les trois cas qui se ressemblent :
      une OMBRE      baisse la luminance sans toucher la teinte  (dL ~ -0,25 ; dC ~ 0)
      une piece NOIRE ecroule la luminance                        (dL ~ -0,8)
      une piece BLEUE deplace la teinte                           (dC grand)
    Un seul canal les melange.
    """
    bgl = grid_median(l, keep=keep)
    bgc = [grid_median(f[:, :, c], keep=keep) for c in range(3)]
    dl = (l - bgl) / np.maximum(bgl, 8)
    ls, bl = np.maximum(l, 12), np.maximum(bgl, 12)
    dc = sum(np.abs(f[:, :, c] / ls - bgc[c] / bl) for c in range(3)) / 3.0
    return dl, dc


# --- 3. masque ---------------------------------------------------------------

def fill_holes(mask):
    """Remplit les trous fermes (trous Technic, axes) sans toucher a ce qui sort du cadre."""
    h, w = mask.shape
    flood = (mask == 0).astype(np.uint8)
    m2 = np.zeros((h + 2, w + 2), np.uint8)
    for seed in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        cv2.floodFill(flood, m2, seed, 0)
    return ((mask > 0) | (flood > 0)).astype(np.uint8)


def build_mask(small):
    f, l = channels(small)
    dl, dc = deviation(f, l)                           # passe 1
    keep = (dl < UP) & (dl > -DOWN) & (dc < DC)        # ce qui ressemble au fond
    dl, dc = deviation(f, l, keep=keep)                # passe 2, fond sans les pieces
    m = ((dl > UP) | (dl < -DOWN) | (dc > DC)).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((OPEN_K,) * 2, np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((CLOSE_K,) * 2, np.uint8))
    return fill_holes(m)


# --- 4. separer deux pieces qui se touchent ----------------------------------

def reconstruct(marker, mask, iters=600):
    """Reconstruction morphologique par dilatation.

    *** C'EST LA SEULE VRAIE PIECE DE TRAVAIL AU PORTAGE. *** Cette boucle naive fait
    jusqu'a 600 dilatations pleine image ; en JS il faut l'algorithme sequentiel de Vincent
    (un balayage avant, un balayage arriere, puis une file FIFO sur les pixels frontiere),
    qui donne EXACTEMENT le meme resultat en 2-3 passes. Elle tourne sur la sous-image d'UNE
    composante, pas sur la photo entiere, donc le cout reste modeste.
    """
    m = np.minimum(marker, mask)
    for _ in range(iters):
        nxt = np.minimum(cv2.dilate(m, _K3), mask)
        if np.array_equal(nxt, m):
            break
        m = nxt
    return m


def regional_maxima(f):
    """Les plateaux dont aucun voisin n'est plus haut. f doit etre ENTIER."""
    lower = np.maximum(f.astype(np.int32) - 1, 0).astype(f.dtype)
    return (f.astype(np.int32) - reconstruct(lower, f).astype(np.int32)) > 0


def _grow(markers, mask, rounds):
    """Chaque marqueur regagne le terrain perdu a egalite de vitesse : la frontiere tombe
    a mi-chemin, c'est-a-dire sur le col entre les deux pieces."""
    lab = markers.astype(np.int32)
    for _ in range(rounds):
        todo = (lab == 0) & (mask > 0)
        if not todo.any():
            break
        lab = np.where(todo, cv2.dilate(lab.astype(np.uint16), _K3).astype(np.int32), lab)
    return lab


def _split_one(sub, beta=BETA, h_min=H_MIN, min_child=MIN_CHILD):
    """Etiquettes 1..k pour UNE composante (1 si on ne la coupe pas).

    Le critere n'est pas une taille mais une DYNAMIQUE : de combien un sommet de la carte de
    distance domine le col qui le relie a son voisin. Deux plaques jointes par un pont mince,
    c'est deux sommets a 10 et un col a 2 — dynamique 8. Une plaque seule avec du relief, ce
    sont des bosses a 1-2. Une erosion a plat, elle, jugeait sur la taille et decoupait les
    gros plans en 25 morceaux pour 7 pieces.
    """
    d = np.round(cv2.distanceTransform(sub, cv2.DIST_L2, 5)).astype(np.uint16)
    dmax = int(d.max())
    h = max(h_min, int(round(beta * dmax)))
    if dmax <= h + 1:
        return sub.astype(np.int32)
    hmax = reconstruct(np.maximum(d.astype(np.int32) - h, 0).astype(np.uint16), d)
    rm = (regional_maxima(hmax) & (sub > 0)).astype(np.uint8)
    n, mk = cv2.connectedComponents(rm, 8)
    if n <= 2:                                    # un seul sommet : une seule piece
        return sub.astype(np.int32)
    lab = _grow(mk, sub, dmax * 2 + 8)
    parent = int((sub > 0).sum())
    areas = np.bincount(lab.ravel(), minlength=n)
    for i in range(1, n):
        if not (0 < areas[i] < min_child * parent):
            continue
        m = (lab == i).astype(np.uint8)
        ring = cv2.dilate(m, _K3) - m
        neigh = lab[(ring > 0) & (lab > 0) & (lab != i)]
        lab[lab == i] = np.bincount(neigh).argmax() if neigh.size else 0
    return lab


def split_mask(mask, pad=3):
    """Carte d'etiquettes de toute la photo, pieces collees separees.

    Composante PAR composante : `min_child` n'a de sens que rapporte a la piece qu'on coupe,
    pas a la surface de toutes les pieces de la photo (l'avoir mesure globalement faisait
    reabsorber toutes les etiquettes, et rendait zero boite).
    """
    n, cc, st, _ = cv2.connectedComponentsWithStats(mask, 8)
    out = np.zeros(mask.shape, np.int32)
    H, W_ = mask.shape
    nxt = 0
    for i in range(1, n):
        x, y, bw, bh, _a = (int(v) for v in st[i, :5])
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(W_, x + bw + pad), min(H, y + bh + pad)
        sub = (cc[y0:y1, x0:x1] == i).astype(np.uint8)
        lab = _split_one(sub)
        for v in range(1, int(lab.max()) + 1):
            m = lab == v
            if not m.any():
                continue
            nxt += 1
            region = out[y0:y1, x0:x1]
            region[m] = nxt
            out[y0:y1, x0:x1] = region
    return out


# --- 5. l'appel public -------------------------------------------------------

def segment(img):
    """Rend (boites, w, h) pour une image BGR pleine resolution.

    Chaque boite : {"x","y","w","h" en px de TRAVAIL, "area", "edge"}. `edge` = la boite
    touche le bord du cadre : on ne la JETTE pas, on la SIGNALE (en orange a l'ecran). Ce
    qui touche le bord est soit le bord de la table, soit une main, soit une piece coupee par
    le cadre — dans les trois cas il faut l'oeil de l'utilisateur, pas une decision auto.
    """
    H0, W0 = img.shape[:2]
    small = cv2.resize(img, (W, int(round(H0 * W / float(W0)))), interpolation=cv2.INTER_AREA)
    h, w = small.shape[:2]
    mask = build_mask(small)
    lab = split_mask(mask)
    area = h * w
    out = []
    for i in range(1, int(lab.max()) + 1):
        ys, xs = np.where(lab == i)
        if not len(ys):
            continue
        a = len(ys)
        if a < MIN_AREA * area or a > MAX_AREA * area:
            continue
        x, y = int(xs.min()), int(ys.min())
        bw, bh = int(xs.max() - x + 1), int(ys.max() - y + 1)
        out.append({"x": x, "y": y, "w": bw, "h": bh, "area": a,
                    "edge": x <= 1 or y <= 1 or x + bw >= w - 1 or y + bh >= h - 1})
    out.sort(key=lambda b: (b["y"], b["x"]))
    return out, w, h


def crop(img, box, w, h):
    """La decoupe a envoyer, prise dans la PLEINE RESOLUTION."""
    H0, W0 = img.shape[:2]
    sx, sy = W0 / float(w), H0 / float(h)
    m = MARGIN * max(box["w"], box["h"])
    x0, y0 = int(max(0, (box["x"] - m) * sx)), int(max(0, (box["y"] - m) * sy))
    x1 = int(min(W0, (box["x"] + box["w"] + m) * sx))
    y1 = int(min(H0, (box["y"] + box["h"] + m) * sy))
    c = img[y0:y1, x0:x1]
    s = LONG_SIDE / float(max(c.shape[:2]))
    if s < 1:
        c = cv2.resize(c, (max(1, int(c.shape[1] * s)), max(1, int(c.shape[0] * s))),
                       interpolation=cv2.INTER_AREA)
    return c


if __name__ == "__main__":
    import sys
    img = cv2.imread(sys.argv[1])
    boxes, w, h = segment(img)
    print(f"{len(boxes)} boites ({sum(1 for b in boxes if b['edge'])} au bord)")
