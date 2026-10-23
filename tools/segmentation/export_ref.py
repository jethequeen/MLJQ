# -*- coding: utf-8 -*-
"""Produit la reference du banc : les pixels bruts et ce que le Python en tire.

Le JS et le Python doivent partir des MEMES pixels, sinon on compare deux choses. Ce script
decode les PNG une fois, ecrit le RGBA brut a cote, et enregistre les boites du Python.
`compare.js` relit les deux.

La sortie va dans le dossier temporaire du systeme, pas dans le depot : elle pese ~29 Mo et
se regenere en une commande. Passer un chemin en argument pour la mettre ailleurs.

    python export_ref.py              # -> %TEMP%\\mljq_seg_ref
    python export_ref.py D:\\quelque\\part
"""
import os
import sys
import json
import glob
import tempfile

import cv2
import numpy as np

import segmentation_finale as S

HERE = os.path.dirname(os.path.abspath(__file__))
PHOTOS = os.path.join(HERE, "photos")


def default_out():
    return os.path.join(tempfile.gettempdir(), "mljq_seg_ref")


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else default_out()
    os.makedirs(out, exist_ok=True)
    truth = {}
    try:
        with open(os.path.join(PHOTOS, "verite.json"), encoding="utf-8") as f:
            truth = json.load(f)
    except (OSError, ValueError):
        pass

    ref = {}
    print(f"{'photo':24s}{'vrai':>6s}{'trouve':>8s}{'bord':>6s}")
    for path in sorted(glob.glob(os.path.join(PHOTOS, "*.png"))):
        name = os.path.basename(path)
        img = cv2.imread(path)
        if img is None:
            print(f"{name:24s}  illisible")
            continue
        h, w = img.shape[:2]
        # Les photos du depot sont DEJA a la largeur de travail : segment() les redimensionne
        # vers la meme largeur, donc c'est sans effet. Le RGBA est ecrit tel quel.
        rgba = np.dstack([img[:, :, 2], img[:, :, 1], img[:, :, 0],
                          np.full((h, w), 255, np.uint8)]).astype(np.uint8)
        rgba.tofile(os.path.join(out, name[:-4] + ".rgba"))
        boxes, bw, bh = S.segment(img)
        edge = sum(1 for b in boxes if b["edge"])
        ref[name[:-4]] = {"w": bw, "h": bh, "n": len(boxes), "edge": edge, "boxes": boxes}
        t = truth.get(name)
        print(f"{name:24s}{(t if t else '-'):>6}{len(boxes):>8d}{edge:>6d}")
    with open(os.path.join(out, "python.json"), "w", encoding="utf-8") as f:
        json.dump(ref, f, indent=0)
    print(f"\nreference ecrite dans {out}")
    print("puis :  node compare.js" + ("" if len(sys.argv) <= 1 else f" {out}"))


if __name__ == "__main__":
    main()
