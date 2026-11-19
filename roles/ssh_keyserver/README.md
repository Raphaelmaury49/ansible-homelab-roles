# ssh_keyserver

Serveur de cles SSH centralise. Un meme role, deux modes (`ssh_keyserver_mode`).

```
sshd (client) --AuthorizedKeysCommand--> ssh-keys-fetch --HTTPS--> NPM (keys.<domaine>) --HTTP:80--> nginx (server) --> /srv/ssh-keys/<user>.keys
```

Le role ne contient aucune donnee privee : les cles (publiques) sont fournies par l'inventaire
(`ssh_keyserver_keys`).

## Mode `server`

nginx sert en texte brut `/srv/ssh-keys/<user>.keys` sur le port 80 (TLS termine par le reverse proxy).
Seul `/<user>.keys` en GET/HEAD est servi, tout le reste renvoie 404, pas de listing.

| Variable | Defaut | Role |
|---|---|---|
| `ssh_keyserver_keys` | `{}` | dict `user -> [cles]` |
| `ssh_keyserver_root` | `/srv/ssh-keys` | dossier servi |
| `ssh_keyserver_nginx_disable_default` | `true` | retire le site nginx par defaut |
| `ssh_keyserver_firewall` / `ssh_keyserver_http_sources` | `true` / `[]` | si la liste n'est pas vide : regle nftables port 80 dans `/etc/nftables.d/ssh-keyserver.nft`. Avec `base` (default-drop), renseigner l'IP du reverse proxy |

Les fichiers de comptes retires de `ssh_keyserver_keys` ne sont pas supprimes du serveur (suppression manuelle).

## Mode `client`

- compte systeme `sshkeys` (sans shell), cache `/var/cache/ssh-keys` (proprietaire `sshkeys`, 0700) ;
- script `/usr/local/bin/ssh-keys-fetch` (root:root 0755, POSIX sh + curl) ;
- `/etc/ssh/sshd_config.d/50-keyserver.conf` : `AuthorizedKeysCommand` + `AuthorizedKeysCommandUser sshkeys` ;
- `Include /etc/ssh/sshd_config.d/*.conf` verifie dans `sshd_config` (ajoute en tete sinon) ;
- validation `sshd -t` avant rechargement (handler `Reload ssh (ssh_keyserver)` : validation puis reload, restart si `ssh.socket` actif) ;
- tache de test : script lance en tant que `sshkeys`, nombre de cles affiche.

Comportement du script (`ssh-keys-fetch %u`) :

1. user hors `ssh_keyserver_users` (defaut `[root]`) : sortie 0, aucune sortie ;
2. `curl -fsS --max-time 3 <url>/<user>.keys` (verification TLS) ;
3. ne garde que les lignes `ssh-ed25519`, `ssh-rsa`, `ecdsa-sha2-`, `sk-` ;
4. resultat non vide : cache ecrit de facon atomique (mktemp + mv) et cles affichees ; sinon cache affiche s'il existe ; sinon rien ;
5. code de sortie toujours 0.

| Variable | Defaut |
|---|---|
| `ssh_keyserver_url` | `https://keys.raphaelmaury.fr` |
| `ssh_keyserver_users` | `[root]` |
| `ssh_keyserver_cmd_user` | `sshkeys` |
| `ssh_keyserver_cache_dir` | `/var/cache/ssh-keys` |
| `ssh_keyserver_test` | `true` |

### Garde-fous

- `AuthorizedKeysFile` n'est pas modifie : le `authorized_keys` local reste l'acces de secours (sshd consulte les deux sources).
- Proxmox VE : le role ne touche jamais a `/root/.ssh/authorized_keys` (lien symbolique vers `/etc/pve/priv/authorized_keys`).
- Le role `base` interdit root par defaut (`PermitRootLogin no`, `AllowUsers <admin>`) : pour que root puisse se connecter avec ces cles, il faut `base_disable_root_login: false`. Ce role ne modifie pas ces reglages.
- `sshd` retient la premiere valeur rencontree : l'`Include` doit preceder toute autre directive `AuthorizedKeysCommand` du fichier principal.

## Exemple

```yaml
# serveur
ssh_keyserver_mode: server
ssh_keyserver_http_sources: ["192.168.2.x/32"]   # NPM
ssh_keyserver_keys:
  root:
    - ssh-ed25519 AAAA... you@laptop

# clients
ssh_keyserver_mode: client
```

```yaml
iac_roles: [base, ssh_keyserver]
```

## Rollback (client)

```bash
rm /etc/ssh/sshd_config.d/50-keyserver.conf
sshd -t && systemctl reload ssh      # ou: systemctl restart ssh si ssh.socket est actif ; `sshd` sur RHEL
```

Facultatif : `rm /usr/local/bin/ssh-keys-fetch; rm -r /var/cache/ssh-keys; userdel sshkeys`.
L'`Include` ajoute en tete de `sshd_config` est inoffensif et peut rester.
Le `authorized_keys` local n'ayant pas ete modifie, l'acces de secours reste valide pendant tout le rollback.

## Rollback (server)

```bash
rm /etc/nginx/sites-enabled/ssh-keyserver /etc/nftables.d/ssh-keyserver.nft
systemctl reload nginx nftables
```
