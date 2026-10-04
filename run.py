# -*- coding: utf-8 -*-
"""
Sort-and-Pack Helper CLI.

    python run.py "<set folder>"            # dry-run: show the plan, write nothing
    python run.py "<set folder>" --apply    # write <Remarks> into the bag files
    python run.py --all                     # dry-run every set under CATALOGUAGE_ROOT
    python run.py --all --apply             # ...and write them

A "set folder" is one subfolder of the cataloguage root (e.g. ".../76342 - ...").
"""

import os
import sys
import glob
import argparse

sys.stdout.reconfigure(encoding="utf-8")

from sortpack import config
from sortpack.weightdb import WeightDB
from sortpack.plan import build_plan, is_set_folder, iter_set_folders
from sortpack.apply import apply_plan
from sortpack import verify


def report(set_folder, plan):
    s = plan["stats"]
    name = os.path.basename(set_folder.rstrip("\\/"))
    print(f"\n=== {name} ===")
    print(f"  bags: {s['bags']}   unique parts: {s['unique_parts']}")
    print(f"  consolidated parts: {s['consolidated_parts']}   C-bins used: {s['cbins_used']}")
    print(f"  final Sacs: {s['sacs']}   total weight: {s['total_weight_g']/1000:.2f} kg")
    print(f"  ×Quantité (sheet): {s['mult_status']}")
    if s["missing_weight"]:
        print(f"  ⚠️ {len(s['missing_weight'])} parts with no weight: "
              f"{[k[0] for k in s['missing_weight'][:10]]}")
    # sample of the remark plan, in bag order
    print("  sample remarks:")
    shown = 0
    for label, path, items in plan["bags"]:
        rem = plan["remarks"].get(path, {})
        for it in items:
            if it["row"] in rem:
                print(f"    [bag {label:>5}] {it['item_id']:>9} {it['color_name'][:14]:14} "
                      f"x{it['qty']:<4} -> {rem[it['row']]}")
                shown += 1
                if shown >= 12:
                    return


def process(set_folder, wdb, apply, cat=None):
    if apply:
        # verify BEFORE planning: correcting can add or drop lots, and the plan
        # addresses its remarks by row index.
        rep = verify.before_apply(set_folder, cat=cat)
        if rep and rep.get("fixed"):
            print("  (quantités corrigées — plan recalculé)")
    plan = build_plan(set_folder, wdb)
    report(set_folder, plan)
    if apply:
        total, backup_dir = apply_plan(set_folder, plan)
        if backup_dir:
            print(f"  📦 backed up originals to: {backup_dir}")
        print(f"  ✅ wrote remarks into {total} lots.")
    return plan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("set_folder", nargs="?", help="a set subfolder to process")
    ap.add_argument("--all", action="store_true", help="process every set folder")
    ap.add_argument("--apply", action="store_true", help="write files (default: dry-run)")
    args = ap.parse_args()

    wdb = WeightDB()
    cat = None
    if args.apply and getattr(config, "VERIFY_ON_APPLY", True):
        from sortpack.catalogdb import CatalogDB
        cat = CatalogDB()

    if args.all:
        for d in iter_set_folders():        # racine + dossiers de batch
            process(d, wdb, args.apply, cat)
    elif args.set_folder:
        process(args.set_folder, wdb, args.apply, cat)
    else:
        ap.print_help()
        return
    if not args.apply:
        print("\n(dry-run — no files changed. add --apply to write.)")


if __name__ == "__main__":
    main()
