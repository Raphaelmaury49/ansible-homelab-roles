#!/bin/sh
# Affiche l'IP source utilisee par le controleur pour joindre l'hote $1 (vide si inconnue).
ip -4 -o route get "$1" 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "src") print $(i + 1)}'
