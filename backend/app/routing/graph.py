"""NetworkX graph annotated with edge + intersection safety scores.

Edge cost model:
    cost_fast = length_m
    cost_safe = length_m * (1 + λ * (10 − edge_score) / 9)
              + intersection_penalty_m * (10 − dest_node_score) / 9   if dest is scored

Unscored edges fall back to a neutral score (5). The graph is built once at
startup and cached.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from functools import lru_cache

import networkx as nx
import osmnx as ox

from app.config import settings
from app.db.store import connect, fetch_intersection_routing_data

log = logging.getLogger(__name__)

NEUTRAL_SCORE = 5.0


# Heuristic intersection score (1–10) derived from OSM tags + geometry alone.
# Used as a fallback when we don't have a Gemini-derived score for the node.
# Calibrated so the average urban intersection sits around 6 and obviously
# scary ones (uncontrolled complex / 4-way arterial) drop below 4.
TYPE_BASE_SCORE = {
    "y": 8.0,            # angled junctions, naturally low-speed turns
    "t": 7.0,            # one through-road; lateral conflict only on the stem
    "four_way": 5.5,     # full cross-traffic conflict
    "complex": 3.5,      # 5+ legs — stay away
    "unknown": 6.0,
}

CONTROL_BONUS = {
    "traffic_signals": 1.5,    # signals manage car-bike conflict
    "all_way_stop": 1.0,       # everyone stops, predictable
    "stop": 0.8,               # cyclist's leg stops; cross may not
    "give_way": 0.4,
    "mini_roundabout": 1.2,    # great for cyclists, low speed
    "crossing": 0.5,
    None: 0.0,                 # uncontrolled (the most common in Davis)
}


def heuristic_intersection_score(
    degree: int | None, intersection_type: str | None, osm_control: str | None
) -> float:
    base = TYPE_BASE_SCORE.get(intersection_type or "unknown", 6.0)
    # If we don't know the type but have degree, use degree as a fallback signal.
    if intersection_type is None and degree is not None:
        base = TYPE_BASE_SCORE["four_way" if degree == 4 else "complex" if degree >= 5 else "t"]
    bonus = CONTROL_BONUS.get(osm_control, 0.0)
    return max(1.0, min(10.0, base + bonus))


def effective_intersection_score(node_data: dict | None) -> tuple[float, str] | None:
    """Return (score, source) for an intersection node, or None if it's not an
    intersection at all (degree<3, end of road, etc.). source ∈ {'gemini','heuristic'}."""
    if node_data is None:
        return None
    g = node_data.get("gemini_score")
    if g is not None:
        return float(g), "gemini"
    return heuristic_intersection_score(
        node_data.get("degree"),
        node_data.get("intersection_type"),
        node_data.get("osm_control"),
    ), "heuristic"


@dataclass
class RoutedEdge:
    edge_id: str
    u: int
    v: int
    name: str | None
    length_m: float
    score: float | None
    intersection_score: float | None  # score of the destination node, if any
    intersection_source: str | None   # 'gemini' | 'heuristic' | None
    geometry: dict


def _segment_cost(length_m: float, score: float | None, lam: float) -> float:
    s = score if score is not None else NEUTRAL_SCORE
    return length_m * (1.0 + lam * (10.0 - s) / 9.0)


def _intersection_addend(score: float | None, penalty_m: float) -> float:
    if score is None:
        return 0.0
    return penalty_m * (10.0 - score) / 9.0


@lru_cache(maxsize=1)
def load_graph() -> nx.MultiDiGraph:
    log.info("Loading OSM graph for routing…")
    n, s, e, w = settings.bbox
    g = ox.graph_from_bbox(bbox=(w, s, e, n), network_type="bike", simplify=True, retain_all=False)

    log.info("Loading edge + intersection data from DB…")
    with connect(settings.db_path) as conn:
        edge_rows = conn.execute("SELECT edge_id, mean_score FROM edge_scores").fetchall()
        edge_scores = {r["edge_id"]: r["mean_score"] for r in edge_rows}
        intersection_data = fetch_intersection_routing_data(conn)

    lam = settings.safety_lambda
    pen = settings.intersection_penalty_m
    annotated_edges = gemini_isects = heuristic_isects = 0

    for u, v, key, data in g.edges(keys=True, data=True):
        edge_id = f"{u}-{v}-{key}"
        score = edge_scores.get(edge_id)
        length_m = float(data.get("length", 0.0))
        # Effective intersection score: Gemini if we have it, otherwise an
        # OSM-tag-derived heuristic. None only if the dest isn't an intersection
        # (degree<3 — end of road or pass-through node).
        eff = effective_intersection_score(intersection_data.get(int(v)))
        if eff is None:
            dest_score = None
            dest_source = None
        else:
            dest_score, dest_source = eff
            if dest_source == "gemini":
                gemini_isects += 1
            else:
                heuristic_isects += 1

        seg_cost = _segment_cost(length_m, score, lam)
        isect_addend = _intersection_addend(dest_score, pen)

        data["edge_id"] = edge_id
        data["safety_score"] = score
        data["dest_intersection_score"] = dest_score
        data["dest_intersection_source"] = dest_source
        data["cost_fast"] = length_m
        data["cost_safe"] = seg_cost + isect_addend

        if score is not None:
            annotated_edges += 1

    log.info(
        "Annotated %d/%d edges with safety. Intersection penalties: %d Gemini, %d heuristic",
        annotated_edges, g.number_of_edges(), gemini_isects, heuristic_isects,
    )
    return g


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def nearest_node(g: nx.MultiDiGraph, lat: float, lon: float) -> int:
    best_node = None
    best_dist = float("inf")
    for node, data in g.nodes(data=True):
        d = _haversine(lat, lon, data["y"], data["x"])
        if d < best_dist:
            best_dist = d
            best_node = node
    if best_node is None:
        raise ValueError("Empty graph")
    return best_node


def _edge_safety_cost_at_lambda(data: dict, lam: float, intersection_penalty_m: float) -> float:
    """Same shape as the cached cost_safe, but recomputed for an arbitrary
    lambda. Used to generate extra-safe variants without rebuilding the
    routing graph."""
    length_m = float(data.get("length", 0.0))
    score = data.get("safety_score")
    s = score if score is not None else NEUTRAL_SCORE
    seg_cost = length_m * (1.0 + lam * (10.0 - s) / 9.0)
    dest_score = data.get("dest_intersection_score")
    isect = (
        intersection_penalty_m * (10.0 - dest_score) / 9.0
        if dest_score is not None
        else 0.0
    )
    return seg_cost + isect


def route_path(
    g: nx.MultiDiGraph,
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
    weight: str | float,
) -> list[RoutedEdge]:
    """Compute shortest path under a weight.

    `weight` is either:
      * a string ("cost_safe" or "cost_fast") — use the precomputed edge attr
      * a float — interpret as a safety lambda; cost is recomputed on the fly
        with `_edge_safety_cost_at_lambda(data, lam, intersection_penalty_m)`.
        Use this to generate extra-safe variants beyond the env's default
        SAFETY_LAMBDA without rebuilding the cached graph.
    """
    src = nearest_node(g, start_lat, start_lon)
    dst = nearest_node(g, end_lat, end_lon)

    if isinstance(weight, str):
        weight_arg: str | object = weight
        edge_picker = lambda kv: kv[1].get(weight, float("inf"))  # noqa: E731
    else:
        lam = float(weight)
        pen = settings.intersection_penalty_m

        def callable_weight(_u: int, _v: int, edges_dict: dict) -> float:
            # MultiDiGraph: we get the dict of all parallel edges. Take the min.
            best = float("inf")
            for data in edges_dict.values():
                cost = _edge_safety_cost_at_lambda(data, lam, pen)
                if cost < best:
                    best = cost
            return best

        weight_arg = callable_weight
        edge_picker = lambda kv: _edge_safety_cost_at_lambda(kv[1], lam, pen)  # noqa: E731

    nodes = nx.shortest_path(g, src, dst, weight=weight_arg)
    out: list[RoutedEdge] = []
    for u, v in zip(nodes[:-1], nodes[1:]):
        edges = g.get_edge_data(u, v) or {}
        if not edges:
            continue
        best_key, best_data = min(edges.items(), key=edge_picker)
        geom = best_data.get("geometry")
        if geom is not None:
            geometry = {"type": "LineString", "coordinates": list(geom.coords)}
        else:
            pu, pv = g.nodes[u], g.nodes[v]
            geometry = {"type": "LineString", "coordinates": [[pu["x"], pu["y"]], [pv["x"], pv["y"]]]}
        name = best_data.get("name")
        if isinstance(name, list):
            name = ", ".join(str(n) for n in name)
        out.append(RoutedEdge(
            edge_id=best_data.get("edge_id", f"{u}-{v}-{best_key}"),
            u=u,
            v=v,
            name=name,
            length_m=float(best_data.get("length", 0.0)),
            score=best_data.get("safety_score"),
            intersection_score=best_data.get("dest_intersection_score"),
            intersection_source=best_data.get("dest_intersection_source"),
            geometry=geometry,
        ))
    return out


def route_summary(edges: list[RoutedEdge]) -> dict:
    total_m = sum(e.length_m for e in edges)
    scored = [e for e in edges if e.score is not None]
    weighted_score = (
        sum(e.score * e.length_m for e in scored) / sum(e.length_m for e in scored)
        if scored else None
    )
    isect_scored = [e for e in edges if e.intersection_score is not None]
    weighted_isect = (
        sum(e.intersection_score for e in isect_scored) / len(isect_scored)
        if isect_scored else None
    )
    return {
        "length_m": total_m,
        "weighted_safety_score": weighted_score,
        "scored_fraction": (sum(e.length_m for e in scored) / total_m) if total_m else 0.0,
        "intersections_traversed": len(isect_scored),
        "mean_intersection_score": weighted_isect,
    }
