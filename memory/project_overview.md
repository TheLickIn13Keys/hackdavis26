---
name: HackDavis 2026 — Bike Safety Routing
description: Backend-only hackathon project that uses Street View + Gemini Flash to score Davis road segments (and intersections) for cyclist safety, then routes around the dangerous bits.
type: project
---

HackDavis 2026 project. A "Street View agent" walks Davis virtually, scores every road segment **and intersection** for cyclist safety, then routes users along safest (not fastest) paths.

**Stack decisions**
- Backend: Python + FastAPI (this repo)
- Frontend: handed off to a teammate — **frontend was deleted from this repo**, do not recreate it
- Vision model: Gemini 2.5 Flash, structured JSON via response_schema
- Routing: OSMnx + NetworkX Dijkstra (chose over OSRM because hackathon timeline — OSRM custom-profile rebuild is fiddly)
- Storage: SQLite, schema in `app/db/store.py`
- Coverage: Davis citywide, ~3k sample points, ~$5 Gemini + ~$21 Street View

**Pipeline architecture (two passes)**
1. **Edge pass** — sample every ~80m along bikeable roads. Each sample fetches **2 Street View images** (forward + 90° right) and sends both to Gemini in one call → one combined safety score per location.
2. **Intersection pass** — every OSM node with bikeable degree ≥ 3 is scored from **4 cardinal-direction images** with a separate intersection-specific prompt (signal type, slip lanes, turn-conflict potential).

**Routing cost model**
```
cost_fast = length_m
cost_safe = length_m × (1 + λ × (10 − edge_score)/9)
          + INTERSECTION_PENALTY_M × (10 − dest_node_score)/9    if dest is scored
```
λ controlled by `SAFETY_LAMBDA` env (default 0.5). Intersection penalty default = 60m of "felt distance" for a score-1 intersection.

**Why:** Standard cycling routers optimize speed/distance and ignore whether the route feels safe. Edge scoring catches scary streets; intersection scoring catches scary turns — together they let Dijkstra detour around both.

**How to apply:** Default to NetworkX-based routing for any planning. If asked to switch to OSRM, push back unless we have buffer time. Vision model is Gemini Flash unless quality demands Pro. Don't add a frontend back to this repo — that's someone else's work.

**Pipeline commands**
- `python -m app.scoring.pipeline extract`
- `python -m app.scoring.pipeline score-edges [--max-edges N --workers N --force]`
- `python -m app.scoring.pipeline score-intersections [--max-nodes N --workers N --force]`
- `python -m app.scoring.pipeline aggregate`
- `python -m app.scoring.pipeline all`

**Demo angle**
`POST /demo/walk` is an SSE stream that animates a virtual cyclist progressing along the safest route, emitting per-edge events with geometry + score + a Street View image + Gemini's hazards. This is the "agent walking around" framing the project was pitched as — meant to be the demo centerpiece.
