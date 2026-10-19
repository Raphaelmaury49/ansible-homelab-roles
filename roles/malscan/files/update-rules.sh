#!/bin/sh
# Telecharge et compile les regles YARA (yara-forge) et rafraichit les regles capa.
# Best-effort : en cas d'echec reseau, les regles existantes sont conservees.
set -u
YDEST="${1:-/srv/malscan/yara}"
YURL="${MALSCAN_YARA_URL:-https://github.com/YARAHQ/yara-forge/releases/latest/download/yara-forge-rules-core.zip}"
CURL_URL="${MALSCAN_CAPA_RULES_URL:-}"
CDIR="${MALSCAN_CAPA_RULES_DIR:-/srv/malscan/capa-rules}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- YARA ---
if curl -fsSL "$YURL" -o "$TMP/y.zip"; then
  unzip -oq "$TMP/y.zip" -d "$TMP/y"
  : > "$TMP/all.yar"
  find "$TMP/y" \( -name '*.yar' -o -name '*.yara' \) | while read -r f; do
    printf 'include "%s"\n' "$f" >> "$TMP/all.yar"
  done
  if [ -s "$TMP/all.yar" ] && yarac -w "$TMP/all.yar" "$YDEST/compiled.yac.new" 2>"$YDEST/compile.log"; then
    mv "$YDEST/compiled.yac.new" "$YDEST/compiled.yac"
    echo "ok yara"
  else
    echo "yara: compilation en echec, regles precedentes conservees (voir compile.log)" >&2
  fi
else
  echo "yara: telechargement impossible, regles precedentes conservees" >&2
fi

# --- capa-rules ---
if [ -n "$CURL_URL" ] && curl -fsSL "$CURL_URL" -o "$TMP/c.zip"; then
  mkdir -p "$CDIR"
  if unzip -oq "$TMP/c.zip" -d "$CDIR"; then echo "ok capa-rules"; fi
else
  echo "capa-rules: maj ignoree" >&2
fi
