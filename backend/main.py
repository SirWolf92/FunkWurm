import os
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

OCTOPRINT_URL = os.environ.get("OCTOPRINT_URL", "http://localhost:5000").rstrip("/")
OCTOPRINT_API_KEY = os.environ.get("OCTOPRINT_API_KEY", "")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="FunkWurm")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def auth_headers() -> dict:
    return {"X-Api-Key": OCTOPRINT_API_KEY}


_gcode_cache: dict = {}


def _parse_gcode(text: str) -> dict:
    segments = []
    x = y = z = 0.0
    e_prev = 0.0
    abs_mode = True
    min_xyz = [float("inf")] * 3
    max_xyz = [float("-inf")] * 3

    for raw in text.splitlines():
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].upper()

        if cmd == "G90":
            abs_mode = True
            continue
        if cmd == "G91":
            abs_mode = False
            continue
        if cmd == "G92":
            for p in parts[1:]:
                if p[0].upper() == "E":
                    try:
                        e_prev = float(p[1:])
                    except ValueError:
                        pass
            continue

        if cmd not in ("G0", "G1"):
            continue

        nx, ny, nz, ne = x, y, z, e_prev
        for p in parts[1:]:
            axis = p[0].upper()
            try:
                val = float(p[1:])
            except ValueError:
                continue
            if axis == "X":
                nx = val if abs_mode else x + val
            elif axis == "Y":
                ny = val if abs_mode else y + val
            elif axis == "Z":
                nz = val if abs_mode else z + val
            elif axis == "E":
                ne = val if abs_mode else e_prev + val

        extruding = ne > e_prev + 1e-6
        if extruding and (nx != x or ny != y or nz != z):
            segments.append([x, y, z, nx, ny, nz])
            for i, v in enumerate((nx, ny, nz)):
                if v < min_xyz[i]:
                    min_xyz[i] = v
                if v > max_xyz[i]:
                    max_xyz[i] = v

        x, y, z, e_prev = nx, ny, nz, ne

    if not segments:
        min_xyz = [0, 0, 0]
        max_xyz = [0, 0, 0]

    return {
        "segments": segments,
        "bounds": {"min": min_xyz, "max": max_xyz},
        "count": len(segments),
    }


@app.get("/api/status")
async def status():
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{OCTOPRINT_URL}/api/printer",
                headers=auth_headers(),
                timeout=5,
            )
        if r.status_code == 200:
            return {"connected": True, "printer": r.json()}
        return {"connected": False, "status_code": r.status_code}
    except Exception as e:
        return {"connected": False, "error": str(e)}


@app.get("/api/job")
async def job():
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{OCTOPRINT_URL}/api/job",
            headers=auth_headers(),
            timeout=5,
        )
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()


@app.get("/api/gcode")
async def get_gcode():
    """Fetch currently selected gcode from OctoPrint, parse, cache."""
    async with httpx.AsyncClient() as client:
        job_r = await client.get(
            f"{OCTOPRINT_URL}/api/job",
            headers=auth_headers(),
            timeout=5,
        )
    if job_r.status_code != 200:
        raise HTTPException(status_code=job_r.status_code, detail="Kon job info niet ophalen")

    job_data = job_r.json()
    file_info = job_data.get("job", {}).get("file", {})
    path = file_info.get("path")
    origin = file_info.get("origin", "local")

    if not path:
        return {"segments": [], "bounds": {"min": [0, 0, 0], "max": [0, 0, 0]}, "count": 0, "filename": None}

    cache_key = f"{origin}/{path}"
    if cache_key in _gcode_cache:
        cached = _gcode_cache[cache_key]
        return {**cached, "filename": path}

    async with httpx.AsyncClient() as client:
        dl = await client.get(
            f"{OCTOPRINT_URL}/downloads/files/{origin}/{path}",
            headers=auth_headers(),
            timeout=60,
        )
    if dl.status_code != 200:
        raise HTTPException(status_code=dl.status_code, detail="Kon gcode niet downloaden")

    parsed = _parse_gcode(dl.text)
    _gcode_cache[cache_key] = parsed
    return {**parsed, "filename": path}


@app.post("/api/upload")
async def upload_file(
    file: UploadFile = File(...),
    auto_print: bool = Form(False),
    auto_select: bool = Form(True),
):
    """
    Upload een gcode of STL naar OctoPrint.

    auto_select=True → bestand wordt geselecteerd voor printen (veilig)
    auto_print=True → start print direct (alleen als printer klaar is)
    """
    name = file.filename.lower()
    if not (name.endswith(".gcode") or name.endswith(".gco") or name.endswith(".g") or name.endswith(".stl")):
        raise HTTPException(status_code=400, detail="Alleen .gcode, .gco, .g of .stl toegestaan")

    content = await file.read()

    files = {"file": (file.filename, content, "application/octet-stream")}
    data = {
        "select": "true" if auto_select else "false",
        "print": "true" if auto_print else "false",
    }

    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{OCTOPRINT_URL}/api/files/local",
            headers=auth_headers(),
            files=files,
            data=data,
            timeout=60,
        )

    if r.status_code == 201:
        result = r.json()
        _gcode_cache.clear()
        done = result.get("done", False)
        return {
            "success": True,
            "filename": result.get("files", {}).get("local", {}).get("name"),
            "printing": result.get("effectivePrint", False),
            "selected": result.get("effectiveSelect", False),
            "analysis_done": done,
            "message": "Upload geslaagd",
        }
    elif r.status_code == 409:
        raise HTTPException(
            status_code=409,
            detail="Upload conflict: printer is bezig of bestand zou huidige print overschrijven",
        )
    elif r.status_code == 415:
        raise HTTPException(
            status_code=415,
            detail="STL slicing niet beschikbaar. Slice lokaal in Creality Slicer en upload de .gcode.",
        )
    else:
        raise HTTPException(
            status_code=r.status_code,
            detail=f"OctoPrint error: {r.text[:200]}",
        )


@app.post("/api/print/start")
async def print_start():
    """Start print van het huidige geselecteerde bestand."""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{OCTOPRINT_URL}/api/job",
            headers=auth_headers(),
            json={"command": "start"},
            timeout=5,
        )
    if r.status_code == 204:
        return {"success": True}
    raise HTTPException(status_code=r.status_code, detail=r.text)


@app.post("/api/print/cancel")
async def print_cancel():
    """Cancel huidige print."""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{OCTOPRINT_URL}/api/job",
            headers=auth_headers(),
            json={"command": "cancel"},
            timeout=5,
        )
    if r.status_code == 204:
        return {"success": True}
    raise HTTPException(status_code=r.status_code, detail=r.text)


@app.post("/api/print/pause")
async def print_pause():
    """Pause/resume toggle."""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{OCTOPRINT_URL}/api/job",
            headers=auth_headers(),
            json={"command": "pause", "action": "toggle"},
            timeout=5,
        )
    if r.status_code == 204:
        return {"success": True}
    raise HTTPException(status_code=r.status_code, detail=r.text)


@app.get("/api/printer-ready")
async def printer_ready():
    """Check of printer klaar is om te printen (niet bezig, verbonden)."""
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{OCTOPRINT_URL}/api/printer",
            headers=auth_headers(),
            timeout=5,
        )
    if r.status_code != 200:
        return {"ready": False, "reason": "Printer niet verbonden met OctoPrint"}

    state = r.json().get("state", {}).get("flags", {})
    if state.get("printing") or state.get("paused"):
        return {"ready": False, "reason": "Printer is al bezig"}
    if not state.get("operational"):
        return {"ready": False, "reason": "Printer is niet operational"}
    return {"ready": True}


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
