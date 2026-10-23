/* Banc d'essai : le JS contre le Python, sur exactement les memes pixels.
 *
 *     python export_ref.py        (une fois, et apres chaque changement du Python)
 *     node compare.js             (ou : node compare.js <dossier de reference>)
 *
 * Il verifie DEUX choses, et la seconde est la vraie :
 *   - le COMPTE de boites par photo ;
 *   - la GEOMETRIE de chaque boite, au pixel pres. Un compte egal peut cacher deux
 *     decoupages differents qui se compensent.
 *
 * Reference connue (voir README.md) : 102 boites sur 104 identiques au pixel. Les 2 ecarts
 * sont les deux moities d'une meme separation sur 5-noirs-et-roue, decalees de 2 px parce
 * que la carte de distance du JS est le chamfer de cv2 sans en etre la copie bit-a-bit.
 * Si tu vois ces 2-la, tout va bien. Si tu en vois d'autres, quelque chose a bouge.
 */
var fs = require("fs");
var path = require("path");
var os = require("os");
var seg = require("./segmentation.js");

var refDir = process.argv[2] || path.join(os.tmpdir(), "mljq_seg_ref");
var pyPath = path.join(refDir, "python.json");
if (!fs.existsSync(pyPath)) {
  console.error("Pas de reference dans " + refDir + "\nLance d'abord :  python export_ref.py");
  process.exit(1);
}
var ref = JSON.parse(fs.readFileSync(pyPath, "utf8"));
var truth = {};
try {
  truth = JSON.parse(fs.readFileSync(path.join(__dirname, "photos", "verite.json"), "utf8"));
} catch (e) { /* la verite terrain est un confort, pas une dependance */ }

var names = Object.keys(ref).sort();
var totalBoxes = 0, totalSame = 0, countMismatch = [];

console.log("photo                  vrai  python    js  ecart   bord py/js   identiques    ms");
names.forEach(function (n) {
  var w = ref[n].w, h = ref[n].h;
  var buf = fs.readFileSync(path.join(refDir, n + ".rgba"));
  var rgba = new Uint8ClampedArray(buf.buffer, buf.byteOffset, buf.length);
  var t0 = Date.now();
  var js = seg.segBoxes(rgba, w, h);
  var ms = Date.now() - t0;

  var py = ref[n].boxes.slice().sort(function (a, b) { return a.y - b.y || a.x - b.x; });
  var same = 0, lim = Math.min(py.length, js.length);
  for (var i = 0; i < lim; i++) {
    var a = py[i], b = js[i];
    if (a.x === b.x && a.y === b.y && a.w === b.w && a.h === b.h
        && a.area === b.area && !!a.edge === !!b.edge) same++;
  }
  totalBoxes += py.length; totalSame += same;
  var d = js.length - py.length;
  if (d !== 0) countMismatch.push(n);
  var e = js.filter(function (b) { return b.edge; }).length;
  console.log(n.padEnd(22)
    + String(truth[n + ".png"] || "-").padStart(5)
    + String(ref[n].n).padStart(8) + String(js.length).padStart(6)
    + String(d > 0 ? "+" + d : d).padStart(7)
    + (ref[n].edge + "/" + e).padStart(12)
    + (same + "/" + py.length).padStart(13)
    + String(ms).padStart(6));
});

console.log("");
if (countMismatch.length) console.log("ECART DE COMPTE sur : " + countMismatch.join(", "));
console.log(totalSame + "/" + totalBoxes + " boites identiques au pixel pres"
  + (totalSame === totalBoxes ? "" : "   (2 attendues differentes, voir l'en-tete)"));
