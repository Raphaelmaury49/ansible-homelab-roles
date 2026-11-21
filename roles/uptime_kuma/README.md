# uptime_kuma

Uptime Kuma en installation native (sans Docker) : clone du tag git, `npm ci --omit dev`,
`npm run download-dist`, service systemd durci, port filtre par nftables.

**Version par defaut : 1.23.17** (derniere 1.x, tete de la branche `1.23.X`). Une 2.x n'est installee que si
`uptime_kuma_allow_v2: true` (voir « Future montee en 2.x »). Raison : le serveur MCP de supervision utilise
`uptime-kuma-api` 1.2.1, qui ne supporte que Kuma 1.21.3 a 1.23.2.

Le role ne contient aucune donnee privee. A jouer apres `base` (qui cree `/etc/nftables.d` et active nftables),
sur Debian 13 (relecture uniquement, voir la section Tests).

## Ce que fait le role

1. garde-fous : `uptime_kuma_http_sources` non vide (**obligatoire**), `uptime_kuma_version` = tag 1.x/2.x (2.x seulement avec `uptime_kuma_allow_v2`), port valide ;
2. paquets Debian : `git`, `nodejs`, `npm`, `iputils-ping` (+ `build-essential`, `python3` en repli pour compiler le module natif sqlite) ;
3. verifie `node >= 20.4.0` : les `engines` de Kuma 1.23.17 sont `14 || 16 || 18 || >= 20.4.0` (et `>= 20.4.0` en 2.5.x) ; le Node 20.19 de Debian 13 convient, donc pas de NodeSource ;
4. utilisateur/groupe systeme `uptime-kuma` (sans shell, home = repertoire de donnees) ;
5. `git clone` du tag dans `/opt/uptime-kuma` (root:root : le service ne peut pas modifier son propre code) ;
6. si necessaire (voir ci-dessous) : arret du service, copie de `kuma.db`, `npm ci --omit dev --no-audit`, `npm run download-dist`, controle de `dist/index.html`, ecriture du marqueur `/opt/uptime-kuma/.ansible-build`. C'est l'equivalent de `npm run setup` (`git checkout <tag> && npm ci --production && npm run download-dist`), decoupe pour etre idempotent ;
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
| `uptime_kuma_version` | `1.23.17` | tag git (sans `v`) |
| `uptime_kuma_allow_v2` | `false` | autorise un tag 2.x (garde-fou contre une montee involontaire) |
| `uptime_kuma_repo` | `https://github.com/louislam/uptime-kuma.git` | depot |
| `uptime_kuma_git_depth` | `1` | profondeur du clone (0 = complet) |
| `uptime_kuma_user` / `uptime_kuma_group` | `uptime-kuma` | compte systeme |
| `uptime_kuma_install_dir` | `/opt/uptime-kuma` | code |
| `uptime_kuma_data_dir` | `/var/lib/uptime-kuma` | `DATA_DIR` : `kuma.db`, `upload/` |
| `uptime_kuma_host` | `0.0.0.0` | `UPTIME_KUMA_HOST` |
| `uptime_kuma_port` | `3001` | `UPTIME_KUMA_PORT` (la page de statut est servie sur le meme port) |
| `uptime_kuma_node_bin` / `uptime_kuma_node_min` | `/usr/bin/node` / `20.4.0` | Node.js |
| `uptime_kuma_packages` / `uptime_kuma_build_packages` | voir `defaults/main.yml` | paquets apt |
| `uptime_kuma_extra_env` | `{}` | variables d'environnement supplementaires du service |
| `uptime_kuma_force_rebuild` | `false` | rejoue `npm ci` + `download-dist` |
| `uptime_kuma_backup_db_on_upgrade` | `true` | copie `kuma.db.pre-<version>` avant chaque build (jamais ecrasee) |
| `uptime_kuma_healthcheck` / `_retries` / `_delay` | `true` / `60` / `5` | controle HTTP apres demarrage |

Environnement pose dans l'unite : `NODE_ENV=production`, `DATA_DIR=<data_dir>/`, `UPTIME_KUMA_HOST`,
`UPTIME_KUMA_PORT` (+ `uptime_kuma_extra_env`). Avec une 2.x s'ajoute `UPTIME_KUMA_DB_TYPE=sqlite`
(evite l'assistant de choix de base ; ignore en 1.x, donc non pose).

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
uptime_kuma_version: "1.23.17"
uptime_kuma_port: 3001
uptime_kuma_http_sources:
  - 192.168.2.0/24        # NPM, Prometheus, Homepage, MCP : a restreindre aux IP utiles
```

```bash
ansible-playbook site.yml --limit kuma -e ansible_user=root   # bootstrap
ansible-playbook site.yml --limit kuma --check --diff         # plan
```

## Deposer un kuma.db 1.23 avant le premier demarrage

Le role n'a pas de variable de restauration : la copie de `kuma.db` est une operation a part, **avant le
premier demarrage** du service. Le format de base est celui de la 1.x : aucune migration de schema (les
patchs 1.x deja appliques sont reconnus), le service demarre sur les donnees copiees.

1. Sur l'ancien hote, arreter proprement Kuma 1.x (`docker stop`) pour que le journal WAL soit integre dans `kuma.db`.
   Garder l'ancien conteneur/CT intact : c'est le rollback.
2. Sur le nouvel hote, creer le repertoire de donnees (le role le cree aussi, mais le premier passage demarre le service :
   deposer les fichiers **avant** de jouer le role) et y copier `kuma.db` (+ `upload/` si des icones/logos sont utilises ;
   `kuma.db-wal` et `kuma.db-shm` seulement s'ils existent) :
   ```bash
   install -d -o root -g root -m 0750 /var/lib/uptime-kuma
   cp kuma.db /var/lib/uptime-kuma/kuma.db            # DATA_DIR = uptime_kuma_data_dir
   ```
   Les proprietaires sont corriges par le role (`uptime-kuma:uptime-kuma`, creation de l'utilisateur comprise). Si le
   compte existe deja, `chown -R uptime-kuma:uptime-kuma /var/lib/uptime-kuma` est equivalent.
3. Jouer le role. Il copie d'abord `kuma.db` en `kuma.db.pre-<version>` (base d'origine), puis demarre.
4. Verifier : `journalctl -u uptime-kuma`, connexion avec les identifiants de l'ancienne instance, moniteurs presents.
   Si le « Primary Base URL » (parametres) change de nom, le mettre a jour.

Sans `kuma.db`, Kuma cree une base vide et affiche l'ecran de creation du compte administrateur.

## Future montee en 2.x

A ne faire que lorsque le client MCP (`uptime-kuma-api`) aura ete adapte a l'API socket.io de la 2.x : sinon les outils MCP cassent.

1. **Sauvegarde** : arreter le service, copier `kuma.db` (+ `-wal`/`-shm` s'ils existent) hors de l'hote. Le role en fait aussi une copie
   `kuma.db.pre-<version cible>` avant le build, mais une sauvegarde externe reste necessaire : le schema 2.x n'est pas reversible.
2. Positionner `uptime_kuma_version: "2.x.y"` (tag verifie sur GitHub, Node >= 20.4 requis) **et** `uptime_kuma_allow_v2: true`, puis jouer le role.
3. **Premiere execution longue** : au premier demarrage 2.x, Kuma migre le schema (knex) puis recalcule les agregats
   (`stat_minutely/hourly/daily`) a partir de tout l'historique `heartbeat`, et purge les heartbeats non importants de plus de 24 h
   (100 derniers conserves par moniteur). Un serveur temporaire de progression ecoute sur le meme port (« [DON'T STOP] Migrating monitor ... »).
   Duree proportionnelle a la taille de l'historique, non bornee par le code : suivre `journalctl -u uptime-kuma -f`, ne **pas** redemarrer.
   Augmenter `uptime_kuma_healthcheck_retries` (ou desactiver le controle) pour une grosse base.
4. SQLite est conserve : un `kuma.db` existant sans `db-config.json` est declare `sqlite` par Kuma, et `UPTIME_KUMA_DB_TYPE=sqlite` est pose par le role.
   Le MariaDB embarque n'est propose que si `UPTIME_KUMA_ENABLE_EMBEDDED_MARIADB=1` (image Docker), absent ici.
5. Migration interrompue (etat `migrating`) : le service refuse de demarrer (« Aggregate table migration is already in progress »).
   Restaurer `kuma.db.pre-<version>` puis relancer, ou, service arrete :
   `cd /opt/uptime-kuma && sudo -u uptime-kuma env DATA_DIR=/var/lib/uptime-kuma/ npm run reset-migrate-aggregate-table-state`.
6. **MCP a adapter** : verifier le client avant la montee ; tester les outils MCP (liste/creation de moniteurs, statuts) sur une copie de la base.

## Rollback

- **Mise a jour d'une version vers une autre** : arreter le service, restaurer `kuma.db.pre-<version cible>` (cree juste avant la mise a jour),
  remettre l'ancienne `uptime_kuma_version`, relancer le role. Indispensable apres une 2.x (schema non reversible).
- **Retour a l'ancien Kuma** : reactiver l'ancien conteneur/CT ; le role ne touche a rien d'autre.
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
