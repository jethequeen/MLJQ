# Découpage d'une photo de pièces — la référence et son banc d'essai

Le cataloguage par photo commence par **découper une photo en pièces individuelles**, une
découpe par pièce, chacune envoyée à Brickognize. Ce dossier tient l'algorithme de découpage,
les photos sur lesquelles il a été réglé, et de quoi vérifier en trente secondes qu'un
changement ne l'a pas cassé.

Il ne contient aucun code d'application : l'appariement avec l'inventaire, l'écran de
validation et l'écriture du `.bsx` vivent ailleurs (`sortpack/photocat.py`,
`appscript/SortPack_mobile*`).

## ⚠ `segmentation.js` est une COPIE

Le code qui tourne vraiment est **inliné dans `appscript/SortPack_mobile_page.html`** : la
page du téléphone est un seul script, sans dépendance externe. Le fichier ici en est la copie
de référence, celle qu'on peut tester hors navigateur.

**Les deux peuvent diverger sans que rien ne proteste.** Si tu touches au découpage, change
les deux, et refais passer le banc ci-dessous sur celui d'ici.

## Rejouer le banc

```bash
python export_ref.py     # décode les photos et enregistre ce que le Python en tire
node compare.js          # rejoue le JS sur les MÊMES pixels et compare
```

`export_ref.py` écrit dans le dossier temporaire du système (~29 Mo de pixels bruts,
régénérables) — rien n'atterrit dans le dépôt. Il faut `numpy` + `opencv-python` pour le
Python ; le JS n'a besoin de rien.

Ce que le banc vérifie, et la seconde chose est la vraie :

1. le **compte** de découpes par photo ;
2. la **géométrie** de chaque boîte, au pixel près — un compte égal peut cacher deux
   découpages différents qui se compensent.

Résultat attendu aujourd'hui : **102 boîtes sur 104 identiques au pixel**. Les 2 écarts sont
les deux moitiés d'une même séparation sur `5-noirs-et-roue`, décalées de 2 px parce que la
carte de distance du JS reprend les poids d'OpenCV sans en être la copie bit-à-bit. Si tu vois
ces deux-là, tout va bien. Si tu en vois d'autres, quelque chose a bougé.

## Les photos

Six photos réelles du set **42226 — BMW M4 GT3 EVO**, prises par Jérémie dans ses conditions
de travail. `1-dense` est son mode préféré : une seule photo, tout le sac étalé. Les cinq
autres sont des sous-groupes des mêmes pièces, en gros plan.

`photos/verite.json` porte le nombre de pièces **réellement posées**, compté à l'œil. C'est la
seule vérité du banc ; l'écart entre elle et ce que le découpage trouve est la mesure qui
compte.

**Elles sont en PNG, et ça n'est pas un détail.** Elles sont déjà à la largeur de travail
(1100 px) pour que le banc parte des pixels exacts. Les ré-encoder en JPEG qualité 95 — testé —
**change les comptes sur 3 photos sur 6**, dont la dense qui tombe à 44. Un banc qui ne
reproduit pas ne vaut rien, donc : PNG, sans perte, et on paie les 9 Mo.

## Ce que le découpage vaut, mesuré

Sur `1-dense` (47 pièces posées), chaîne complète jusqu'à l'inventaire réel du set :

```
45 découpes, 0 au bord
36 reconnues au rang 1 par Brickognize
 8 repêchées par l'inventaire du set
 1 sans correspondance, signalée
```

**Deux pièces manquent, et aucun seuil ne les récupère** : en abaissant le seuil clair, le
compte *baisse* (44, puis 43). Dans cette photo-là, elles sont à la même luminance que la
table sous l'ombre — il n'y a rien à seuiller. C'est ce que le geste « taper sur la photo pour
ajouter une pièce oubliée » vient couvrir, côté page.

Temps : 0,33 à 0,79 s sur Node desktop, 0,44 s sur la dense. Compter 1 à 2 s sur un iPhone.

## Trois résultats qui ont décidé de la forme du truc

**Une fusion donne une réponse sûre et fausse.** Deux plaques collées dans une même découpe
ont rendu « Plate 2x14 » avec 0,84 de confiance. Ni le score ni l'inventaire ne rattrapent ça.
Seule la validation visuelle du découpage l'attrape — cette étape porte la justesse, elle n'est
pas un confort. Corollaire : **sur-découper est le défaut bon marché** (un fragment ne trouve
rien dans l'inventaire et se signale tout seul), fusionner est le défaut cher. Le réglage de la
séparation penche de ce côté exprès.

**Cinq candidats ne suffisent pas.** Deux découpes n'avaient aucun candidat appartenant au set
dans les 5 premiers ; elles sortent au **rang 17**, Brickognize préférant obstinément `2476`,
absent de ce set. D'où `top_k_items=20`, `top_k_colors=5`, `min_similarity_items=0.10`.

**L'inventaire du set n'est pas un garde-fou, c'est la moitié de la reconnaissance.** 8 fois
sur 45, le bon choix n'était pas le candidat brut.

## Les seuils, et l'échec que chacun corrige

Détail complet en tête de `segmentation.js` et de `segmentation_finale.py`. Les trois qui
comptent :

| réglage | pourquoi il vaut ça |
|---|---|
| luminance **asymétrique** (+0,14 / −0,34) | une ombre ne va QUE vers le bas (−20 à −35 %) et rien n'éclaircit le fond. Symétrique à 0,34, la plaque Light Bluish Gray (+25 %) était invisible. |
| teinte mesurée **contre le fond local** (0,115) | un seuil de saturation absolu mettait 21,6 % de l'image en « couleur » — le gris de la table sous lumière chaude est teinté, et toute la zone éclairée devenait une seule pièce géante. |
| séparation par **dynamique relative** (0,30 × épaisseur) | en pixels absolus, le même réglage coupait une roue en gros plan et laissait collées deux plaques vues de loin. |

Et le fond est une **grille médiane**, pas une surface ajustée : la surface a été essayée et
rejetée. Elle répare les gros plans mais ne suit pas une ombre portée, et sur la photo dense
elle faisait passer quatre grandes zones de table pour des pièces.

## Une réserve, pour qui compare au navigateur

Le banc tourne sur des pixels réduits par OpenCV (`INTER_AREA`). La page **ne délègue pas** sa
réduction à `drawImage` : son filtre n'est pas spécifié et change d'un navigateur à l'autre, et
le réducteur fait partie de l'algorithme — mesuré, deux filtres différents donnent 104 boîtes
ou 94, soit un dixième des pièces. Elle calcule donc elle-même une moyenne de zone dont les
pixels de bord comptent **au prorata** de leur recouvrement, ce qui reproduit `INTER_AREA` ;
leur donner une part entière décale l'image d'un demi-pixel et tombe à 95. La chaîne complète,
réduction comprise, retrouve bien 45 sur la dense.

Deux différences restent, irréductibles. Le **décodeur JPEG** du navigateur n'est pas celui
d'OpenCV, donc attends-toi à ±1 boîte de temps en temps par rapport aux chiffres d'ici — ce
n'est pas une régression. Et l'**orientation EXIF** doit être appliquée au décodage
(`createImageBitmap(file, {imageOrientation:'from-image'})`), sinon tout part couché : c'est
elle qui expliquait un écart de 104 contre 96 qu'on a d'abord imputé au portage.

Garde la largeur de travail à ~1100 px. Les seuils sont réglés pour cette échelle, et la
taille de cellule de la grille de fond (55 px) en dépend directement.
