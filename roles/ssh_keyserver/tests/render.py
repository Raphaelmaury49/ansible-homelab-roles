#!/usr/bin/env python3
"""Rend roles/ssh_keyserver/templates/ssh-keys-fetch.j2 avec les defaults du role.

Usage : python3 -I render.py ROLE_DIR OUT [--set cle=valeur ...]
Les valeurs de --set sont interpretees en YAML (true, 12, [a, b]...).
Hors Ansible : filtre regex_replace reimplemente (re.sub), ansible_managed fixe.
Seul le rendu est local : aucune connexion, aucun acces a l'infra.
"""
import argparse
import os
import re
import sys

import jinja2
import yaml


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("role_dir")
    ap.add_argument("out")
    ap.add_argument("--set", action="append", default=[], metavar="K=V")
    a = ap.parse_args()

    with open(os.path.join(a.role_dir, "defaults", "main.yml")) as f:
        ctx = yaml.safe_load(f)
    for kv in a.set:
        k, v = kv.split("=", 1)
        ctx[k] = yaml.safe_load(v)
    ctx["ansible_managed"] = "Ansible managed"

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(os.path.join(a.role_dir, "templates")),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=False,
    )
    env.filters["regex_replace"] = lambda s, pat, rep="": re.sub(pat, rep, str(s))
    out = env.get_template("ssh-keys-fetch.j2").render(**ctx)
    with open(a.out, "w") as f:
        f.write(out)
    os.chmod(a.out, 0o755)


if __name__ == "__main__":
    sys.exit(main())
