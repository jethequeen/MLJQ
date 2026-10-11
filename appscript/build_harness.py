# -*- coding: utf-8 -*-
"""Monte un banc d'essai LOCAL de la page telephone.

On rejoue en Python ce que SortPack_mobile.gs renvoie (getSets / getRestants /
getDashboard), a partir des VRAIS fichiers JSON du dossier CFB, puis on injecte un
google.script.run bouchonne juste avant le script de la page. But : voir les erreurs JS et
la mise en page sans toucher au deploiement.

ATTENTION : ce fichier REJOUE SortPack_mobile.gs. Il derive donc des que le .gs change, et
la page semble alors buguee alors que c'est le banc qui ment — c'est arrive le 2026-10-11
avec les champs conso_*. Quand tu touches a getSets, touche ici aussi.
"""
import io, json, os, re, sys

CFB = r"G:\Mon Disque\BSX\CFB"
PAGE = r"G:\Mon Disque\MLJQ - Sort and Pack Helper\appscript\SortPack_mobile_page.html"
OUT = "G:/Mon Disque/MLJQ - Sort and Pack Helper/_cfb_debug/phone_harness.html"


def rj(name, default=None):
    try:
        with io.open(os.path.join(CFB, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# --- getSets(), tel que le .gs le construit ---------------------------------
idx = rj("sort_index.json", {"sets": []})
st = rj("set_status.json", {}) or {}
cmds = (rj("sort_commands.json", {"commands": []}) or {}).get("commands") or []

latest = {}
for c in cmds:
    if c.get("key") and c.get("action") in ("restants", "build_cfb"):
        latest[c["key"]] = c

sets = []
for s in idx.get("sets", []):
    e = st.get(s["key"]) or {}
    bags = int(s.get("bag_count") or 0)
    done = [b for b in (e.get("bags_done") or []) if 1 <= b <= bags]
    row = {
        "name": s.get("name"), "key": s.get("key"), "number": s.get("number") or "",
        "base": s.get("base_status"), "bags": bags, "done": done,
        "cataloguing": bool(s.get("cataloguing")),
        "batch": s.get("batch") or "", "batch_open": bool(s.get("batch_open")),
        "separated": True if s.get("separated") is None else bool(s.get("separated")),
        "phase": e.get("status") or "",
        "sorting": (e.get("status") == "Trier") or len(done) > 0,
        "has_inventory": True if s.get("has_inventory") is None else bool(s.get("has_inventory")),
        "restants_done": bool(s.get("restants_done")),
    }
    c = latest.get(s["key"])
    if c:
        row["cmd"] = {"action": c["action"], "status": c["status"], "result": c.get("result") or ""}
    sets.append(row)

# --- les envois + la derniere demande de chaque action d'envoi --------------
BATCH_ACTIONS = ("batch_check", "batch_cfb")
reg = rj("batches.json", {}) or {}
bcmd = {}
for c in cmds:
    if c.get("action") not in BATCH_ACTIONS:
        continue
    n = str(((c.get("payload") or {}).get("batch")) or "")
    if not n:
        continue
    bcmd.setdefault(n, {})[c["action"]] = {
        "action": c["action"], "status": c["status"], "result": c.get("result") or "",
        "created": c.get("created") or "", "finished": c.get("finished") or ""}

# L'etat de la consolidation vient de l'index, comme dans le .gs : le telephone n'ouvre
# jamais de .bsx, c'est le PC qui le lui dit.
conso = {str(r.get("name")): r for r in (idx.get("batches") or []) if r.get("name")}
batches = []
for n in sorted(reg):
    c = conso.get(n, {})
    batches.append({"name": n, "closed": bool(reg[n].get("closed")),
                    "created": reg[n].get("created") or "",
                    "sets": [str(x) for x in (reg[n].get("sets") or [])],
                    "cmds": bcmd.get(n, {}),
                    "conso_written": bool(c.get("conso_written")),
                    "conso_done": bool(c.get("conso_done")),
                    "conso_lots": int(c.get("conso_lots") or 0),
                    "conso_groups": int(c.get("conso_groups") or 0)})

# --- getRestants() pour chaque set ------------------------------------------
restants = {}
for s in sets:
    num = s["number"]
    d = rj("restants_%s.json" % num)
    if d is not None:
        d["ok"] = True
        d["cmds"] = {}
        restants[str(num)] = d

harness = {"generated": idx.get("generated") or "", "sets": sets, "batches": batches,
           "restants": restants, "dash": rj("dashboard.json", {}) or {}}

PRELUDE = """
<script>
/* ---- BANC D'ESSAI LOCAL : google.script.run bouchonne ------------------- */
var HARNESS = __DATA__;
window.__ERRORS__ = [];
window.addEventListener('error', function(e){
  window.__ERRORS__.push(String(e.message) + ' @' + (e.lineno||'?'));
});
(function(){
  function reply(v, ms){
    var api = this;
    setTimeout(function(){ try { api._ok && api._ok(v); } catch(err){
      window.__ERRORS__.push('handler: ' + err.message); } }, ms || 30);
  }
  function maker(){
    var api = {_ok:null, _fail:null};
    api.withSuccessHandler = function(f){ api._ok = f; return api; };
    api.withFailureHandler = function(f){ api._fail = f; return api; };
    api.getSets = function(){ reply.call(api, {generated:HARNESS.generated,
      sets:HARNESS.sets, batches:HARNESS.batches}); };
    api.getRestants = function(n){ reply.call(api,
      HARNESS.restants[String(n)] || {ok:false, error:'pas de liste pour ' + n}, 60); };
    api.getDashboard = function(){ reply.call(api, HARNESS.dash); };
    api.setBags = function(){ reply.call(api, {ok:true}); };
    api.setPhase = function(){ reply.call(api, {ok:true}); };
    api.finishSeparating = function(){ reply.call(api, {ok:true}); };
    api.closeBatch = function(){ reply.call(api, {ok:true}); };
    api.queueCommand = function(k, a, p){
      window.__ERRORS__.push('[queueCommand] ' + a + ' ' + JSON.stringify(p || {}));
      reply.call(api, {ok:true, id:'stub', status:'pending'});
    };
    api.logPurchase = function(){ reply.call(api, {ok:true}); };
    return api;
  }
  window.google = { script: { get run(){ return maker(); } } };
})();
</script>
"""

page = io.open(PAGE, encoding="utf-8").read()
prelude = PRELUDE.replace("__DATA__", json.dumps(harness, ensure_ascii=False))

# juste avant le <script> principal de la page
m = list(re.finditer(r"<script>", page))
if not m:
    sys.exit("pas de <script> dans la page")
at = m[-1].start()
io.open(OUT, "w", encoding="utf-8", newline="").write(page[:at] + prelude + page[at:])
print("harness ecrit :", OUT)
print("  sets:", len(sets), " envois:", len(batches),
      " index restants:", list(restants.keys()))
