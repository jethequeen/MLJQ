# -*- coding: utf-8 -*-
"""
Single-owner lock for the shared Google-Drive folder.

The CFB folder lives on Drive, so every machine with the project installed sees the same
``.bsx`` files, the same ``sort_commands.json`` and the same ``auto_apply.json``. If two of
them run the poller, both will happily process the queue and auto-apply — and whichever
sync lands last wins. That is what re-applied three finished sets on 2026-09-23 at 12:11:
a second machine holding a stale copy of the folder saw the bags un-remarked and wrote
them again.

So one machine owns the work. ``poller_owner.json`` in the CFB folder carries the owner's
name and a heartbeat; everyone else stands down. If the owner goes quiet for
``config.POLLER_LOCK_STALE_MINUTES`` (laptop closed, machine retired), the next machine to
look takes over — the work never stalls for good.

**This is a courtesy lock, not a mutex.** Drive propagates a write in seconds to minutes, so
two machines starting at the same instant can both believe they own it. It is sized for the
real problem (a forgotten install quietly fighting the main PC for days), not for a race.
Nothing destructive rests on it alone: the multiplication marker still stops a double ×N,
the verification still fixes quantities, and every write is still backed up.
"""

import os
import json
import socket
import datetime

from . import config

_LAST_BEAT = None          # when this process last wrote the heartbeat (throttling)
_WARNED = None             # who we last told the user was holding the lock


def lock_path():
    return getattr(config, "POLLER_LOCK_FILE",
                   os.path.join(config.CATALOGUAGE_ROOT, "poller_owner.json"))


def machine_name():
    """This machine's name — config override, else the hostname."""
    return (getattr(config, "POLLER_MACHINE_NAME", "") or socket.gethostname() or "?").strip()


def _now():
    return datetime.datetime.now()


def _parse(ts):
    try:
        return datetime.datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None


def read_owner():
    """The lock file as a dict, or None when absent/unreadable."""
    try:
        with open(lock_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_owner(data):
    try:
        with open(lock_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


def status():
    """Who holds the lock right now: {owner, mine, free, stale, age_minutes, since}."""
    me = machine_name()
    data = read_owner()
    if not data:
        return {"owner": None, "mine": False, "free": True, "stale": False,
                "age_minutes": None, "since": None, "me": me}
    beat = _parse(data.get("heartbeat")) or _parse(data.get("since"))
    age = None if beat is None else max(0.0, (_now() - beat).total_seconds() / 60.0)
    stale_after = getattr(config, "POLLER_LOCK_STALE_MINUTES", 10)
    return {"owner": data.get("machine"), "mine": data.get("machine") == me,
            "free": False, "stale": age is None or age > stale_after,
            "age_minutes": age, "since": data.get("since"), "me": me}


def acquire(force=False, log=None):
    """Claim the lock, or refresh it when it is already ours. Returns True when this machine
    owns the work afterwards.

    Taken when the file is missing, already ours, gone stale, or `force` (the user asked THIS
    machine to do the work) or config.POLLER_FORCE_OWNER (the main PC always wins)."""
    global _LAST_BEAT, _WARNED
    me = machine_name()
    st = status()
    take = (st["free"] or st["mine"] or st["stale"] or force
            or getattr(config, "POLLER_FORCE_OWNER", False))
    if not take:
        if log and _WARNED != st["owner"]:
            _WARNED = st["owner"]
            log(f"⏸ Traitement en pause : « {st['owner']} » tient le verrou "
                f"(actif il y a {st['age_minutes']:.0f} min). Ce poste ne touchera à rien.")
        return False

    # Refresh at most every POLLER_HEARTBEAT_SECONDS: this file lives on Drive and a write
    # every poll would be pure sync churn.
    every = getattr(config, "POLLER_HEARTBEAT_SECONDS", 120)
    now = _now()
    if (st["mine"] and not force and _LAST_BEAT is not None
            and (now - _LAST_BEAT).total_seconds() < every):
        return True

    if log and not st["mine"] and not st["free"]:
        why = "verrou périmé" if st["stale"] else "demandé sur ce poste"
        log(f"🔑 Reprise du verrou à « {st['owner']} » ({why}).")
    _write_owner({"machine": me, "pid": os.getpid(),
                  "since": (st["since"] if st["mine"] and st["since"]
                            else now.isoformat(timespec="seconds")),
                  "heartbeat": now.isoformat(timespec="seconds")})
    _LAST_BEAT = now
    _WARNED = None
    return True


def owns(log=None):
    """The gate the poller calls: does this machine own the work (refreshing our heartbeat
    along the way)? Disabled by config.POLLER_LOCK = False, which makes everything run as
    before — only do that if a single machine is ever going to run this."""
    if not getattr(config, "POLLER_LOCK", True):
        return True
    return acquire(force=False, log=log)


def release(log=None):
    """Give up the lock (only if it is ours), so another machine can pick the work up at
    once instead of waiting for the staleness timeout."""
    global _LAST_BEAT
    st = status()
    if not st["mine"]:
        return False
    try:
        os.remove(lock_path())
        _LAST_BEAT = None
        if log:
            log("🔓 Verrou libéré.")
        return True
    except OSError:
        return False


def describe():
    """One line for the GUI / CLI."""
    st = status()
    if st["free"]:
        return f"Verrou libre — ce poste ({st['me']}) prendra le travail."
    if st["mine"]:
        return f"Ce poste ({st['me']}) tient le verrou depuis {st['since'] or '?'}."
    age = "?" if st["age_minutes"] is None else f"{st['age_minutes']:.0f}"
    tail = " — périmé, reprenable" if st["stale"] else ""
    return f"Verrou tenu par « {st['owner']} » (actif il y a {age} min){tail}."


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="Qui traite le travail partagé (file du téléphone + auto-application) ?")
    ap.add_argument("--take", action="store_true", help="prendre le verrou pour ce poste")
    ap.add_argument("--release", action="store_true", help="libérer le verrou de ce poste")
    args = ap.parse_args()
    if args.take:
        acquire(force=True, log=print)
    elif args.release:
        if not release(log=print):
            print("Rien à libérer — ce poste ne tient pas le verrou.")
    print(describe())


if __name__ == "__main__":
    main()
