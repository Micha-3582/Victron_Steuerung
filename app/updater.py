"""
Update-Mechanismus über GitHub (git-basiert).
Prüft, ob im öffentlichen Repo eine neuere Version liegt, und aktualisiert per
`git pull`. Nutzerdaten (config.json, state.json, ...) bleiben unberührt, weil
sie in .gitignore stehen.
"""
import os
import subprocess

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _git(*args, timeout=90):
    return subprocess.run(["git", "-C", APP_DIR, *args],
                          capture_output=True, text=True, timeout=timeout)


def is_git_repo() -> bool:
    try:
        r = _git("rev-parse", "--is-inside-work-tree", timeout=10)
        return r.returncode == 0 and r.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def current_version() -> str:
    vf = os.path.join(APP_DIR, "VERSION")
    ver = ""
    if os.path.exists(vf):
        with open(vf, encoding="utf-8") as f:
            ver = f.read().strip()
    short = ""
    if is_git_repo():
        r = _git("rev-parse", "--short", "HEAD", timeout=10)
        short = r.stdout.strip()
    if ver and short:
        return f"{ver} ({short})"
    return ver or short or "unbekannt"


def check_update() -> dict:
    """Holt den Remote-Stand und meldet, ob ein Update verfügbar ist."""
    if not is_git_repo():
        return {"git": False, "current": current_version(),
                "reason": "Keine Git-Installation – Update über GitHub nicht möglich."}
    f = _git("fetch", "--quiet")
    if f.returncode != 0:
        return {"git": True, "current": current_version(), "update_available": False,
                "reason": f"Kein Zugriff auf GitHub: {f.stderr.strip()[:200]}"}
    local = _git("rev-parse", "HEAD").stdout.strip()
    try:
        remote = _git("rev-parse", "@{u}").stdout.strip()
        behind = _git("rev-list", "--count", "HEAD..@{u}").stdout.strip()
    except Exception:                                    # noqa: BLE001
        return {"git": True, "current": current_version(), "update_available": False,
                "reason": "Kein Upstream-Branch gesetzt."}
    return {"git": True, "current": current_version(),
            "update_available": bool(local and remote and local != remote),
            "behind": int(behind or 0)}


def do_update() -> dict:
    """Führt `git pull --ff-only` aus. Startet NICHT neu (das macht der Aufrufer)."""
    if not is_git_repo():
        return {"ok": False, "output": "Keine Git-Installation."}
    r = _git("pull", "--ff-only")
    out = (r.stdout + r.stderr).strip()[-2000:]
    return {"ok": r.returncode == 0, "output": out, "version": current_version()}
