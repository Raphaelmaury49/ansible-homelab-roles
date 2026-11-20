#!/usr/bin/env python3
"""Verifie que tous les YAML du role se chargent (ansible-playbook --syntax-check ne parse pas
les fichiers inclus dynamiquement par include_tasks, ex. tasks/server.yml).
Usage : python3 -I check_yaml.py ROLE_DIR"""
import glob
import os
import sys

import yaml

rc = 0
for f in sorted(glob.glob(os.path.join(sys.argv[1], "**", "*.yml"), recursive=True)):
    try:
        with open(f) as fh:
            yaml.safe_load(fh)
    except yaml.YAMLError as e:
        print("KO", f, str(e).splitlines()[0:3])
        rc = 1
    else:
        print("ok", f)
sys.exit(rc)
