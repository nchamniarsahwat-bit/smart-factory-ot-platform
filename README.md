# Smart Factory OT Reliability, SCADA & Predictive Analytics Platform

> **Solo project by Nontapat Chamniarsahwat — Automation Engineer**
> Allen-Bradley ControlLogix simulation core · ISA-95 MES/ERP · IEC 62443-3-3 · Full OT reliability stack · ML predictive analytics
> Built 100% in VS Code · No hardware required · Python asyncio architecture

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│  L0 – Control Layer (AB ControlLogix Runtime Engine)        │
│  Scan cycle · LD/ST interpreter · AOI · L5X tags · Faults   │
└────────────────────┬────────────────────────────────────────┘
                     │ EtherNet/IP simulation
┌────────────────────▼────────────────────────────────────────┐
│  L1 – OT/IT Gateway  (OPC-UA IEC 62541 · MQTT · WebSocket)  │
│  Purdue zone map · RBAC · Network health · Anomaly detect    │
└────────────────────┬────────────────────────────────────────┘
                     │ FastAPI + asyncio
┌────────────────────▼────────────────────────────────────────┐
│  L2 – SCADA Dashboard  (React · ISA-18.2 Alarms · Historian) │
└────────────────────┬────────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────────┐
│  L3 – MES · MOM · EAM · PM · RCA/CAPA · MOC · IEC 62443    │
│  ISA-95 · SAP/ERP bridge · RCM/FMEA · .L5X version ctrl     │
└────────────────────┬────────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────────┐
│  L4 – ML Analytics  (RF · LSTM · Autoencoder · RL PM opt)   │
└─────────────────────────────────────────────────────────────┘
```

## Modules

| # | Module | Status | Mars JD |
|---|--------|--------|---------|
| M1 | AB ControlLogix Runtime Engine | 🔨 Building | Modify PLC/SCADA |
| M2 | OT/IT Gateway + Network Monitor | ⏳ Planned | OT/IT integration |
| M3 | SCADA Dashboard + ISA-18.2 Alarms | ⏳ Planned | Factory dashboard |
| M4 | ISA-95 MES + ERP Bridge | ⏳ Planned | MES software |
| M5 | MOM + Training Portal | ⏳ Planned | Technical docs |
| M6 | EAM + RCM/FMEA Engine | ⏳ Planned | OT assets |
| M7 | PM Planning Engine | ⏳ Planned | PM plans |
| M8 | RCA Management + CAPA | ⏳ Planned | Breakdown/RCA |
| M9 | MOC + .L5X Version Control | ⏳ Planned | Software backup |
| M10 | IEC 62443-3-3 Cyber Engine | ⏳ Planned | Cyber compliance |
| M11 | ML Predictive Pipeline | ⏳ Planned | Reliability improvement |

## Quick Start

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
python -m core.plc.runtime
```

## Tech Stack

- **Core**: Python 3.11, asyncio, FastAPI, Pydantic v2
- **OT**: opcua-asyncio, paho-mqtt, Mosquitto
- **DB**: TimescaleDB, SQLite, Redis
- **Frontend**: React, Recharts, WebSocket
- **ML**: scikit-learn, PyTorch, Stable-Baselines3
- **Test**: pytest, pytest-asyncio, httpx
