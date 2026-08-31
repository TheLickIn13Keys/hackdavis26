from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import settings
from app.db.store import (
    connect,
    fetch_all_intersections,
    fetch_edge_detail,
    fetch_intersection_detail,
    fetch_scored_edges,
    init_db,
)
from app.routing.graph import load_graph, route_path, route_summary
from app.scoring.geocoding import geocode

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

app = FastAPI(title="Aggie Street Smarts", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    init_db(settings.db_path)
    try:
        load_graph()
    except Exception as exc:  # noqa: BLE001
        log.warning("Routing graph not yet available: %s", exc)


# Demo UI directory — mounted at the bottom of the file, AFTER all API routes,
# so the explicit `/demo/walk` GET handler wins over the StaticFiles fallback.
_DEMO_DIR = Path(__file__).resolve().parents[2] / "demo"


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/scores")
def scores_geojson() -> dict:
    """All bikeable edges as GeoJSON FeatureCollection with safety scores."""
    with connect(settings.db_path) as conn:
        rows = fetch_scored_edges(conn)
    features = [
        {
            "type": "Feature",
            "geometry": r["geometry"],
            "properties": {
                "edge_id": r["edge_id"],
                "name": r["name"],
                "highway": r["highway"],
                "length_m": r["length_m"],
                "score": r["mean_score"],
                "sample_count": r["sample_count"] or 0,
            },
        }
        for r in rows
    ]
    return {"type": "FeatureCollection", "features": features}


@app.get("/intersections")
def intersections_geojson() -> dict:
    """All intersections (degree>=3) as GeoJSON Points, including the
    classification fields the frontend needs to penalize unprotected crossings.

    Properties:
      node_id            — OSM node id
      degree             — count of bikeable legs
      intersection_type  — 't' | 'y' | 'four_way' | 'complex' | null
      osm_control        — 'traffic_signals' | 'stop' | 'give_way' | 'mini_roundabout' | … | null
      gemini_control     — 'signal' | 'all_way_stop' | 'uncontrolled' | … | null
      score              — Gemini-derived 1-10 (null if not photo-scored)
    """
    with connect(settings.db_path) as conn:
        rows = fetch_all_intersections(conn)
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
            "properties": {
                "node_id": r["node_id"],
                "degree": r["degree"],
                "intersection_type": r["intersection_type"],
                "osm_control": r["osm_control"],
                "gemini_control": r["gemini_control"],
                "score": r["score"],
            },
        }
        for r in rows
    ]
    return {"type": "FeatureCollection", "features": features}


class RouteRequest(BaseModel):
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float
    # Additional safety-lambda values to run Dijkstra at. Each one produces a
    # variant route in `variants` with cost_safe = length × (1 + λ × (10−score)/9)
    # plus the configured intersection penalty. Higher λ = more willing to
    # detour around low-score edges. The default `safe` variant uses
    # SAFETY_LAMBDA from env (typically 0.5); pass e.g. [1.5, 3.0] here to get
    # progressively-safer alternatives.
    extra_lambdas: list[float] | None = None


@app.post("/route")
def route(req: RouteRequest) -> dict:
    g = load_graph()
    safe = route_path(g, req.start_lat, req.start_lon, req.end_lat, req.end_lon, "cost_safe")
    fast = route_path(g, req.start_lat, req.start_lon, req.end_lat, req.end_lon, "cost_fast")
    variants: list[dict] = []
    for lam in req.extra_lambdas or []:
        edges = route_path(
            g, req.start_lat, req.start_lon, req.end_lat, req.end_lon, float(lam),
        )
        variants.append(
            {
                "lambda": float(lam),
                "edges": [e.__dict__ for e in edges],
                "summary": route_summary(edges),
            }
        )
    return {
        "safe": {"edges": [e.__dict__ for e in safe], "summary": route_summary(safe)},
        "fast": {"edges": [e.__dict__ for e in fast], "summary": route_summary(fast)},
        "variants": variants,
    }


@app.get("/edge/{edge_id}")
def edge_detail(edge_id: str) -> dict:
    with connect(settings.db_path) as conn:
        detail = fetch_edge_detail(conn, edge_id)
    if detail is None:
        raise HTTPException(404, f"Edge {edge_id} not found")
    return detail


@app.get("/intersection/{node_id}")
def intersection_detail(node_id: int) -> dict:
    with connect(settings.db_path) as conn:
        detail = fetch_intersection_detail(conn, node_id)
    if detail is None:
        raise HTTPException(404, f"Intersection {node_id} not found")
    return detail


@app.get("/geocode")
def geocode_endpoint(q: str = Query(..., min_length=2)) -> dict:
    """Address → lat/lon, biased to Davis bbox."""
    if not settings.google_maps_api_key:
        raise HTTPException(503, "GOOGLE_MAPS_API_KEY not configured")
    with httpx.Client() as client:
        result = geocode(client, q)
    if result is None:
        raise HTTPException(404, "No geocoding result")
    return {
        "lat": result.lat,
        "lon": result.lon,
        "formatted_address": result.formatted_address,
    }


@app.get("/image")
def image(path: str = Query(..., description="Image path returned by /edge/{id}")) -> FileResponse:
    p = Path(path).resolve()
    cache = settings.image_cache_dir.resolve()
    if cache not in p.parents and p.parent != cache:
        raise HTTPException(403, "Path outside image cache")
    if not p.exists():
        raise HTTPException(404, "Image not cached")
    return FileResponse(p, media_type="image/jpeg")


# --- Demo "agent walking" stream -------------------------------------------------
#
# Streams Server-Sent Events to animate a virtual cyclist progressing along the
# safest route. Frontend subscribes to /demo/walk via EventSource and receives
# one event per edge with the edge's geometry, score, and a sample image. Each
# event is delayed by `step_ms` so the UI can animate in real time.

def _format_sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _alerts_for_edge(edge, samples: list[dict]) -> list[dict]:
    """Decide which (if any) agent-perspective alerts to emit before stepping
    onto `edge`. Order is priority: intersection > hazard > low edge score —
    we cap at one alert per step so the toast stack/list stays readable."""
    out: list[dict] = []

    if edge.intersection_score is not None and edge.intersection_score <= 5:
        out.append({
            "kind": "scary_intersection",
            "level": "danger" if edge.intersection_score <= 3 else "warning",
            "title": "Approaching unprotected intersection"
                if edge.intersection_score <= 3 else "Caution: intersection ahead",
            "summary": f"Intersection score {edge.intersection_score:.1f}/10",
        })
        return out

    for s in samples:
        for hz in (s.get("hazards") or []):
            sev = hz.get("severity") or 0
            if sev >= 7:
                hz_type = (hz.get("type") or "hazard").replace("_", " ")
                out.append({
                    "kind": "hazard",
                    "level": "danger" if sev >= 8 else "warning",
                    "title": f"Hazard: {hz_type}",
                    "summary": hz.get("note") or f"severity {sev}/10",
                })
                return out

    if edge.score is not None and edge.score <= 4:
        out.append({
            "kind": "low_score",
            "level": "danger" if edge.score <= 3 else "warning",
            "title": f"Low-safety segment{f' on {edge.name}' if edge.name else ''}",
            "summary": f"Safety {edge.score:.1f}/10 over {int(edge.length_m)} m",
        })

    return out


# GET (not POST) so EventSource can subscribe — the browser's SSE client only
# supports GET. Per-edge playback duration is derived from `speed_mps` (cyclist
# speed) divided by `time_scale` (playback multiplier); falls back to step_ms.
@app.get("/demo/walk")
async def demo_walk(
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
    weight: str = "cost_safe",
    step_ms: int = 600,
    speed_mps: float = 4.0,
    time_scale: float | None = None,
) -> StreamingResponse:
    g = load_graph()

    async def stream():
        edges = route_path(
            g, start_lat, start_lon, end_lat, end_lon,
            "cost_safe" if weight not in ("cost_safe", "cost_fast") else weight,
        )
        summary = route_summary(edges)
        yield _format_sse("start", {
            "weight": weight,
            "edge_count": len(edges),
            "summary": summary,
        })

        with connect(settings.db_path) as conn:
            for i, e in enumerate(edges):
                detail = fetch_edge_detail(conn, e.edge_id)
                samples = (detail or {}).get("samples") or []
                if time_scale and time_scale > 0 and speed_mps > 0:
                    # Real-world traversal time / playback speedup.
                    play_s = (e.length_m / speed_mps) / time_scale
                else:
                    play_s = max(0, step_ms) / 1000.0
                for alert in _alerts_for_edge(e, samples):
                    yield _format_sse("alert", alert)
                yield _format_sse("step", {
                    "i": i,
                    "edge_id": e.edge_id,
                    "name": e.name,
                    "length_m": e.length_m,
                    "score": e.score,
                    "intersection_score": e.intersection_score,
                    # 'gemini' = scored from photos; 'heuristic' = derived from
                    # OSM tags + leg geometry; null = not an intersection.
                    "intersection_source": e.intersection_source,
                    "geometry": e.geometry,
                    "samples": samples,
                    "duration_ms": int(play_s * 1000),
                })
                await asyncio.sleep(play_s)

        yield _format_sse("done", {"summary": summary})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable proxy buffering so events arrive in real time
        },
    )


# Mount the standalone demo UI LAST so explicit API routes above (notably the
# GET /demo/walk SSE handler) take precedence over the StaticFiles fallback.
if _DEMO_DIR.is_dir():
    app.mount("/demo", StaticFiles(directory=str(_DEMO_DIR), html=True), name="demo")
else:
    log.warning("Demo dir not found at %s; /demo will 404", _DEMO_DIR)
