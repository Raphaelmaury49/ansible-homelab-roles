# Serveur MCP malscan : expose le scanner statique (ClamAV+YARA+capa) derriere une cle X-MCP-Key.
# Aucun echantillon n'est execute ; les chemins sont confines au dossier d'echantillons.
import os
from pathlib import Path

from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

import malscan

KEY = os.environ["MCP_UPSTREAM_KEY"]
HOST = os.environ.get("BIND_HOST", "127.0.0.1")
PORT = int(os.environ.get("BIND_PORT", "8101"))
SAMPLES = Path(os.environ.get("MALSCAN_SAMPLES", "/srv/reva/samples")).resolve()


class KeyCheck(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.headers.get("x-mcp-key") != KEY:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


mcp = FastMCP(name="malscan-" + os.environ.get("MALSCAN_NAME", "reva"))


def _inside(path):
    p = Path(path)
    p = (SAMPLES / p).resolve() if not p.is_absolute() else p.resolve()
    if p != SAMPLES and SAMPLES not in p.parents:
        raise ValueError("chemin hors du dossier d'echantillons")
    return p


@mcp.tool()
def scan_sample(path: str) -> dict:
    """Scan statique multi-moteur (ClamAV + YARA + capa + entropie) d'un echantillon deja depose.
    Aucune execution. Verdict: propre / suspect / malveillant, avec le detail par moteur."""
    return malscan.scan(_inside(path))


@mcp.tool()
def list_samples(subdir: str = "") -> dict:
    """Liste les echantillons deposes (memes fichiers que ReVa)."""
    base = _inside(Path(subdir)) if subdir else SAMPLES
    out = []
    for p in sorted(base.rglob("*")):
        if p.is_file():
            st = p.stat()
            out.append({"path": str(p), "size": st.st_size, "mtime": int(st.st_mtime)})
            if len(out) >= 500:
                break
    return {"dir": str(base), "count": len(out), "files": out}


@mcp.tool()
def engine_status() -> dict:
    """Versions des moteurs (ClamAV, YARA, capa) et fraicheur des signatures ClamAV."""
    return malscan.status()


if __name__ == "__main__":
    mcp.run(transport="http", host=HOST, port=PORT, path="/mcp", middleware=[Middleware(KeyCheck)])
