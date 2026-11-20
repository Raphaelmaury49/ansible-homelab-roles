# ssh_keyserver

Serveur de cles SSH centralise. Un meme role, deux modes (`ssh_keyserver_mode`).

```
sshd (client) --AuthorizedKeysCommand--> ssh-keys-fetch --HTTPS--> NPM (keys.<domaine>) --HTTP:80--> nginx (server) --> /srv/ssh-keys/<user>.keys
```

Le role ne contient aucune donnee privee : les cles (publiques) sont fournies par l'inventaire
(`ssh_keyserver_keys`). Aucune cle privee, jamais (y compris la cle de signature).

## Hotes a EXCLURE du mode client

Dependance circulaire : ces hotes fournissent (ou resolvent) le service de cles. S'ils en dependaient
pour leur propre acces SSH, une panne les rendrait inaccessibles au moment ou on en a besoin.

| Hote | Raison |
|---|---|
| Technitium (LXC 201) | resout `keys.<domaine>` |
| DOCKERMASTER / NPM (.249) | termine TLS et relaie vers le backend |
| sshkeys (LXC 205) | sert les cles (mode `server`) |

Renseigner `ssh_keyserver_client_excluded_hosts` dans l'inventaire (noms d'hotes) : le role echoue
sur un hote de cette liste en mode `client`.

## Comptes : root et admin

Par defaut (`ssh_keyserver_users: [root, admin]`), le serveur sert la meme liste de cles pour `root`
et pour `admin` (fichiers `root.keys` et `admin.keys`, a alimenter dans `ssh_keyserver_keys`) :

- les hotes geres par le role `base` gardent `PermitRootLogin no` + `AllowUsers <admin>` et se connectent avec `admin` ;
- les hotes hors IaC (ex. Proxmox VE) se connectent avec `root`.

## Mode `server`

nginx sert en texte brut `/srv/ssh-keys/<user>.keys` sur le port 80 (TLS termine par le reverse proxy).
Seul `/<user>.keys` en GET/HEAD est servi (plus `/<user>.keys.sig` si la signature est activee),
tout le reste renvoie 404, pas de listing.

- ACL nginx : `allow <source>; ... deny all;` genere depuis `ssh_keyserver_http_sources` (IP du reverse proxy). La liste est **obligatoire** (le role echoue si elle est vide). Toute autre source recoit 403.
- Regle nftables port 80 depuis les memes sources (`/etc/nftables.d/ssh-keyserver.nft`), conservee en defense en profondeur.
- `access_log` dedie : `ssh_keyserver_access_log` (defaut `/var/log/nginx/ssh-keyserver.access.log`, rotation par le logrotate nginx de Debian).

| Variable | Defaut | Role |
|---|---|---|
| `ssh_keyserver_keys` | `{}` | dict `user -> [cles]` (typiquement `root` et `admin`) |
| `ssh_keyserver_root` | `/srv/ssh-keys` | dossier servi |
| `ssh_keyserver_http_sources` | `[]` (obligatoire) | IP/CIDR autorisees (nginx + nftables) |
| `ssh_keyserver_firewall` | `true` | regle nftables port 80 |
| `ssh_keyserver_access_log` | `/var/log/nginx/ssh-keyserver.access.log` | log d'acces dedie |
| `ssh_keyserver_nginx_disable_default` | `true` | retire le site nginx par defaut |
| `ssh_keyserver_signatures` / `ssh_keyserver_signatures_dir` | `{}` / `""` | signatures fournies (voir plus bas) |

Les fichiers de comptes retires de `ssh_keyserver_keys` ne sont pas supprimes du serveur (suppression manuelle).

## Mode `client`

- paquets `curl`, `ca-certificates`, `openssh-client` ;
- compte systeme `sshkeys` (sans shell), cache `/var/cache/ssh-keys` (`sshkeys:sshkeys` 0700) ;
- script `/usr/local/bin/ssh-keys-fetch` (`root:root` 0755, POSIX sh + curl) ;
- `/etc/ssh/sshd_config.d/50-keyserver.conf` : `AuthorizedKeysCommand` + `AuthorizedKeysCommandUser sshkeys` ;
- `Include /etc/ssh/sshd_config.d/*.conf` verifie dans `sshd_config` (ajoute en tete sinon) ;
- validation `sshd -t` avant rechargement (handler `Reload ssh (ssh_keyserver)` : validation puis reload, restart si `ssh.socket` actif) ;
- **garde-fou** : le deploiement echoue si le script rendu contient l'option d'insecurite TLS de curl (`-k`, y compris combinee) ou `--insecure`, ou s'il n'impose plus `--proto` / `--tlsv1.2` ;
- tache de test : script lance en tant que `sshkeys` pour le premier compte de `ssh_keyserver_users`, nombre de cles affiche.

| Variable | Defaut |
|---|---|
| `ssh_keyserver_url` | `https://keys.raphaelmaury.fr` (https obligatoire) |
| `ssh_keyserver_users` | `[root, admin]` |
| `ssh_keyserver_cmd_user` | `sshkeys` |
| `ssh_keyserver_cache_dir` | `/var/cache/ssh-keys` |
| `ssh_keyserver_cache_max_age_days` | `14` |
| `ssh_keyserver_max_keys` | `10` |
| `ssh_keyserver_allow_rsa` | `false` (si `true` : `ssh-rsa` >= 3072 bits seulement) |
| `ssh_keyserver_client_excluded_hosts` | `[]` |
| `ssh_keyserver_signing` | `false` |
| `ssh_keyserver_signer_pubkey` | `""` |
| `ssh_keyserver_allowed_signers` | `/etc/ssh/keyserver_allowed_signers` |
| `ssh_keyserver_test` | `true` |

### Comportement du script (`ssh-keys-fetch %u`)

1. `%u` doit correspondre a `^[a-z_][a-z0-9_-]*$` (teste par `case`) puis etre dans la liste blanche stricte `ssh_keyserver_users` (`case`) ; sinon sortie 0 sans rien afficher ;
2. `curl -sS -o "$tmp" -w '%{http_code}' --proto '=https' --tlsv1.2 --max-time 3 --max-filesize 16384` : pas de `-L` (aucune redirection suivie), verification TLS toujours active ;
3. le code HTTP decide (voir tableau) ; un echec de curl (reseau, timeout, TLS, fichier trop gros) vaut code `000` ;
4. validation ligne par ligne : seuls `ssh-ed25519`, `sk-ssh-ed25519@openssh.com`, `ecdsa-sha2-nistp256/384/521`, `sk-ecdsa-sha2-nistp256@openssh.com` sont acceptes (`ssh-rsa` seulement si `ssh_keyserver_allow_rsa`, >= 3072 bits) ; chaque ligne est controlee par `ssh-keygen -lf` sur un fichier temporaire (fonctionne sur toutes les versions d'OpenSSH : Debian 12/13, Proxmox 8/9) ; les lignes avec options (`command=`, ...) sont rejetees ; au plus `ssh_keyserver_max_keys` cles ;
5. cache : `mktemp` dans `/var/cache/ssh-keys` puis `mv` atomique, fichiers 0600 ;
6. journalisation `logger -t ssh-keys-fetch` : repli sur le cache, revocation (cache purge), erreur HTTP (avec le code), reponse rejetee ;
7. code de sortie toujours 0.

### Matrice des cas

Le `authorized_keys` local reste toujours l'acces de secours (`AuthorizedKeysFile` n'est pas modifie).

| Cas | Decision du script | Cache | Sortie | Log |
|---|---|---|---|---|
| HTTP 200, au moins une cle valide | cles filtrees/validees | mis a jour (mtime rafraichi) | les cles | aucun |
| HTTP 200, zero cle valide (vide, HTML, lignes invalides) | revocation explicite | **purge** | rien | revocation (cache purge) |
| HTTP 404 | revocation explicite | **purge** | rien | revocation (cache purge) |
| HTTP 403 (ACL, ban CrowdSec) | erreur, **jamais** une revocation | conserve | cache si age < 14 j, sinon rien | erreur HTTP 403 + repli |
| 5xx, timeout, reseau, TLS, reponse > 16 Kio (code 000) | erreur, repli | conserve | cache si age < 14 j, sinon rien | erreur HTTP 000/5xx + repli |
| Autre code (301, 401, 429...) | erreur, repli | conserve | cache si age < 14 j, sinon rien | erreur HTTP + repli |
| Cache expire (age >= 14 j) et serveur en erreur | rien servi | conserve (non servi) | rien | erreur HTTP + "cache plus vieux que 14j" |
| Signature invalide ou `.sig` absent (signature activee) | reponse rejetee | **non modifie** | cache si age < 14 j, sinon rien | reponse rejetee + repli |
| Serial inferieur au cache (signature activee) | reponse rejetee (rejeu) | non modifie | cache si age < 14 j, sinon rien | reponse rejetee + repli |
| `%u` invalide ou hors liste blanche | sortie immediate | inchange | rien | aucun |

Limite connue : avec la signature activee, un `404` n'est pas authentifie (pas de signature sur une erreur). Il purge le cache, ce qui ne peut que retirer des acces (le `authorized_keys` local reste valide), jamais en ajouter.

## Signature des listes (optionnelle, `ssh_keyserver_signing: false` par defaut)

Protege contre un serveur 205 ou un chemin reseau compromis : le client n'accepte que des listes
signees par une cle dont la **cle privee n'est jamais sur 205 ni dans Ansible** (poste de confiance,
idealement hors ligne ou sur une cle materielle).

Quand `ssh_keyserver_signing: true` :

- serveur : publie `<user>.keys` et `<user>.keys.sig`. Les `.sig` sont **fournies**, jamais generees : variable `ssh_keyserver_signatures` (`user -> contenu`) ou dossier du depot `ssh_keyserver_signatures_dir` (`<user>.keys.sig`) ;
- la 1re ligne de chaque liste est `# serial: <entier>` (verifie au deploiement) ;
- client : deploie `/etc/ssh/keyserver_allowed_signers` depuis `ssh_keyserver_signer_pubkey` (format `keyserver namespaces="sshkeys-v1" <cle publique>`), telecharge aussi le `.sig` et verifie :
  `ssh-keygen -Y verify -f /etc/ssh/keyserver_allowed_signers -I keyserver -n sshkeys-v1 -s sig < keys`
- signature invalide, `.sig` absent ou serial absent : reponse rejetee, repli sur le cache **sans le mettre a jour** ;
- anti-rejeu : un serial inferieur au plus haut serial connu (cache `<user>.keys` ou `<user>.serial`) est refuse. Le fichier `<user>.serial` survit a la purge du cache (une revocation ne doit pas permettre de rejouer une ancienne liste). Pour repartir d'un serial plus bas volontairement : supprimer `/var/cache/ssh-keys/<user>.serial` sur les clients.

### Procedure de signature (sur le poste de confiance)

```bash
# une fois : generer la cle de signature (la cle publique va dans ssh_keyserver_signer_pubkey)
ssh-keygen -t ed25519 -f ~/.ssh/keyserver_signer -C keyserver-signer

# root.keys : "# serial: N" puis une cle par ligne, fins de ligne LF, LF final
# (octets identiques a ce que publie le role : liste jointe par des LF + LF final)
ssh-keygen -Y sign -f ~/.ssh/keyserver_signer -n sshkeys-v1 root.keys   # produit root.keys.sig

# controle local (allowed_signers : keyserver namespaces="sshkeys-v1" <cle publique>)
ssh-keygen -Y verify -f allowed_signers -I keyserver -n sshkeys-v1 -s root.keys.sig < root.keys
```

La signature porte sur le contenu et le namespace, pas sur le nom du fichier : si `admin.keys` a
exactement le meme contenu que `root.keys`, `root.keys.sig` peut etre reutilisee pour `admin`.

### Flux de travail : ajouter une cle

1. Ajouter la cle publique dans la liste (`ssh_keyserver_keys`, inventaire prive `homelab-iac`) et **incrementer** `# serial: N`.
2. Regenerer `root.keys` (et `admin.keys`) avec exactement ces lignes, puis signer (`ssh-keygen -Y sign ...`).
3. Placer les `.sig` dans `ssh_keyserver_signatures` (ou dans `ssh_keyserver_signatures_dir`) et commiter dans `homelab-iac`.
4. Lancer Ansible sur l'hote `sshkeys` (mode `server`). Entre l'ecriture de `.keys` et celle de `.sig`, un client peut brievement rejeter la reponse et se replier sur son cache : sans consequence.
5. Verifier sur un client : `runuser -u sshkeys -- /usr/local/bin/ssh-keys-fetch root` et `journalctl -t ssh-keys-fetch`.

## Revocation d'urgence d'une cle

1. Retirer la cle de `ssh_keyserver_keys` dans `homelab-iac` (si signature activee : incrementer le serial, re-signer, mettre a jour les `.sig`) et commiter.
2. Lancer Ansible sur `sshkeys` pour republier les listes :
   ```bash
   ansible-playbook -i <inventaire> <playbook>.yml --limit sshkeys
   ```
3. Purger le cache des clients (sinon une cle revoquee reste utilisable en repli jusqu'a 14 jours si le serveur est injoignable ou refuse l'acces). Les `.serial` (anti-rejeu) sont conserves :
   ```bash
   ansible <groupe_clients> -i <inventaire> -b -m ansible.builtin.command -a "find /var/cache/ssh-keys -maxdepth 1 -type f -name '*.keys' -delete"
   ```
4. Retirer aussi la cle des `authorized_keys` locaux des hotes (acces de secours non gere par ce role). Les sessions deja ouvertes ne sont pas coupees.

## Garde-fous

- `AuthorizedKeysFile` n'est pas modifie : le `authorized_keys` local reste l'acces de secours (sshd consulte les deux sources).
- Proxmox VE : le role ne touche jamais a `/root/.ssh/authorized_keys` (lien symbolique vers `/etc/pve/priv/authorized_keys`).
- Le role `base` interdit root par defaut (`PermitRootLogin no`, `AllowUsers <admin>`) : sur un hote `base`, c'est le compte `admin` qui utilise les cles du serveur. Pour que `root` puisse se connecter avec ces cles, il faut `base_disable_root_login: false` ; ce role ne modifie pas ces reglages.
- `sshd` retient la premiere valeur rencontree : l'`Include` doit preceder toute autre directive `AuthorizedKeysCommand` du fichier principal.

## Exemple

```yaml
# serveur : meme liste pour root et admin (ancre YAML pour ne pas dupliquer)
ssh_keyserver_mode: server
ssh_keyserver_http_sources: ["192.168.2.x/32"]   # NPM (obligatoire)
ssh_keyserver_common_keys: &common_keys
  - ssh-ed25519 AAAA... you@laptop
ssh_keyserver_keys:
  root: *common_keys
  admin: *common_keys

# clients
ssh_keyserver_mode: client
ssh_keyserver_client_excluded_hosts: [technitium, dockermaster, sshkeys]
```

```yaml
iac_roles: [base, ssh_keyserver]
```

## Rollback (client)

```bash
rm /etc/ssh/sshd_config.d/50-keyserver.conf
sshd -t && systemctl reload ssh      # ou: systemctl restart ssh si ssh.socket est actif ; `sshd` sur RHEL
```

Facultatif : `rm /usr/local/bin/ssh-keys-fetch; rm -r /var/cache/ssh-keys; rm -f /etc/ssh/keyserver_allowed_signers; userdel sshkeys`.
L'`Include` ajoute en tete de `sshd_config` est inoffensif et peut rester.
Le `authorized_keys` local n'ayant pas ete modifie, l'acces de secours reste valide pendant tout le rollback.

## Rollback (server)

```bash
rm /etc/nginx/sites-enabled/ssh-keyserver /etc/nftables.d/ssh-keyserver.nft
systemctl reload nginx nftables
```
