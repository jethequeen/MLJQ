# MLJQ - Sort and Pack Helper

## What this project does
Iterate **daily** over a set of BrickStore parts lists and write **remarks** (a BSX
field) onto parts so the user can sort and pack physical LEGO parts efficiently.
The remarks are computed from:
- values read from the **BrickStore database** (the sqlite DB, reading a value on
  the blob — but **not** the same value the reference projects read), and
- data already present in the **`.bsx` XML lists** themselves.

Core loop, roughly:
1. Read the target `.bsx` lists (BrickStore's XML format).
2. Look up the relevant value(s) per part from the BrickStore DB blob.
3. Compute a sort/pack remark per part.
4. Write the remark back into each part's `<Remarks>` field in the `.bsx`.

## Reference code to reuse (do NOT copy — read in place)
All in the sibling folder `..\Automatisation\`:

- `Automatisation\getInventories.py` — launches/kills `brickstore.exe`, detects
  `.bsx` / `.xml` inventory files. Shows how the project drives BrickStore and
  where inventory files live.
- `Automatisation\getInventories_experimental.py` — experimental variant of the above.
- `Automatisation\selling_ratios.py`, `Automatisation\run_brickstore_ratios.py`,
  `Automatisation\infoSets.py` — these touch the BrickStore DB / sqlite / blob.
  This is the pattern for **reading a value on the blob** — we do the same
  mechanism but read a different value.

Note: `Automatisation\SetsEnRabais.py` is a web scraper (Brickset/Amazon/Costco)
and is **not** relevant to this project despite being the file first mentioned.

## Another related project
The user has a second project that also iterates over lists and writes BSX fields;
it will be pointed to and added here as a reference when available.

## BSX / BrickStore notes
- `.bsx` files are XML (BrickStore's native format). Each part item has fields
  including `<Remarks>` — that's the field we write.
- BrickStore's exe on this machine: `C:\Program Files\BrickStore\brickstore.exe`.
- The BrickStore database is a local sqlite DB; the reference `*ratios*` scripts
  show how to open it and read the blob value.

## Environment
- Windows. The Automatisation project uses a `venv` at `..\Automatisation\venv`.
  Decide whether to reuse it or create a dedicated one for this project.
- Lives on Google Drive (`G:\Mon Disque\`), so avoid committing large/venv files.

## Open questions (confirm before building)
- Which exact DB value drives the remark? (different from what the ratio scripts read)
- Which `.bsx` lists are the daily targets, and where do they live?
- Exact remark format / sort-and-pack logic.
