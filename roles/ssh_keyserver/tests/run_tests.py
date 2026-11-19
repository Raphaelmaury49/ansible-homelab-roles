#!/usr/bin/env python3
"""Harnais de test du script ssh-keys-fetch (role ssh_keyserver). 100 % local.

Usage : python3 -I run_tests.py ROLE_DIR WORK_DIR [--shells "sh;dash;busybox sh"] [--json out.json] [-k motif]

- rend le template avec les defaults du role (render.py) vers 127.0.0.1:<port>, cache dans WORK_DIR/cache ;
- genere certificat auto-signe (CA passee via CURL_CA_BUNDLE), cles de test, cle de signature ;
- lance server.py (HTTPS local) et rejoue la matrice des cas du README, plus des cas limites ;
- le script rendu fixe PATH=/usr/local/...: pour le test UNIQUEMENT, la ligne PATH est reecrite
  afin de placer un stub `logger` (et, variante busybox, les applets busybox) en tete.
Sortie : tableau, JSON optionnel, code retour 1 si un cas echoue.
"""
import argparse
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable


def sh(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


# ------------------------------------------------------------------ fixtures
def gen_fixtures(w):
    fx = os.path.join(w, "fixtures")
    shutil.rmtree(fx, ignore_errors=True)
    os.makedirs(fx, exist_ok=True)
    K = {}

    def keygen(name, *args):
        p = os.path.join(fx, name)
        sh(["ssh-keygen", "-q", "-N", "", "-C", name, "-f", p] + list(args))
        return open(p + ".pub").read().strip()

    for i in range(1, 13):
        K["ed%d" % i] = keygen("ed%d" % i, "-t", "ed25519")
    K["old"] = keygen("old", "-t", "ed25519")
    for b in (256, 384, 521):
        K["ecdsa%d" % b] = keygen("ecdsa%d" % b, "-t", "ecdsa", "-b", str(b))
    K["rsa2048"] = keygen("rsa2048", "-t", "rsa", "-b", "2048")
    K["rsa3072"] = keygen("rsa3072", "-t", "rsa", "-b", "3072")

    def s(b):
        return struct.pack(">I", len(b)) + b

    def sk_from(line, newtype, kind):
        parts = line.split()
        blob = base64.b64decode(parts[1])
        # decode blob
        fields, i = [], 0
        while i < len(blob):
            n = struct.unpack(">I", blob[i : i + 4])[0]
            fields.append(blob[i + 4 : i + 4 + n])
            i += 4 + n
        if kind == "ed":
            nb = s(newtype.encode()) + s(fields[1]) + s(b"ssh:")
        else:
            nb = s(newtype.encode()) + s(fields[1]) + s(fields[2]) + s(b"ssh:")
        return "%s %s sk-%s" % (newtype, base64.b64encode(nb).decode(), kind)

    K["sk_ed"] = sk_from(K["ed1"], "sk-ssh-ed25519@openssh.com", "ed")
    K["sk_ecdsa"] = sk_from(K["ecdsa256"], "sk-ecdsa-sha2-nistp256@openssh.com", "ecdsa")
    K["opt_from"] = 'from="10.0.0.1" ' + K["ed1"]
    K["opt_cmd"] = 'command="/bin/true",no-pty ' + K["ed1"]
    K["corrupt"] = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIbogus-notbase64!! x"
    K["corrupt2"] = K["ed1"].split()[0] + " " + K["ed1"].split()[1][:-12] + " zz"

    # certificat auto-signe (IP SAN) : sert aussi de CA
    cert, key = os.path.join(fx, "cert.pem"), os.path.join(fx, "key.pem")
    sh(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
        "-days", "2", "-subj", "/CN=127.0.0.1", "-keyout", key, "-out", cert,
        "-addext", "subjectAltName=IP:127.0.0.1", "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,digitalSignature"])
    # mauvaise CA
    sh(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
        "-days", "2", "-subj", "/CN=autre", "-keyout", os.path.join(fx, "other-key.pem"),
        "-out", os.path.join(fx, "other-ca.pem")])

    # cles de signature (signataire attendu + un autre)
    signer = os.path.join(fx, "signer")
    pub = keygen("signer", "-t", "ed25519")
    keygen("badsigner", "-t", "ed25519")
    with open(os.path.join(fx, "allowed_signers"), "w") as f:
        f.write('keyserver namespaces="sshkeys-v1" %s\n' % pub)
    return K, fx


def signed(fx, name, content, key="signer", ns="sshkeys-v1"):
    p = os.path.join(fx, name + ".keys")
    with open(p, "w") as f:
        f.write(content)
    if os.path.exists(p + ".sig"):
        os.remove(p + ".sig")
    sh(["ssh-keygen", "-Y", "sign", "-q", "-f", os.path.join(fx, key), "-n", ns, p])
    return p, p + ".sig"


# ------------------------------------------------------------------ harness
class Env:
    pass


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def render(e, name, **over):
    out = os.path.join(e.w, "render", name)
    args = [PYTHON, "-I", os.path.join(HERE, "render.py"), e.role, out,
            "--set", "ssh_keyserver_url=https://127.0.0.1:%d" % e.port,
            "--set", "ssh_keyserver_cache_dir=%s" % e.cache,
            "--set", "ssh_keyserver_allowed_signers=%s" % os.path.join(e.fx, "allowed_signers")]
    for k, v in over.items():
        args += ["--set", "ssh_keyserver_%s=%s" % (k, v)]
    sh(args)
    return out


def patch_path(src, dst, prefix):
    out = []
    stub = os.path.join(os.path.dirname(prefix), "bin", "logger")
    for l in open(src).read().split("\n"):
        # test uniquement : appel du stub par chemin absolu (l'applet logger de busybox passerait avant le PATH)
        l = l.replace("logger -t", stub + " -t")
        if l.startswith("PATH=/usr/local/sbin"):
            l = "PATH=%s:%s" % (prefix, l[5:])
        out.append(l)
    with open(dst, "w") as f:
        f.write("\n".join(out))
    os.chmod(dst, 0o755)


def ok_body(lines):
    return {"status": 200, "body": "".join(l + "\n" for l in lines)}


def st(code, body="", **kw):
    d = {"status": code, "body": body}
    d.update(kw)
    return d


def build_cases(K, fx):
    C = []
    old = [K["old"]]
    k2 = [K["ed1"], K["ecdsa256"]]

    def case(id_, desc, expect, **kw):
        d = dict(id=id_, desc=desc, expect=expect, user="root", script="nosig", cache=None,
                 serial_file=None, keys=st(404), sig=st(404), ca="good", out=[], cache_after="absent",
                 serial_after=None, log=None, reqs=None, max_s=None, min_s=None, cache_dir=True, matrix=None)
        d.update(kw)
        C.append(d)

    UP = "updated"
    # ---- ligne 1 : 200 + cles valides
    case("m1-200-valid", "200, 2 cles valides, pas de cache", "cles servies, cache cree 0600, aucun log",
         matrix="HTTP 200, au moins une cle valide", keys=ok_body(k2), out=k2, cache_after=("lines", k2), log=[])
    case("m1-200-refresh", "200 valide, cache de 5 j", "cache remplace, mtime rafraichi",
         matrix="HTTP 200, au moins une cle valide (mtime rafraichi)", cache=dict(lines=old, age=5), keys=ok_body(k2),
         out=k2, cache_after=("lines", k2), log=[])
    case("m1-crlf-nonl", "200 avec CRLF, commentaires, lignes vides, sans LF final", "seules les cles, sans CR",
         keys=st(200, "# commentaire\r\n\r\n%s\r\n%s" % (k2[0], k2[1])), out=k2, cache_after=("lines", k2), log=[])
    case("m1-sk-keys", "200 avec cles sk-ssh-ed25519 et sk-ecdsa", "acceptees",
         keys=ok_body([K["sk_ed"], K["sk_ecdsa"]]), out=[K["sk_ed"], K["sk_ecdsa"]],
         cache_after=("lines", [K["sk_ed"], K["sk_ecdsa"]]), log=[])
    case("m1-ecdsa-all", "200 ecdsa 256/384/521", "acceptees",
         keys=ok_body([K["ecdsa256"], K["ecdsa384"], K["ecdsa521"]]),
         out=[K["ecdsa256"], K["ecdsa384"], K["ecdsa521"]],
         cache_after=("lines", [K["ecdsa256"], K["ecdsa384"], K["ecdsa521"]]), log=[])
    case("m1-admin", "utilisateur admin, 200", "cles servies", user="admin", keys=ok_body(k2), out=k2,
         cache_after=("lines", k2), log=[])
    # ---- ligne 2 : 200 sans cle valide
    rev = ["revocation"]
    case("m2-200-empty", "200 corps vide, cache 1 j", "revocation : cache purge, rien affiche, log revocation",
         matrix="HTTP 200, zero cle valide (vide)", cache=dict(lines=old, age=1), keys=st(200, ""), out=[],
         cache_after="absent", log=rev)
    case("m2-200-html", "200 page HTML", "revocation", matrix="HTTP 200, zero cle valide (HTML)",
         cache=dict(lines=old, age=1),
         keys=st(200, "<html><body><h1>Welcome</h1>\nssh-ed25519 pas une cle</body></html>\n"), cache_after="absent", log=rev)
    case("m2-200-invalid-lines", "200 uniquement lignes invalides (options, corrompue, rsa)", "revocation",
         matrix="HTTP 200, zero cle valide (lignes invalides)", cache=dict(lines=old, age=1),
         keys=ok_body([K["opt_from"], K["opt_cmd"], K["corrupt"], K["corrupt2"], K["rsa2048"], K["rsa3072"]]),
         cache_after="absent", log=rev)
    # ---- ligne 3 : 404
    case("m3-404", "404, cache 1 j", "revocation : cache purge", matrix="HTTP 404", cache=dict(lines=old, age=1),
         keys=st(404, "not found"), cache_after="absent", log=rev)
    case("m3-404-nocache", "404, pas de cache", "rien, log revocation", keys=st(404), cache_after="absent", log=rev)
    # ---- ligne 4 : 403
    case("m4-403-cache", "403, cache 1 j", "cache servi, conserve, log erreur HTTP 403 + repli",
         matrix="HTTP 403", cache=dict(lines=old, age=1), keys=st(403, "forbidden"), out=old, cache_after="same",
         log=["erreur HTTP 403", "repli sur le cache"])
    case("m4-403-nocache", "403, pas de cache", "rien servi", keys=st(403), cache_after="absent",
         log=["erreur HTTP 403", "aucune cle servie"])
    # ---- ligne 5 : 5xx / timeout / reseau / TLS / >16 Kio
    for c in (500, 502, 503):
        case("m5-%d" % c, "%d, cache 1 j" % c, "repli cache", matrix="5xx" if c == 500 else None,
             cache=dict(lines=old, age=1), keys=st(c, "err"), out=old, cache_after="same",
             log=["erreur HTTP %d" % c, "repli sur le cache"])
    case("m5-timeout", "reponse apres 6 s (max-time 3)", "code 000 : repli cache, ~3 s",
         matrix="timeout", cache=dict(lines=old, age=1), keys=st(200, "x", sleep=6), out=old, cache_after="same",
         log=["erreur HTTP 000", "repli sur le cache"], min_s=2.5, max_s=5.5)
    case("m5-big-cl", "200 > 16 Kio (Content-Length 40000)", "code 000 : repli cache",
         matrix="reponse > 16 Kio", cache=dict(lines=old, age=1), keys=st(200, size=40000), out=old,
         cache_after="same", log=["erreur HTTP 000", "repli sur le cache"])
    case("m5-big-chunked", "200 > 16 Kio en chunked", "code 000 : repli cache",
         cache=dict(lines=old, age=1), keys=st(200, size=40000, chunked=True), out=old, cache_after="same",
         log=["erreur HTTP 000", "repli sur le cache"])
    case("m5-big-valid-keys", "200 > 16 Kio dont des cles valides en tete", "rejet complet (000), cle absente",
         cache=dict(lines=old, age=1), keys=st(200, "".join(l + "\n" for l in k2) + "a" * 20000), out=old,
         cache_after="same", log=["erreur HTTP 000"])
    case("m5-exact-16k", "200 de 16384 octets pile (cles valides + commentaires)", "accepte (limite incluse)",
         keys=st(200, "".join(l + "\n" for l in k2) + "#" * (16384 - len("".join(l + "\n" for l in k2)) - 1) + "\n"),
         out=k2, cache_after=("lines", k2), log=[])
    case("m5-close", "connexion fermee sans reponse", "code 000 : repli cache",
         cache=dict(lines=old, age=1), keys=st(200, close=True), out=old, cache_after="same",
         log=["erreur HTTP 000", "repli sur le cache"])
    case("m5-tls-badca", "CA non reconnue (TLS)", "code 000 : repli cache, jamais d'insecurite",
         matrix="TLS", cache=dict(lines=old, age=1), keys=ok_body(k2), ca="bad", out=old, cache_after="same",
         log=["erreur HTTP 000", "repli sur le cache"], reqs=[])
    # ---- ligne 6 : autres codes
    for c in (301, 401, 429):
        hdr = {"Location": "/redirige.keys"} if c == 301 else {}
        case("m6-%d" % c, "%d, cache 1 j" % c, "repli cache, redirection NON suivie",
             matrix="Autre code (301, 401, 429)" if c == 301 else None, cache=dict(lines=old, age=1),
             keys=st(c, "x", headers=hdr), out=old, cache_after="same",
             log=["erreur HTTP %d" % c, "repli sur le cache"], reqs=["GET /root.keys"])
    # ---- ligne 7 : cache expire
    case("m7-expired-500", "500, cache de 15 j", "rien servi, cache conserve, log 'plus vieux que 14j'",
         matrix="Cache expire (age >= 14 j) et serveur en erreur", cache=dict(lines=old, age=15), keys=st(500),
         out=[], cache_after="same", log=["erreur HTTP 500", "plus vieux que 14j"])
    case("m7-14d-exact", "500, cache de 14 j", "expire (age >= 14 j)", cache=dict(lines=old, age=14 + 3 / 86400), keys=st(500),
         out=[], cache_after="same", log=["plus vieux que 14j"])
    case("m7-13d23h", "500, cache de 13 j 23 h", "encore utilisable", cache=dict(lines=old, age=13.96), keys=st(500),
         out=old, cache_after="same", log=["repli sur le cache"])
    case("m7-expired-200", "200 valide, cache de 15 j", "cache rafraichi", cache=dict(lines=old, age=15),
         keys=ok_body(k2), out=k2, cache_after=("lines", k2), log=[])
    case("m7-empty-cache-file", "500, fichier cache vide", "rien servi", cache=dict(lines=[], age=1), keys=st(500),
         out=[], cache_after="same", log=["aucune cle servie"])
    case("m7-cache-comment-only", "500, cache avec commentaires + cle", "commentaires filtres",
         cache=dict(raw="# commentaire\n%s\n# autre\n" % K["old"], age=1), keys=st(500), out=old,
         cache_after="same", log=["repli sur le cache"])
    # ---- cles : filtres
    case("k-rsa2048-default", "rsa2048 + ed25519, allow_rsa=false", "rsa rejetee",
         keys=ok_body([K["rsa2048"], K["ed1"]]), out=[K["ed1"]], cache_after=("lines", [K["ed1"]]), log=[])
    case("k-rsa3072-default", "rsa3072 + ed25519, allow_rsa=false", "rsa rejetee (refusee par defaut)",
         keys=ok_body([K["rsa3072"], K["ed1"]]), out=[K["ed1"]], cache_after=("lines", [K["ed1"]]), log=[])
    case("k-rsa2048-allowed", "rsa2048, allow_rsa=true", "rsa < 3072 rejetee", script="rsa",
         keys=ok_body([K["rsa2048"], K["ed1"]]), out=[K["ed1"]], cache_after=("lines", [K["ed1"]]), log=[])
    case("k-rsa3072-allowed", "rsa3072, allow_rsa=true", "rsa >= 3072 acceptee", script="rsa",
         keys=ok_body([K["rsa3072"], K["ed1"]]), out=[K["rsa3072"], K["ed1"]],
         cache_after=("lines", [K["rsa3072"], K["ed1"]]), log=[])
    case("k-options", "lignes avec options from=/command=", "rejetees",
         keys=ok_body([K["opt_from"], K["opt_cmd"], K["ed1"]]), out=[K["ed1"]], cache_after=("lines", [K["ed1"]]), log=[])
    case("k-corrupt", "cle a base64 invalide / tronquee", "rejetees", keys=ok_body([K["corrupt"], K["corrupt2"], K["ed1"]]),
         out=[K["ed1"]], cache_after=("lines", [K["ed1"]]), log=[])
    case("k-leading-space", "ligne avec espace/tab en tete", "rejetee",
         keys=ok_body([" " + K["ed2"], "\t" + K["ed2"], K["ed1"]]), out=[K["ed1"]],
         cache_after=("lines", [K["ed1"]]), log=[])
    e12 = [K["ed%d" % i] for i in range(1, 13)]
    case("k-12-keys", "12 cles valides, max 10", "10 premieres cles", keys=ok_body(e12), out=e12[:10],
         cache_after=("lines", e12[:10]), log=[])
    case("k-invalid-not-counted", "5 lignes invalides puis 11 valides", "invalides non comptees : 10 cles",
         keys=ok_body([K["opt_from"]] * 5 + e12[:11]), out=e12[:10], cache_after=("lines", e12[:10]), log=[])
    case("k-dup-glob", "cle contenant * ou $(...) dans le commentaire", "affichee telle quelle, sans execution",
         keys=ok_body([K["ed1"] + " $(touch /nonexistent-pwned) * `id`"]), out=[K["ed1"] + " $(touch /nonexistent-pwned) * `id`"],
         cache_after=("lines", [K["ed1"] + " $(touch /nonexistent-pwned) * `id`"]), log=[])
    case("k-backslash", "cle avec backslash dans le commentaire", "preservee (read -r / printf %s)",
         keys=ok_body([K["ed1"] + " a\\nb\\tc"]), out=[K["ed1"] + " a\\nb\\tc"],
         cache_after=("lines", [K["ed1"] + " a\\nb\\tc"]), log=[])
    case("k-cachedir-missing", "repertoire de cache absent, 200 valide", "cles servies quand meme, sans crash",
         keys=ok_body(k2), out=k2, cache_after="nodir", cache_dir=False, log=[])
    case("k-cachedir-missing-500", "repertoire de cache absent, 500", "rien servi", keys=st(500), out=[],
         cache_after="nodir", cache_dir=False, log=["aucune cle servie"])
    # ---- signature
    sp5, ss5 = signed(fx, "s5", "# serial: 5\n%s\n%s\n" % tuple(k2))
    sp10, ss10 = signed(fx, "s10", "# serial: 10\n%s\n%s\n" % tuple(k2))
    sp3, ss3 = signed(fx, "s3", "# serial: 3\n%s\n" % K["ed2"])
    spns, ssns = signed(fx, "s_noserial", "%s\n%s\n" % tuple(k2))
    spab, ssab = signed(fx, "s_abc", "# serial: abc\n%s\n" % K["ed1"])
    sp13, ss13 = signed(fx, "s_13", "# serial: 1234567890123\n%s\n" % K["ed1"])
    spnf, ssnf = signed(fx, "s_notfirst", "# note\n# serial: 99\n%s\n" % K["ed1"])
    spbad, ssbad = signed(fx, "s_badkey", "# serial: 50\n%s\n" % K["ed1"], key="badsigner")
    spns2, ssns2 = signed(fx, "s_wrongns", "# serial: 50\n%s\n" % K["ed1"], ns="autre-ns")
    spe, sse = signed(fx, "s_nokeys", "# serial: 20\n%s\n" % K["opt_from"])
    spcr, sscr = signed(fx, "s_crlf", "# serial: 30\r\n%s\r\n" % K["ed1"])
    sp0, ss0 = signed(fx, "s0", "# serial: 0\n%s\n" % K["ed1"])
    fok = lambda p: {"status": 200, "body_file": p}
    ced = lambda n, ls: "# serial: %d\n" % n + "".join(l + "\n" for l in ls)
    S = dict(script="sig")
    case("s1-valid", "signature valide, serial 5, pas de cache", "cles servies sans ligne serial, cache + .serial",
         matrix="signature valide", keys=fok(sp5), sig=fok(ss5), out=k2,
         cache_after=("raw", ced(5, k2)), serial_after=5, log=[], **S)
    case("s1-valid-higher", "signature valide, serial 10 > cache 5", "mise a jour", cache=dict(raw=ced(5, old), age=1),
         serial_file=5, keys=fok(sp10), sig=fok(ss10), out=k2, cache_after=("raw", ced(10, k2)), serial_after=10,
         log=[], **S)
    case("s1-valid-equal", "serial 5 == cache 5", "accepte", cache=dict(raw=ced(5, old), age=1), serial_file=5,
         keys=fok(sp5), sig=fok(ss5), out=k2, cache_after=("raw", ced(5, k2)), serial_after=5, log=[], **S)
    case("s1-serial0", "serial 0 sans cache", "accepte", keys=fok(sp0), sig=fok(ss0), out=[K["ed1"]],
         cache_after=("raw", ced(0, [K["ed1"]])), serial_after=0, log=[], **S)
    case("s1-crlf", "corps signe en CRLF, serial en CRLF", "serial accepte (CR retire), cle servie",
         keys=fok(spcr), sig=fok(sscr), out=[K["ed1"]], cache_after=("raw", ced(30, [K["ed1"]])), serial_after=30,
         log=[], **S)
    rej = ["reponse rejetee"]
    case("s2-badsig-key", "signature par une autre cle", "rejet, cache conserve, repli",
         matrix="Signature invalide", cache=dict(raw=ced(5, old), age=1), serial_file=5, keys=fok(spbad), sig=fok(ssbad),
         out=old, cache_after="same", serial_after="same", log=["signature invalide", "repli sur le cache"], **S)
    case("s2-badsig-ns", "signature avec mauvais namespace", "rejet", cache=dict(raw=ced(5, old), age=1),
         keys=fok(spns2), sig=fok(ssns2), out=old, cache_after="same", log=["signature invalide"], **S)
    case("s2-tampered", "corps modifie apres signature", "rejet", cache=dict(raw=ced(5, old), age=1), serial_file=5,
         keys=st(200, open(sp10).read().replace(K["ed1"], K["ed3"])), sig=fok(ss10), out=old, cache_after="same",
         serial_after="same", log=["signature invalide"], **S)
    case("s2-sig-404", ".sig absent (404)", "rejet, repli", matrix=".sig absent", cache=dict(raw=ced(5, old), age=1),
         keys=fok(sp10), sig=st(404), out=old, cache_after="same", log=["erreur HTTP 404 sur root.keys.sig", "repli"], **S)
    case("s2-sig-500", ".sig en 500", "rejet, repli", cache=dict(raw=ced(5, old), age=1), keys=fok(sp10), sig=st(500),
         out=old, cache_after="same", log=["erreur HTTP 500 sur root.keys.sig"], **S)
    case("s2-sig-html", ".sig = 200 HTML", "rejet", cache=dict(raw=ced(5, old), age=1), keys=fok(sp10),
         sig=st(200, "<html>x</html>"), out=old, cache_after="same", log=["signature invalide"], **S)
    case("s2-badsig-nocache", "signature invalide, pas de cache", "rien servi, rien cree", keys=fok(spbad), sig=fok(ssbad),
         out=[], cache_after="absent", serial_after=None, log=["signature invalide", "aucune cle servie"], **S)
    case("s2-badsig-expired", "signature invalide, cache 15 j", "rien servi, cache conserve",
         cache=dict(raw=ced(5, old), age=15), keys=fok(spbad), sig=fok(ssbad), out=[], cache_after="same",
         log=["plus vieux que 14j"], **S)
    case("s2-no-serial", "signature valide mais pas de serial", "rejet", cache=dict(raw=ced(5, old), age=1),
         keys=fok(spns), sig=fok(ssns), out=old, cache_after="same", log=["serial absent"], **S)
    case("s2-serial-abc", "serial non numerique", "rejet", cache=dict(raw=ced(5, old), age=1), keys=fok(spab), sig=fok(ssab),
         out=old, cache_after="same", log=["serial absent ou invalide"], **S)
    case("s2-serial-13d", "serial a 13 chiffres", "rejet", cache=dict(raw=ced(5, old), age=1), keys=fok(sp13), sig=fok(ss13),
         out=old, cache_after="same", log=["serial absent ou invalide"], **S)
    case("s2-serial-notfirst", "serial pas en 1re ligne", "rejet", cache=dict(raw=ced(5, old), age=1), keys=fok(spnf),
         sig=fok(ssnf), out=old, cache_after="same", log=["serial absent ou invalide"], **S)
    ced10 = ced(10, old)
    case("s3-replay-cache", "serial 5 < cache 10 (rejeu)", "rejet, repli sans MAJ", matrix="Serial inferieur au cache",
         cache=dict(raw=ced10, age=1), serial_file=10, keys=fok(sp5), sig=fok(ss5), out=old, cache_after="same",
         serial_after="same", log=["serial 5 inferieur a 10", "repli sur le cache"], **S)
    case("s3-replay-serialfile", "cache purge, .serial=10, offert 5", "rejet : rien servi, .serial conserve",
         serial_file=10, keys=fok(sp5), sig=fok(ss5), out=[], cache_after="absent", serial_after="same",
         log=["serial 5 inferieur a 10"], **S)
    case("s3-replay-cache-only", "cache serial 10 sans .serial, offert 5", "rejet", cache=dict(raw=ced10, age=1),
         keys=fok(sp5), sig=fok(ss5), out=old, cache_after="same", serial_after=None, log=["serial 5 inferieur a 10"], **S)
    case("s3-serialfile-higher-than-cache", ".serial 20, cache 5, offert 10", "rejet (plus haut des deux)",
         cache=dict(raw=ced(5, old), age=1), serial_file=20, keys=fok(sp10), sig=fok(ss10), out=old, cache_after="same",
         serial_after="same", log=["serial 10 inferieur a 20"], **S)
    case("s3-serial-cache-corrupt", "cache sans ligne serial valide, offert 5", "pas de plancher : accepte",
         cache=dict(raw="# serial: zzz\n%s\n" % K["old"], age=1), keys=fok(sp5), sig=fok(ss5), out=k2,
         cache_after=("raw", ced(5, k2)), serial_after=5, log=[], **S)
    case("s4-404-signing", "404 en mode signature, cache + .serial", "purge cache, .serial conserve",
         matrix="Limite connue : 404 non authentifie", cache=dict(raw=ced(5, old), age=1), serial_file=5, keys=st(404),
         cache_after="absent", serial_after="same", log=rev, **S)
    case("s4-403-signing", "403 en mode signature", "repli cache", cache=dict(raw=ced(5, old), age=1), serial_file=5,
         keys=st(403), out=old, cache_after="same", serial_after="same", log=["erreur HTTP 403", "repli"], **S)
    case("s4-empty-signed-nokeys", "signe valide, serial 20, aucune cle valide", "revocation, .serial mis a 20",
         cache=dict(raw=ced(5, old), age=1), serial_file=5, keys=fok(spe), sig=fok(sse), out=[], cache_after="absent",
         serial_after=20, log=rev, **S)
    case("s4-nosig-fetch-not-done", "mode sans signature : .sig jamais demande", "aucune requete .sig",
         keys=ok_body(k2), out=k2, cache_after=("lines", k2), log=[], reqs=["GET /root.keys"])
    # ---- utilisateurs
    for u, label in (("nobody", "nobody"), ("../etc", "../etc"), ("", "chaine vide"), ("Root", "Root (majuscule)"),
                     ("root ", "'root ' (espace)"), ("-root", "-root"), ("1root", "1root"), ("ro*", "glob ro*"),
                     ("root|admin", "root|admin"), ("root/../admin", "root/../admin"), ("root\nadmin", "root<LF>admin"),
                     ("$(id)", "$(id)"), ("a_b-c", "a_b-c valide mais hors liste"), ("rooty", "rooty"), ("roo", "roo"),
                     ("sshkeys", "sshkeys")):
        case("u-" + (label.replace(" ", "_").replace("/", "_")), "utilisateur %s" % label,
             "sortie immediate, aucun acces reseau, cache inchange, rien affiche", matrix="%u invalide ou hors liste blanche" if u == "nobody" else None,
             user=u, cache=dict(lines=old, age=1), keys=ok_body(k2), out=[], cache_after="same", log=[], reqs=[])
    case("u-noarg", "aucun argument", "sortie immediate", user=None, cache=dict(lines=old, age=1), keys=ok_body(k2), out=[],
         cache_after="same", log=[], reqs=[])
    case("u-root-ok", "utilisateur root (liste blanche)", "traite normalement", keys=ok_body(k2), out=k2,
         cache_after=("lines", k2), log=[], reqs=["GET /root.keys"])
    return C


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("role_dir")
    ap.add_argument("work")
    ap.add_argument("--shells", default="sh;dash;busybox sh")
    ap.add_argument("--busybox", default=shutil.which("busybox") or "")
    ap.add_argument("--json")
    ap.add_argument("-k", default="")
    a = ap.parse_args()

    e = Env()
    e.role = os.path.abspath(a.role_dir)
    e.w = os.path.abspath(a.work)
    for d in ("render", "bin", "bbbin", "state"):
        shutil.rmtree(os.path.join(e.w, d), ignore_errors=True)
        os.makedirs(os.path.join(e.w, d))
    e.cache = os.path.join(e.w, "cache")
    e.state = os.path.join(e.w, "state")
    e.port = free_port()
    K, e.fx = gen_fixtures(e.w)
    cases = build_cases(K, e.fx)
    if a.k:
        cases = [c for c in cases if a.k in c["id"]]

    # stub logger (dans WORK/bin)
    stub = os.path.join(e.w, "bin", "logger")
    with open(stub, "w") as f:
        f.write('#!/bin/sh\nprintf "%s\\n" "$*" >> "$SSHKS_LOGFILE"\nexit 0\n')
    os.chmod(stub, 0o755)
    bbdir = os.path.join(e.w, "bbbin")
    if a.busybox:
        for ap_ in ("find", "mktemp", "head", "tr", "sed", "grep", "cat", "rm", "mv", "chmod", "printf"):
            os.symlink(a.busybox, os.path.join(bbdir, ap_))
        os.symlink(stub, os.path.join(bbdir, "logger"))

    scripts = {}
    for name, over in (("nosig", {}), ("sig", {"signing": "true"}), ("rsa", {"allow_rsa": "true"})):
        raw = render(e, name, **over)
        scripts[name] = os.path.join(e.w, "render", name + ".test")
        patch_path(raw, scripts[name], os.path.join(e.w, "bin"))
        if a.busybox:
            scripts[name + "@bb"] = os.path.join(e.w, "render", name + ".bbtest")
            patch_path(raw, scripts[name + "@bb"], bbdir)

    srv = subprocess.Popen([PYTHON, "-I", os.path.join(HERE, "server.py"), str(e.port),
                            os.path.join(e.fx, "cert.pem"), os.path.join(e.fx, "key.pem"), e.state],
                           stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", e.port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)

    logfile = os.path.join(e.w, "logger.log")
    results = []
    shells = [s.strip() for s in a.shells.split(";") if s.strip()]
    try:
        for shell in shells:
            argv0 = shell.split()
            if argv0[0] == "busybox":
                if not a.busybox:
                    print("SKIP %s (absent)" % shell)
                    continue
                argv0 = [a.busybox, "sh"]
            elif shutil.which(argv0[0]) is None:
                print("SKIP %s (absent)" % shell)
                continue
            variants = [("", shell)]
            if argv0[-1] == "sh" and argv0[0] == a.busybox:
                variants.append(("@bb", shell + " + applets busybox"))
            for suffix, label in variants:
                for c in cases:
                    results.append(run_case(e, c, argv0, suffix, label, scripts, logfile, K))
    finally:
        srv.terminate()
        srv.wait()

    bad = [r for r in results if not r["ok"]]
    for r in results:
        print("%s [%s] %-32s %s" % ("PASS" if r["ok"] else "FAIL", r["shell"], r["id"], "" if r["ok"] else "; ".join(r["problems"])))
    print("\n%d/%d OK" % (len(results) - len(bad), len(results)))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(results, f, indent=1, ensure_ascii=False)
    return 1 if bad else 0


def run_case(e, c, argv0, suffix, label, scripts, logfile, K):
    cache = e.cache
    shutil.rmtree(cache, ignore_errors=True)
    if c["cache_dir"]:
        os.makedirs(cache, mode=0o700)
        os.chmod(cache, 0o700)
    user = c["user"]
    ufile = "root" if user is None or user not in ("root", "admin") else user
    kf, sf = os.path.join(cache, ufile + ".keys"), os.path.join(cache, ufile + ".serial")
    before = None
    if c["cache"] and c["cache_dir"]:
        raw = c["cache"].get("raw")
        if raw is None:
            raw = "".join(l + "\n" for l in c["cache"]["lines"])
        with open(kf, "w") as f:
            f.write(raw)
        t = time.time() - c["cache"]["age"] * 86400
        os.utime(kf, (t, t))
        before = (raw, os.stat(kf).st_mtime_ns)
    sbefore = None
    if c["serial_file"] is not None and c["cache_dir"]:
        with open(sf, "w") as f:
            f.write("# serial: %d\n" % c["serial_file"])
        sbefore = open(sf).read()
    with open(os.path.join(e.state, "scenario.json"), "w") as f:
        json.dump({"keys": c["keys"], "sig": c["sig"]}, f)
    open(os.path.join(e.state, "requests.log"), "w").close()
    open(logfile, "w").close()

    env = dict(os.environ)
    env["SSHKS_LOGFILE"] = logfile
    env.pop("SSL_CERT_FILE", None)
    env["CURL_CA_BUNDLE"] = os.path.join(e.fx, "cert.pem" if c["ca"] == "good" else "other-ca.pem")
    script = scripts[c["script"] + suffix]
    argv = argv0 + [script] + ([] if user is None else [user])
    t0 = time.time()
    try:
        p = subprocess.run(argv, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=30)
        rc, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        rc, out, err = "TIMEOUT", "", ""
    el = time.time() - t0

    probs = []
    if rc != 0:
        probs.append("rc=%s" % rc)
    if err:
        probs.append("stderr=%r" % err[:120])
    exp_out = "".join(l + "\n" for l in c["out"])
    if out != exp_out:
        probs.append("stdout=%r attendu=%r" % (out[:150], exp_out[:150]))
    # cache
    state = "absent"
    if not c["cache_dir"]:
        state = "nodir" if not os.path.isdir(cache) else "dir-cree"
    elif os.path.exists(kf):
        cur = open(kf).read()
        mt = os.stat(kf).st_mtime_ns
        mode = oct(os.stat(kf).st_mode & 0o777)
        if before and cur == before[0] and mt == before[1]:
            state = "same"
        else:
            state = "modifie"
            if mode != "0o600":
                probs.append("mode cache %s" % mode)
            if before and mt <= before[1]:
                probs.append("mtime non rafraichi")
            if time.time() - mt / 1e9 > 60:
                probs.append("mtime ancien")
    ca = c["cache_after"]
    if isinstance(ca, tuple):
        want = "".join(l + "\n" for l in ca[1]) if ca[0] == "lines" else ca[1]
        if state not in ("modifie", "same") or open(kf).read() != want:
            probs.append("cache=%s contenu=%r attendu=%r" % (state, open(kf).read()[:100] if os.path.exists(kf) else None, want[:100]))
    elif state != ca:
        probs.append("cache=%s attendu=%s" % (state, ca))
    # serial
    sa = c["serial_after"]
    cur_s = open(sf).read() if os.path.exists(sf) else None
    if sa == "same":
        if cur_s != sbefore:
            probs.append(".serial=%r attendu inchange (%r)" % (cur_s, sbefore))
    elif sa is None:
        if cur_s != sbefore:
            probs.append(".serial=%r attendu %r" % (cur_s, sbefore))
    elif cur_s != "# serial: %d\n" % sa:
        probs.append(".serial=%r attendu %d" % (cur_s, sa))
    # fichiers residuels
    if os.path.isdir(cache):
        extra = [f for f in os.listdir(cache) if f not in (ufile + ".keys", ufile + ".serial")]
        if extra:
            probs.append("residus dans le cache: %s" % extra)
    # logs
    logs = open(logfile).read()
    if c["log"] is not None:
        if c["log"] == []:
            if logs:
                probs.append("log inattendu: %r" % logs[:150])
        else:
            for sub in c["log"]:
                if sub not in logs:
                    probs.append("log sans %r (log=%r)" % (sub, logs[:200]))
    if logs and not all(l.startswith("-t ssh-keys-fetch ") for l in logs.splitlines()):
        probs.append("format logger inattendu: %r" % logs[:100])
    reqs = open(os.path.join(e.state, "requests.log")).read().splitlines()
    if c["reqs"] is not None and reqs != c["reqs"]:
        probs.append("requetes=%r attendu %r" % (reqs, c["reqs"]))
    if c["max_s"] and el > c["max_s"]:
        probs.append("duree %.1fs > %.1f" % (el, c["max_s"]))
    if c["min_s"] and el < c["min_s"]:
        probs.append("duree %.1fs < %.1f" % (el, c["min_s"]))
    if os.path.exists("/nonexistent-pwned"):
        probs.append("INJECTION")
    obtenu = "rc=%s, %d ligne(s) en sortie, cache=%s, log=%s, %.1fs" % (
        rc, len(out.splitlines()), state, (logs.strip().splitlines() or ["aucun"])[-1][:80].replace("-t ssh-keys-fetch ", ""), el)
    return dict(id=c["id"], cas=c["desc"], matrice=c["matrix"], attendu=c["expect"], obtenu=obtenu,
                ok=not probs, problems=probs, shell=label)


if __name__ == "__main__":
    sys.exit(main())
