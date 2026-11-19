# Tests du role ssh_keyserver

Tests 100 % locaux du script `ssh-keys-fetch` (serveur HTTPS sur 127.0.0.1, certificat auto-signe,
aucun acces reseau externe ni a l'infra). Les `.keys` / `.sig` de test sont generes a la volee.

Prerequis : python3 avec `jinja2` et `pyyaml` (venv recommande), `curl`, `openssh-client`, `openssl`.
Facultatif : `dash`, `busybox`, `shellcheck`, `ansible-lint`, `ansible-core`.

```bash
python3 -m venv --system-site-packages /tmp/venv && /tmp/venv/bin/pip install jinja2 pyyaml shellcheck-py
# depuis un dossier hors depot ; WORK_DIR est recree
/tmp/venv/bin/python -I roles/ssh_keyserver/tests/run_tests.py roles/ssh_keyserver /tmp/sshks-work \
    --shells "sh;dash;busybox sh" [--busybox /chemin/busybox] [--json res.json] [-k motif]
/tmp/venv/bin/python -I roles/ssh_keyserver/tests/check_yaml.py roles/ssh_keyserver
```

- `render.py` : rend `ssh-keys-fetch.j2` avec `defaults/main.yml` (+ `--set cle=valeur`), hors Ansible
  (filtre `regex_replace` reimplemente).
- `server.py` : serveur HTTPS dont la reponse est pilotee par `scenario.json` (200/404/403/500, timeout,
  > 16 Kio, connexion fermee, redirection...).
- `run_tests.py` : rejoue la matrice du README (+ cas limites : utilisateurs invalides, cles, serial, cache age)
  pour chaque shell. Verifie stdout, code retour (0), cache (contenu, mtime, 0600, residus), `.serial`,
  appels `logger` (stub) et requetes recues.

Pour le test uniquement, la ligne `PATH=/usr/local/...` du script rendu est reecrite (stub `logger` en
tete) ; la CA est choisie par `CURL_CA_BUNDLE` (le script ne la fixe pas, curl la lit dans l'environnement).
Le script de production n'est pas modifie par ces reecritures.
