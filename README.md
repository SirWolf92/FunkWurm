# FunkWurm

G-code visualizer met OctoPrint upload + print controle.

## Setup

```bash
cd backend
pip install -r requirements.txt
export OCTOPRINT_URL=http://octopi.local
export OCTOPRINT_API_KEY=your-api-key
uvicorn main:app --reload
```

Open http://localhost:8000

## Features

- 3D visualizer voor geselecteerde gcode uit OctoPrint
- Upload .gcode / .gco / .g / .stl
- Preview voor start (standaard — aanbevolen)
- Optionele directe print na upload (met extra safety checks)
- Print start / pause / cancel knoppen
- Drag and drop upload

## Safety

Direct-print is uitgeschakeld tenzij expliciet aangevinkt. De backend checkt printer-ready status voordat het direct-print accepteert. STL uploads geven een duidelijke foutmelding als OctoPrint geen slicing plugin heeft.

## Endpoints

- `GET /api/status` — OctoPrint printer state
- `GET /api/job` — huidige job info
- `GET /api/gcode` — parsed segments van geselecteerde file
- `POST /api/upload` — upload bestand (form: file, auto_print, auto_select)
- `POST /api/print/start` — start print
- `POST /api/print/pause` — pause/resume toggle
- `POST /api/print/cancel` — cancel print
- `GET /api/printer-ready` — ready check
