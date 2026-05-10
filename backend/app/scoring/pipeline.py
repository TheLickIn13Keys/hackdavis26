"""End-to-end scoring pipeline.

Run with:
    python -m app.scoring.pipeline extract                # download Davis network
    python -m app.scoring.pipeline score-edges            # multi-heading edge scoring
    python -m app.scoring.pipeline score-intersections    # 4-way intersection scoring
    python -m app.scoring.pipeline aggregate              # recompute per-edge means
    python -m app.scoring.pipeline classify-intersections # backfill osm_control / type
    python -m app.scoring.pipeline statistical-analysis   # run EDA and statistical models
    python -m app.scoring.pipeline all                    # run everything
"""

from __future__ import annotations

import argparse
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
from tqdm import tqdm

from app.config import settings
from app.db.store import (
    already_scored_edges,
    already_scored_intersections,
    connect,
    infer_unscored_edge_scores,
    init_db,
    recompute_edge_scores,
    upsert_intersection_score,
    upsert_sample,
)
from app.scoring import gemini, network, statistical_model, streetview

# bbox = (south, west, north, east). Returned as a tuple for easy unpacking.
Bbox = tuple[float, float, float, float]


def _parse_bbox(s: str | None) -> Bbox | None:
    if not s:
        return None
    parts = [p.strip() for p in s.split(",")]
    if len(parts) != 4:
        raise SystemExit("--bbox must be 'south,west,north,east' (4 comma-separated floats)")
    try:
        south, west, north, east = (float(p) for p in parts)
    except ValueError as exc:
        raise SystemExit(f"--bbox parse error: {exc}")
    if south >= north or west >= east:
        raise SystemExit(
            f"--bbox: south < north and west < east required (got {south},{west},{north},{east})"
        )
    return (south, west, north, east)


def _in_bbox(lat: float, lon: float, bbox: Bbox | None) -> bool:
    if bbox is None:
        return True
    south, west, north, east = bbox
    return south <= lat <= north and west <= lon <= east


log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# httpx logs the full URL (including ?key=…) at INFO. Silence it to keep API keys
# out of console logs and any pasted output.
logging.getLogger("httpx").setLevel(logging.WARNING)


def cmd_extract() -> None:
    log.info("Downloading Davis OSM network…")
    g = network.download_davis_graph()
    log.info("Graph: %d nodes, %d edges", g.number_of_nodes(), g.number_of_edges())
    persisted = network.persist_graph(g)
    log.info("Persisted %d bikeable edges to %s", persisted, settings.db_path)
    nodes = network.intersection_samples(g)
    log.info("Identified %d intersections (degree>=3)", len(nodes))


def _fetch_images_for(
    sv_client: httpx.Client, lat: float, lon: float, headings: list[float]
) -> list[dict]:
    """Resolve metadata once, then fetch one image per heading. Returns image records."""
    meta = streetview.metadata(sv_client, lat, lon)
    if meta is None:
        return []
    images: list[dict] = []
    for h in headings:
        try:
            img_path = streetview.fetch_image(sv_client, meta.pano_id, h)
            images.append({"heading": h, "pano_id": meta.pano_id, "image_path": str(img_path)})
        except Exception as exc:  # noqa: BLE001
            log.warning("image fetch failed (pano=%s heading=%s): %s", meta.pano_id, h, exc)
    return images


def _score_edge_sample(sv_client, gem_client, sample) -> dict:
    images = _fetch_images_for(sv_client, sample.lat, sample.lon, sample.headings)
    if not images:
        return {
            "edge_id": sample.edge_id,
            "lat": sample.lat,
            "lon": sample.lon,
            "images": [],
            "score": None,
            "infrastructure": None,
            "hazards": [{"type": "no_streetview", "severity": 1, "image_index": 0}],
            "reasons": ["No Street View imagery available within 50m"],
            "raw": None,
        }
    paths = [Path(im["image_path"]) for im in images]
    a = gemini.score_segment_images(
        gem_client, paths, osm_context=getattr(sample, "osm_tags", None) or None
    )
    return {
        "edge_id": sample.edge_id,
        "lat": sample.lat,
        "lon": sample.lon,
        "images": images,
        "score": a.score,
        "infrastructure": a.infrastructure,
        "hazards": a.hazards,
        "reasons": a.reasons,
        "raw": a.raw,
    }


def _score_intersection(sv_client, gem_client, isect) -> dict:
    images = _fetch_images_for(sv_client, isect.lat, isect.lon, isect.headings)
    if not images:
        return {
            "node_id": isect.node_id,
            "images": [],
            "score": None,
            "control": None,
            "hazards": [{"type": "no_streetview", "severity": 1, "image_index": 0}],
            "reasons": ["No Street View imagery at intersection"],
            "raw": None,
        }
    paths = [Path(im["image_path"]) for im in images]
    osm_ctx = dict(getattr(isect, "osm_context", None) or {})
    osm_ctx["degree"] = isect.degree
    a = gemini.score_intersection_images(gem_client, paths, osm_context=osm_ctx)
    return {
        "node_id": isect.node_id,
        "images": images,
        "score": a.score,
        "control": a.control,
        "hazards": a.hazards,
        "reasons": a.reasons,
        "raw": a.raw,
    }


def cmd_score_edges(
    max_edges: int | None, workers: int, force: bool, bbox: Bbox | None = None,
) -> None:
    init_db(settings.db_path)
    log.info("Loading network…")
    g = network.download_davis_graph()
    network.persist_graph(g)

    with connect(settings.db_path) as conn:
        scored = set() if force else already_scored_edges(conn)

    samples: list = []
    seen_edges: set[str] = set()
    for s in network.all_sample_points(g, settings.sample_interval_m):
        if not _in_bbox(s.lat, s.lon, bbox):
            continue
        if s.edge_id in scored:
            continue
        samples.append(s)
        seen_edges.add(s.edge_id)
        if max_edges is not None and len(seen_edges) >= max_edges:
            break

    log.info("Will score %d samples across %d edges (skip %d already-scored%s)",
             len(samples), len(seen_edges), len(scored),
             "; bbox-filtered" if bbox else "")
    sv_client = streetview.make_client()
    gem_client = gemini.make_client()

    # Write each result as it completes with a commit every COMMIT_BATCH samples,
    # so a kill or crash mid-run doesn't throw away the Gemini work we paid for.
    # Re-aggregate edge_scores every AGGREGATE_BATCH samples so the coverage
    # page (which reads edge_scores, not samples) stays roughly fresh during
    # long runs.
    COMMIT_BATCH = 20
    AGGREGATE_BATCH = 100
    persisted = 0
    with connect(settings.db_path) as conn, \
         ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_score_edge_sample, sv_client, gem_client, s) for s in samples]
        for fut in tqdm(as_completed(futures), total=len(futures), desc="edges"):
            try:
                r = fut.result()
            except Exception as exc:  # noqa: BLE001
                log.warning("edge sample failed: %s", exc)
                continue
            upsert_sample(
                conn,
                edge_id=r["edge_id"],
                lat=r["lat"],
                lon=r["lon"],
                images=r["images"],
                score=r["score"],
                infrastructure=r["infrastructure"],
                hazards=r["hazards"],
                reasons=r["reasons"],
                raw_response=r["raw"],
            )
            persisted += 1
            if persisted % COMMIT_BATCH == 0:
                conn.commit()
            if persisted % AGGREGATE_BATCH == 0:
                recompute_edge_scores(conn)
                conn.commit()
        conn.commit()
        n = recompute_edge_scores(conn)
        m = infer_unscored_edge_scores(conn)
    log.info("Persisted %d samples; recomputed %d measured edge scores; inferred %d from tags",
             persisted, n, m)


def cmd_score_intersections(
    max_nodes: int | None, workers: int, force: bool, bbox: Bbox | None = None,
) -> None:
    init_db(settings.db_path)
    log.info("Loading network for intersection scan…")
    g = network.download_davis_graph()
    nodes = network.intersection_samples(g)

    with connect(settings.db_path) as conn:
        scored = set() if force else already_scored_intersections(conn)

    todo = [n for n in nodes if n.node_id not in scored and _in_bbox(n.lat, n.lon, bbox)]
    if max_nodes is not None:
        todo = todo[:max_nodes]
    log.info("Will score %d intersections (skip %d already-scored%s)",
             len(todo), len(scored), "; bbox-filtered" if bbox else "")

    sv_client = streetview.make_client()
    gem_client = gemini.make_client()

    COMMIT_BATCH = 20
    persisted = 0
    with connect(settings.db_path) as conn, \
         ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_score_intersection, sv_client, gem_client, n) for n in todo]
        for fut in tqdm(as_completed(futures), total=len(futures), desc="intersections"):
            try:
                r = fut.result()
            except Exception as exc:  # noqa: BLE001
                log.warning("intersection failed: %s", exc)
                continue
            upsert_intersection_score(
                conn,
                node_id=r["node_id"],
                images=r["images"],
                score=r["score"],
                hazards=r["hazards"],
                reasons=r["reasons"],
                raw_response=r["raw"],
                gemini_control=r.get("control"),
            )
            persisted += 1
            if persisted % COMMIT_BATCH == 0:
                conn.commit()
        conn.commit()
    log.info("Persisted %d intersection scores", persisted)


def cmd_aggregate() -> None:
    with connect(settings.db_path) as conn:
        n = recompute_edge_scores(conn)
        m = infer_unscored_edge_scores(conn)
    log.info("Recomputed %d measured edge scores; inferred %d from highway tags", n, m)


def cmd_classify_intersections() -> None:
    """Backfill intersection_type, osm_control, and gemini_control on existing
    rows. No API calls — pure DB + OSM-graph lookups, so it's free and fast.

    Use this:
      * After upgrading from a DB created before these columns existed.
      * After running score-intersections with old code that didn't populate
        gemini_control (we promote it from raw_response_json here).
    """
    init_db(settings.db_path)
    log.info("Loading network for intersection classification…")
    g = network.download_davis_graph()
    g_undirected = g.to_undirected()

    n_geom = n_osm = n_gem = n_unknown = 0
    with connect(settings.db_path) as conn:
        rows = conn.execute(
            "SELECT node_id, raw_response_json FROM intersections"
        ).fetchall()
        for r in tqdm(rows, desc="classifying"):
            node_id = r["node_id"]
            updates: list[str] = []
            params: list = []
            if node_id in g_undirected.nodes:
                node_data = g_undirected.nodes[node_id]
                bearings = network.node_leg_bearings(g_undirected, node_id)
                itype = network.classify_intersection_type(len(bearings), bearings)
                osm_ctrl = network.osm_control_tag(node_data)
                updates += ["intersection_type = ?", "osm_control = ?"]
                params += [itype, osm_ctrl]
                n_geom += 1
                if osm_ctrl is not None:
                    n_osm += 1
            else:
                n_unknown += 1
            # Promote Gemini's `control` from the raw response into its column.
            if r["raw_response_json"]:
                try:
                    raw = json.loads(r["raw_response_json"])
                    gctrl = raw.get("control")
                except (TypeError, ValueError, json.JSONDecodeError):
                    gctrl = None
                if gctrl:
                    updates.append("gemini_control = ?")
                    params.append(gctrl)
                    n_gem += 1
            if not updates:
                continue
            params.append(node_id)
            conn.execute(
                f"UPDATE intersections SET {', '.join(updates)} WHERE node_id = ?",
                params,
            )
        conn.commit()
    log.info(
        "Classified %d (geom) · %d (OSM control) · %d (Gemini control) · %d unknown nodes",
        n_geom, n_osm, n_gem, n_unknown,
    )


def cmd_statistical_analysis(output_dir: Path) -> None:
    """Run statistical analysis and modeling on the safety scoring data."""
    log.info("Running statistical analysis...")
    results = statistical_model.run_statistical_analysis(settings.db_path, output_dir)
    log.info("Statistical analysis completed. Check %s for results.", output_dir)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("extract")

    p_e = sub.add_parser("score-edges")
    p_e.add_argument("--max-edges", type=int, default=None)
    p_e.add_argument("--workers", type=int, default=4)
    p_e.add_argument("--force", action="store_true")
    p_e.add_argument("--bbox", type=str, default=None,
                     help="Filter to bbox 'south,west,north,east' "
                          "(e.g. '38.535,-121.760,38.555,-121.735' for downtown Davis)")

    p_i = sub.add_parser("score-intersections")
    p_i.add_argument("--max-nodes", type=int, default=None)
    p_i.add_argument("--workers", type=int, default=4)
    p_i.add_argument("--force", action="store_true")
    p_i.add_argument("--bbox", type=str, default=None,
                     help="Filter to bbox 'south,west,north,east'")

    sub.add_parser("aggregate")
    sub.add_parser("classify-intersections")
    p_s = sub.add_parser("statistical-analysis")
    p_s.add_argument("--output-dir", type=str, default="analysis_output",
                     help="Directory to save analysis results and plots")
    sub.add_parser("all")
    args = parser.parse_args()

    if args.cmd == "extract":
        cmd_extract()
    elif args.cmd == "score-edges":
        cmd_score_edges(args.max_edges, args.workers, args.force, _parse_bbox(args.bbox))
    elif args.cmd == "score-intersections":
        cmd_score_intersections(args.max_nodes, args.workers, args.force, _parse_bbox(args.bbox))
    elif args.cmd == "aggregate":
        cmd_aggregate()
    elif args.cmd == "classify-intersections":
        cmd_classify_intersections()
    elif args.cmd == "statistical-analysis":
        cmd_statistical_analysis(Path(args.output_dir))
    elif args.cmd == "all":
        cmd_extract()
        cmd_score_edges(None, 4, False, None)
        cmd_score_intersections(None, 4, False, None)
        cmd_aggregate()
        cmd_classify_intersections()


if __name__ == "__main__":
    main()
