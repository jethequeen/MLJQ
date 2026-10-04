# -*- coding: utf-8 -*-
"""Backup + write orchestration, shared by the CLI and the GUI."""

import os
import shutil
import datetime

from . import config
from . import bsx


def backup_files(set_folder, paths):
    """Copy each path into a timestamped backup folder. Returns the backup dir."""
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    set_name = os.path.basename(set_folder.rstrip("\\/"))
    dest_dir = os.path.join(config.BACKUP_ROOT, stamp, set_name)
    os.makedirs(dest_dir, exist_ok=True)
    for p in paths:
        shutil.copy2(p, os.path.join(dest_dir, os.path.basename(p)))
    return dest_dir


def apply_plan(set_folder, plan, backup=None, force_multiply=False):
    """
    Write the plan's remarks into the real bag files. Backs up first unless
    disabled. Returns (lots_written, backup_dir_or_None).

    force_multiply=True overrides the multiplication guard: every bag is multiplied by
    the set's multiplier even if it looks already multiplied. Use only to recover from
    a false 'divisible' detection — forcing an already-multiplied set doubles it.
    """
    if backup is None:
        backup = config.ENABLE_BACKUP
    paths = [path for _, path, _ in plan["bags"] if plan["remarks"].get(path)]
    backup_dir = backup_files(set_folder, paths) if (backup and paths) else None
    multiplier = plan.get("multiplier", 1)
    total = 0
    for path in paths:
        n, _eff, _state = bsx.write_plan(
            path, plan["remarks"][path], multiplier=multiplier, force=force_multiply)
        total += n

    # The bags are now at physical (xN) quantities; bring the leftovers inventory to the
    # same scale, since the CFB master folds it in as-is.
    from . import verify
    verify.normalize_inventory(set_folder, multiplier)

    # Sorting starts now, so leave the phone what it needs for « je suis au sac N ».
    from . import restants
    restants.write_bag_help(set_folder, plan=plan)
    return total, backup_dir
