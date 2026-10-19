#!/usr/bin/env python3
"""malscan - scanner statique multi-moteur (ClamAV + YARA + capa + heuristiques).

Aucune execution de l'echantillon, aucun appel reseau externe au scan (offline).
Sortie JSON facon Trivy : empreintes, type, entropie, detections par moteur, verdict.

Usage :
    malscan <fichier> [--pretty]
    malscan --status
"""
import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

YARA_RULES = os.environ.get("MALSCAN_YARA", "/srv/malscan/yara/compiled.yac")
CAPA = os.environ.get("MALSCAN_CAPA", "/opt/malscan/venv/bin/capa")
CAPA_RULES = os.environ.get("MALSCAN_CAPA_RULES", "/srv/malscan/capa-rules")


def _run(cmd, timeout):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -9, "", "timeout"
    except FileNotFoundError:
        return -127, "", "introuvable"


def _hashes(p):
    h = {n: hashlib.new(n) for n in ("md5", "sha1", "sha256")}
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            for x in h.values():
                x.update(c)
    return {n: x.hexdigest() for n, x in h.items()}


def _entropy(p, limit=16 << 20):
    data = Path(p).read_bytes()[:limit]
    if not data:
        return 0.0
    freq = [0] * 256
    for b in data:
        freq[b] += 1
    n = len(data)
    e = 0.0
    for c in freq:
        if c:
            pp = c / n
            e -= pp * math.log2(pp)
    return round(e, 3)


def _magic(p):
    rc, out, _ = _run(["file", "-b", "--", str(p)], 20)
    return out.strip()[:200] if rc == 0 else ""


def _clam(p):
    if shutil.which("clamdscan"):
        rc, out, err = _run(["clamdscan", "--fdpass", "--no-summary", "--", str(p)], 180)
    else:
        rc, out, err = _run(["clamscan", "--no-summary", "--", str(p)], 600)
    sigs = []
    for line in out.splitlines():
        line = line.strip()
        if line.endswith("FOUND") and ": " in line:
            sigs.append(line.split(": ", 1)[1].rsplit(" ", 1)[0])
    return {
        "available": rc != -127,
        "infected": rc == 1,
        "signatures": sigs,
        "error": (err or out).strip()[:300] if rc not in (0, 1) else "",
    }


def _yara(p):
    if not shutil.which("yara") or not os.path.exists(YARA_RULES):
        return {"available": False, "matches": []}
    rc, out, err = _run(["yara", "-C", "-w", YARA_RULES, str(p)], 180)
    matches = sorted({line.split()[0] for line in out.splitlines() if line.strip()})
    return {"available": True, "matches": matches, "error": err.strip()[:200] if rc not in (0, 1) else ""}


def _capa(p, kind):
    if not os.path.exists(CAPA):
        return {"available": False, "ran": False}
    if not any(k in kind for k in ("ELF", "PE32", "executable", "Mach-O")):
        return {"available": True, "ran": False, "reason": "format non supporte"}
    cmd = [CAPA, "-j", "-q"]
    if os.path.isdir(CAPA_RULES) and os.listdir(CAPA_RULES):
        sub = [d for d in Path(CAPA_RULES).iterdir() if d.is_dir()]
        cmd += ["-r", str(sub[0] if sub else CAPA_RULES)]
    cmd += ["--", str(p)]
    rc, out, err = _run(cmd, 300)
    if rc != 0 or not out.strip():
        return {"available": True, "ran": False, "error": err.strip()[:200]}
    try:
        doc = json.loads(out)
    except ValueError:
        return {"available": True, "ran": False, "error": "json illisible"}
    caps, attack = [], set()
    rules = doc.get("rules") or {}
    for rid, r in rules.items():
        meta = r.get("meta", {})
        caps.append(meta.get("name", rid))
        for a in meta.get("attack", []) or []:
            if isinstance(a, dict):
                attack.add(a.get("id") or "::".join(a.get("parts", [])))
            elif isinstance(a, str):
                attack.add(a)
    return {
        "available": True,
        "ran": True,
        "capabilities": sorted(set(caps))[:200],
        "attack": sorted(x for x in attack if x),
    }


def _verdict(res):
    score, reasons = 0, []
    if res["clamav"]["infected"]:
        score = 100
        reasons.append("ClamAV: " + ", ".join(res["clamav"]["signatures"][:3]))
    if res["yara"]["matches"]:
        score = max(score, 80)
        reasons.append("YARA: " + ", ".join(res["yara"]["matches"][:5]))
    if res["entropy"] >= 7.2:
        score = max(score, 40)
        reasons.append("entropie elevee (%.3f) - packer/chiffrement probable" % res["entropy"])
    caps = res.get("capa", {}).get("capabilities", []) or []
    sus = [c for c in caps if any(k in c.lower() for k in (
        "inject", "keylog", "ransom", "anti-", "persist", "shellcode", "c2", "download and execute", "hijack"))]
    if sus:
        score = max(score, 50)
        reasons.append("capa: " + ", ".join(sus[:5]))
    label = "malveillant" if score >= 80 else "suspect" if score >= 40 else "propre"
    return {"label": label, "score": score, "reasons": reasons}


def scan(path):
    p = Path(path)
    if not p.is_file():
        raise SystemExit("fichier introuvable: %s" % path)
    kind = _magic(p)
    res = {
        "path": str(p),
        "size": p.stat().st_size,
        "type": kind,
        "scanned_at": int(time.time()),
        **_hashes(p),
        "entropy": _entropy(p),
        "clamav": _clam(p),
        "yara": _yara(p),
        "capa": _capa(p, kind),
    }
    res["verdict"] = _verdict(res)
    return res


def status():
    def ver(cmd):
        rc, out, err = _run(cmd, 20)
        if rc == -127:
            return "absent"
        line = (out or err).strip().splitlines()
        return line[0][:120] if line else "?"
    db = {}
    for f in ("main", "daily", "bytecode"):
        for ext in ("cvd", "cld"):
            fp = "/var/lib/clamav/%s.%s" % (f, ext)
            if os.path.exists(fp):
                db[f] = int(os.path.getmtime(fp))
    return {
        "clamav": ver(["clamscan", "--version"]),
        "yara": ver(["yara", "--version"]),
        "capa": ver([CAPA, "--version"]) if os.path.exists(CAPA) else "absent",
        "yara_rules": os.path.exists(YARA_RULES),
        "clamav_db_mtime": db,
    }


def main():
    ap = argparse.ArgumentParser(description="Scanner statique multi-moteur (ClamAV+YARA+capa)")
    ap.add_argument("file", nargs="?", help="fichier a scanner")
    ap.add_argument("--status", action="store_true", help="etat des moteurs/signatures")
    ap.add_argument("--pretty", action="store_true", help="JSON indente")
    a = ap.parse_args()
    if a.status:
        print(json.dumps(status(), indent=2))
        return
    if not a.file:
        ap.error("indiquer un fichier ou --status")
    out = scan(a.file)
    print(json.dumps(out, indent=2 if a.pretty else None, ensure_ascii=False))
    v = out["verdict"]
    sys.exit(0 if v["label"] == "propre" else 1)


if __name__ == "__main__":
    main()
