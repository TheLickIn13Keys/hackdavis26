/**
 * Adapter between the SafePath backend (/route) and the frontend's Route[] shape.
 *
 * The backend returns a list of edges (each with a 1–10 score, intersection
 * score, and LineString geometry). This module groups consecutive edges with
 * the same safety bucket into RouteSegment[] and computes the headline
 * stats (distance, ETA, 0–100 score, subtitle) the side panel renders.
 */

import {
  fetchSafePathRoute,
  type RouteEdge,
  type RouteResult,
} from "@/lib/safepath-api";
import type {
  Route,
  RouteIntel,
  RoutePoint,
  RouteSegment,
  SafetyLevel,
} from "./types";

/** Casual cycling speed used to estimate ETA from length when the backend
 *  doesn't return a duration. ~16 km/h, matches our default in the SSE walk. */
const CRUISE_SPEED_MPS = 4.5;

/** Map a 1–10 backend score to the frontend's safe/caution/danger bucket.
 *  These thresholds line up with the heatmap colors so the route segment
 *  colors match the underlying data layer. */
function scoreToLevel(score: number | null): SafetyLevel {
  if (score == null) return "caution";
  if (score >= 7) return "safe";
  if (score >= 4) return "caution";
  return "danger";
}

function reasonForEdge(edge: RouteEdge): string | undefined {
  const name = edge.name?.trim();
  if (edge.score == null) {
    return name ? `${name} — coverage gap` : "Unscored stretch";
  }
  const score = edge.score.toFixed(1);
  if (edge.score < 4) {
    return name
      ? `${name} — sketchy stretch, score ${score}/10`
      : `Sketchy stretch (score ${score}/10)`;
  }
  if (edge.score < 7) {
    return name
      ? `${name} — mixed safety (${score}/10)`
      : `Mixed safety (${score}/10)`;
  }
  return name
    ? `${name} — calm, ${score}/10`
    : `Calm stretch (${score}/10)`;
}

/** Group consecutive edges with the same safety level into segments. The
 *  geometry of contiguous edges is concatenated, dropping the duplicate
 *  vertex at each join. */
function edgesToSegments(edges: RouteEdge[]): RouteSegment[] {
  const segments: RouteSegment[] = [];
  let current: RouteSegment | null = null;
  let stressBuffer: string[] = [];

  for (const edge of edges) {
    const level = scoreToLevel(edge.score);
    const points: RoutePoint[] = edge.geometry.coordinates.map(([lng, lat]) => ({
      lng,
      lat,
    }));
    const reason = reasonForEdge(edge);

    if (current && current.level === level) {
      // Continue the existing segment — skip the first point because it
      // duplicates the previous edge's last point.
      const skip = current.points.length > 0 ? 1 : 0;
      current.points.push(...points.slice(skip));
      current.edgeIds?.push(edge.edge_id);
      if (reason) stressBuffer.push(reason);
    } else {
      if (current) {
        if (stressBuffer.length) current.stressNotes = dedupe(stressBuffer);
        segments.push(current);
      }
      current = {
        id: `seg-${segments.length}-${edge.edge_id}`,
        level,
        points,
        edgeIds: [edge.edge_id],
        reason,
      };
      stressBuffer = reason ? [reason] : [];
    }
  }
  if (current) {
    if (stressBuffer.length) current.stressNotes = dedupe(stressBuffer);
    segments.push(current);
  }
  return segments;
}

function dedupe(items: string[]): string[] {
  return [...new Set(items.map((s) => s.trim()).filter(Boolean))];
}

function summarize(result: RouteResult): {
  distanceMi: number;
  durationMin: number;
  score0to100: number;
  subtitle: string;
} {
  const distanceMi = result.summary.length_m / 1609.344;
  const durationMin = Math.max(
    1,
    Math.round(result.summary.length_m / CRUISE_SPEED_MPS / 60),
  );
  const safety10 = result.summary.weighted_safety_score ?? 6;
  const score0to100 = Math.round(Math.max(20, Math.min(99, safety10 * 10)));
  // Build a subtitle from the most prominent named edges along the route.
  const namedEdges = result.edges
    .filter((e) => (e.name ?? "").trim().length > 0)
    .map((e) => (e.name as string).trim());
  const uniqueNames: string[] = [];
  for (const n of namedEdges) {
    if (!uniqueNames.includes(n)) uniqueNames.push(n);
    if (uniqueNames.length >= 3) break;
  }
  const subtitle = uniqueNames.length
    ? `Via ${uniqueNames.join(" → ")}`
    : "SafePath routing";
  return { distanceMi, durationMin, score0to100, subtitle };
}

function buildIntel(label: string, result: RouteResult): RouteIntel {
  const lines: string[] = [];
  const { weighted_safety_score, scored_fraction, intersections_traversed, mean_intersection_score } =
    result.summary;

  if (weighted_safety_score != null) {
    lines.push(
      `Length-weighted safety score ${weighted_safety_score.toFixed(1)} / 10` +
        ` across ${(scored_fraction * 100).toFixed(0)}% scored coverage.`,
    );
  } else {
    lines.push("No measured safety scores along this route — coverage gap.");
  }
  lines.push(
    `${intersections_traversed} intersection${intersections_traversed === 1 ? "" : "s"} traversed` +
      (mean_intersection_score != null
        ? `, avg intersection score ${mean_intersection_score.toFixed(1)} / 10.`
        : "."),
  );
  lines.push(
    `${label} variant — Dijkstra over OSM bike network with ` +
      (label.toLowerCase().includes("safe")
        ? "safety-weighted edge costs."
        : "raw distance costs."),
  );

  return {
    mapbox: {
      lines,
      scorePenalty: 0,
      peakPostedMph: null,
      motorwayTouches: 0,
      tunnelTouches: 0,
    },
  };
}

/** True if the two edge sequences resolve to the same path (same edge_ids in
 *  the same order). When safe and fast converge this lets the page render a
 *  single "Safest & Fastest" card rather than two duplicate options. */
function sameEdgeSequence(a: RouteEdge[], b: RouteEdge[]): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i += 1) {
    if (a[i].edge_id !== b[i].edge_id) return false;
  }
  return true;
}

function buildRoute(
  id: string,
  initialName: string,
  result: RouteResult,
): Route {
  const segments = edgesToSegments(result.edges);
  const { distanceMi, durationMin, score0to100, subtitle } = summarize(result);
  return {
    id,
    name: initialName,
    subtitle,
    durationMin,
    distanceMi,
    score: score0to100,
    segments,
    intel: buildIntel(initialName, result),
  };
}

/** Pick a friendly label for an extra-lambda variant based on how aggressive
 *  it is relative to the env's default SAFETY_LAMBDA (0.5). */
function labelForLambda(lam: number): string {
  if (lam <= 0.75) return `SafePath safer (λ=${lam.toFixed(1)})`;
  if (lam <= 1.5) return `SafePath extra-safe (λ=${lam.toFixed(1)})`;
  if (lam <= 3) return `SafePath max-safe (λ=${lam.toFixed(1)})`;
  return `SafePath ultra-safe (λ=${lam.toFixed(1)})`;
}

export async function fetchSafePathBikeRoutes(
  origin: RoutePoint,
  destination: RoutePoint,
  opts: { extraLambdas?: number[]; signal?: AbortSignal } = {},
): Promise<Route[]> {
  const response = await fetchSafePathRoute(
    { lat: origin.lat, lng: origin.lng },
    { lat: destination.lat, lng: destination.lng },
    { extraLambdas: opts.extraLambdas, signal: opts.signal },
  );

  if (!response.safe.edges.length && !response.fast.edges.length) {
    throw new Error("SafePath returned no route");
  }

  // IDs prefixed so they don't collide with Mapbox-derived routes (which use
  // bare "safest" / "balanced" / "fastest"). Page-level renaming then picks
  // labels based on the combined sort, so the prefix is invisible to users.
  const routes: Route[] = [];

  const safeRoute = buildRoute("safepath-safe", "SafePath safest", response.safe);
  const fastRoute = buildRoute("safepath-fast", "SafePath fastest", response.fast);
  const safeFastSame = sameEdgeSequence(response.safe.edges, response.fast.edges);

  if (safeFastSame) {
    routes.push({ ...safeRoute, name: "SafePath route" });
  } else {
    routes.push(safeRoute, fastRoute);
  }

  // Extra-lambda variants — drop ones whose path is identical to safe / fast
  // or to a previously-added variant. Otherwise two near-identical lambdas
  // (e.g. 1.5 and 3.0 in a small bbox) produce duplicate green lines.
  const acceptedEdgeSequences: RouteEdge[][] = [response.safe.edges];
  if (!safeFastSame) acceptedEdgeSequences.push(response.fast.edges);
  for (const v of response.variants ?? []) {
    if (!v.edges.length) continue;
    if (acceptedEdgeSequences.some((seq) => sameEdgeSequence(v.edges, seq))) continue;
    const id = `safepath-l${v.lambda.toFixed(2).replace(".", "_")}`;
    const result: RouteResult = { edges: v.edges, summary: v.summary };
    routes.push(buildRoute(id, labelForLambda(v.lambda), result));
    acceptedEdgeSequences.push(v.edges);
  }

  return routes;
}

/** Convenience: just the safety-optimized route (the first one). Returns null
 *  if the backend has no route for this query (origin/destination outside the
 *  scored region, backend down, etc.) — caller should silently fall back to
 *  Mapbox-only variants. */
export async function fetchSafePathSafestRoute(
  origin: RoutePoint,
  destination: RoutePoint,
  opts: { extraLambdas?: number[]; signal?: AbortSignal } = {},
): Promise<Route | null> {
  try {
    const routes = await fetchSafePathBikeRoutes(origin, destination, opts);
    return routes[0] ?? null;
  } catch (err) {
    console.warn("SafePath /route unavailable", err);
    return null;
  }
}
