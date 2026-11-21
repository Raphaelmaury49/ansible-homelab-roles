# uptime_kuma

Uptime Kuma **2.x** en installation native (sans Docker) : clone du tag git, `npm ci --omit dev`,
`npm run download-dist`, service systemd durci, port filtre par nftables.

Le role ne contient aucune donnee privee. A jouer apres `base` (qui cree `/etc/nftables.d` et active nftables),
sur Debian 13 (testee uniquement en relecture, voir la section Tests).

## Ce que fait le role

1. garde-fous : `uptime_kuma_http_sources` non vide (**obligatoire**), `uptime_kuma_version` = tag 2.x, port valide ;
2. paquets Debian : `git`, `nodejs`, `npm`, `iputils-ping` (+ `build-essential`, `python3` en repli pour compiler le module natif sqlite) ;
3. verifie `node >= 20.4.0` (exigence `engines` de Kuma 2.5.x ; Debian 13 fournit Node 20.19, donc pas de NodeSource) ;
4. utilisateur/groupe systeme `uptime-kuma` (sans shell, home = repertoire de donnees) ;
5. `git clone` du tag dans `/opt/uptime-kuma` (root:root : le service ne peut pas modifier son propre code) ;
6. si necessaire (voir ci-dessous) : arret du service, copie de `kuma.db`, `npm ci --omit dev --no-audit`, `npm run download-dist`, controle de `dist/index.html`, ecriture du marqueur `/opt/uptime-kuma/.ansible-build` ;
7. unite `/etc/systemd/system/uptime-kuma.service` durcie, regle `/etc/nftables.d/uptime-kuma.nft` ;
8. `enabled` + `started`, puis controle HTTP sur `http://127.0.0.1:<port>/` (200 ou 302, 60 essais x 5 s par defaut).

### Idempotence et mise a jour

Le build n'est rejoue que si l'une de ces conditions est vraie : le module `git` a change le depot
(nouveau tag), `node_modules` ou `dist/index.html` manquent, le marqueur `<version>|node<majeur>` differe
(changement de version, ou de version majeure de Node : le module natif depend de l'ABI), ou
`uptime_kuma_force_rebuild: true`. Sinon : zero commande npm, zero restart.

Mise a jour : changer `uptime_kuma_version`, relancer. Sequence : fetch du tag, arret du service, copie
`kuma.db.pre-<nouvelle version>`, `npm ci`, `download-dist`, restart (handler), controle HTTP.

## Variables

| Variable | Defaut | Role |
|---|---|---|
| `uptime_kuma_http_sources` | `[]` (**obligatoire**) | CIDR autorisees sur le port (nftables). Le role echoue si vide |
| `uptime_kuma_version` | `2.5.12` | tag git (sans `v`), 2.x uniquement |
| `uptime_kuma_repo` | `https://github.com/louislam/uptime-kuma.git` | depot |
| `uptime_kuma_git_depth` | `1` | profondeur du clone (0 = complet) |
| `uptime_kuma_user` / `uptime_kuma_group` | `uptime-kuma` | compte systeme |
| `uptime_kuma_install_dir` | `/opt/uptime-kuma` | code |
| `uptime_kuma_data_dir` | `/var/lib/uptime-kuma` | `DATA_DIR` : `kuma.db`, `upload/`, `db-config.json` |
| `uptime_kuma_host` | `0.0.0.0` | `UPTIME_KUMA_HOST` |
| `uptime_kuma_port` | `3001` | `UPTIME_KUMA_PORT` (la page de statut est servie sur le meme port) |
| `uptime_kuma_node_bin` / `uptime_kuma_node_min` | `/usr/bin/node` / `20.4.0` | Node.js |
| `uptime_kuma_packages` / `uptime_kuma_build_packages` | voir `defaults/main.yml` | paquets apt |
| `uptime_kuma_extra_env` | `{}` | variables d'environnement supplementaires du service |
| `uptime_kuma_force_rebuild` | `false` | rejoue `npm ci` + `download-dist` |
| `uptime_kuma_backup_db_on_upgrade` | `true` | copie `kuma.db.pre-<version>` avant chaque build (jamais ecrasee) |
| `uptime_kuma_healthcheck` / `_retries` / `_delay` | `true` / `60` / `5` | controle HTTP apres demarrage |

Environnement pose dans l'unite : `NODE_ENV=production`, `DATA_DIR=<data_dir>/`, `UPTIME_KUMA_HOST`,
`UPTIME_KUMA_PORT`, `UPTIME_KUMA_DB_TYPE=sqlite` (+ `uptime_kuma_extra_env`).
SQLite est impose : sans cela, un premier demarrage sans `kuma.db` affiche l'assistant de choix de base.
MariaDB embarque n'est propose que si `UPTIME_KUMA_ENABLE_EMBEDDED_MARIADB=1` (variable de l'image Docker, absente ici).

### Unite systemd

`NoNewPrivileges`, `ProtectSystem=strict` avec `ReadWritePaths=<data_dir>`, `ProtectHome`, `PrivateTmp`,
`PrivateDevices`, `ProtectKernel*`, `ProtectControlGroups`, `RestrictSUIDSGID`, `RestrictNamespaces`, `UMask=0077`.
`CAP_NET_RAW` en capacite ambiante : necessaire aux moniteurs ping (le `ping` setuid/capabilities ne fonctionne
plus sous `NoNewPrivileges`). Pas de `MemoryDenyWriteExecute` (incompatible avec le JIT de Node).
Les moniteurs de type Docker (socket) et navigateur reel (Chromium) ne sont pas fournis par ce role.

## Exemple

```yaml
# host_vars/kuma.yml
iac_roles: [base, ssh_keyserver, uptime_kuma]
ssh_keyserver_mode: client
uptime_kuma_version: "2.5.12"
uptime_kuma_port: 3001
uptime_kuma_http_sources:
  - 192.168.2.0/24        # NPM, Prometheus, Homepage, Kuma push : a restreindre aux IP utiles
```

```bash
ansible-playbook site.yml --limit kuma -e ansible_user=root   # bootstrap
ansible-playbook site.yml --limit kuma --check --diff         # plan
```

## Migration depuis Kuma 1.x (Docker)

Le role n'a pas de variable de restauration : la copie de `kuma.db` est une operation a part,
**avant le premier demarrage** du service.

1. Sur l'ancien hote, arreter proprement Kuma 1.x (`docker stop`) pour que le journal WAL soit integre dans `kuma.db`.
   Garder l'ancien conteneur/CT intact : c'est le rollback.
2. Creer le repertoire de donnees sur le nouvel hote, y deposer `kuma.db` (et `upload/` si des icones/logos sont utilises ;
   `kuma.db-wal` et `kuma.db-shm` seulement s'ils existent) :
   ```bash
   install -d -o uptime-kuma -g uptime-kuma -m 0750 /var/lib/uptime-kuma   # ou laisser le role le creer, puis ne PAS le demarrer
   cp kuma.db /var/lib/uptime-kuma/kuma.db
   chown -R uptime-kuma:uptime-kuma /var/lib/uptime-kuma
   ```
   (le role corrige de toute facon le proprietaire du repertoire de donnees.)
3. Jouer le role. Il copie d'abord `kuma.db` en `kuma.db.pre-<version>` (base 1.x d'origine), puis demarre.
4. Au premier demarrage 2.x, **sans action manuelle** :
   - `kuma.db` existe sans `db-config.json` : Kuma ecrit `db-config.json` avec `{"type": "sqlite"}` (et `UPTIME_KUMA_DB_TYPE=sqlite` le garantit) ;
   - migrations de schema (anciens patchs SQL, puis migrations knex) ;
   - migration des tables d'agregats : l'historique des `heartbeat` est recalcule dans `stat_minutely/hourly/daily`, puis les
     heartbeats non importants de plus de 24 h sont supprimes (100 derniers conserves par moniteur). Un serveur temporaire
     sert une page de progression ("[DON'T STOP] Migrating monitor ...") sur le **meme port** ;
   - duree : proportionnelle au nombre de heartbeats historiques, non bornee par le code (a suivre avec
     `journalctl -u uptime-kuma -f`) ; **ne pas redemarrer le service pendant la migration**.
     Pendant ce temps le controle HTTP du role peut etre repondu par ce serveur temporaire : augmenter
     `uptime_kuma_healthcheck_retries` ou le desactiver pour une grosse base.
5. Migration interrompue (etat `migrating`) : le service refuse de demarrer ("Aggregate table migration is already in progress").
   Soit restaurer `kuma.db.pre-<version>` puis relancer, soit, service arrete :
   `cd /opt/uptime-kuma && sudo -u uptime-kuma env DATA_DIR=/var/lib/uptime-kuma/ npm run reset-migrate-aggregate-table-state`.

Le schema 2.x n'est pas reversible : revenir en 1.x = restaurer la base 1.x (`kuma.db.pre-<version>` ou ancien CT).

## Rollback

- **Mise a jour d'une 2.x vers une autre** : arreter le service, restaurer `kuma.db.pre-<version cible>`
  (cree juste avant la mise a jour), remettre l'ancienne `uptime_kuma_version`, relancer le role.
- **Retour a Kuma 1.x** : reactiver l'ancien conteneur/CT (restaurer `kuma.db.pre-<version>` si la base 1.x a ete modifiee) ;
  le role ne touche a rien d'autre.
- **Retrait complet** :
  ```bash
  systemctl disable --now uptime-kuma
  rm /etc/systemd/system/uptime-kuma.service /etc/nftables.d/uptime-kuma.nft
  systemctl daemon-reload && systemctl reload nftables
  rm -r /opt/uptime-kuma          # les donnees /var/lib/uptime-kuma sont conservees ; userdel uptime-kuma si besoin
  ```

## Tests

Aucun test automatise n'accompagne ce role. Verifications a faire avant mise en production :
`ansible-playbook --syntax-check`, `yamllint roles/uptime_kuma`, puis `--check --diff` et un premier passage
sur un hote jetable.
