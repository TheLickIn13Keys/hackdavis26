"""Gemini safety scorer with spatial hazard localization.

Default model is `gemini-3-flash-preview` (override via GEMINI_MODEL env). Each
hazard returned by the model includes either a bounding box or a point on
Gemini's normalized [0, 1000] coordinate grid, plus the index of the image it
appears in. Frontends can scale these to actual image dimensions to overlay
boxes on the Street View thumbnail.

Two scoring modes:
  - score_segment_images: edge-segment safety (forward + side images)
  - score_intersection_images: intersection traversal safety (4 cardinal views)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from google import genai
from google.genai import types
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings

MODEL = settings.gemini_model

# --- Localization protocol -----------------------------------------------------
# Gemini returns object localizations on a [0, 1000] grid normalized to image
# dimensions, with bbox in [ymin, xmin, ymax, xmax] order and points as [y, x].
# Frontends descale by:  px_x = norm_x / 1000 * image_width_px
# We surface the same convention in our schema so the model's output is reusable
# without translation.
LOCALIZATION_NOTE = (
    "When you identify a hazard, localize it precisely:\n"
    " - bbox: [ymin, xmin, ymax, xmax] on a 0–1000 grid normalized to the image, "
    "OR\n"
    " - point: [y, x] on the same 0–1000 grid for small/single-point features.\n"
    "Set image_index to the 0-based index of the image where the hazard appears "
    "(0 = first image given to you). If a hazard spans the whole scene "
    "(e.g. 'fast_traffic'), use image_index of the most relevant image and a "
    "bbox covering the relevant region. Use known hazard type tags when "
    "applicable: door_zone, parked_cars, broken_pavement, narrow_lane, "
    "fast_traffic, blind_corner, no_bike_lane, debris, drainage_grate, "
    "construction, slip_lane, missing_signal, obscured_sightline."
)

SEGMENT_SYSTEM_PROMPT = f"""You are a cycling safety analyst. You will be shown one or more Google \
Street View images captured at a single point along a road segment, from different headings \
(typically a forward view and a side view of the road shoulder). Treat them as a panorama of the \
same location and produce ONE combined safety score for a cyclist riding this segment.

You may also receive OSM tags as ground truth for the road's classification (highway type, bike \
infrastructure, lane count, speed limit, surface). When provided, TAKE THESE AS AUTHORITATIVE — \
don't second-guess them from the photos. They tell you WHAT the road is. Spend your reasoning on \
WHAT THE PHOTOS SHOW THAT TAGS CAN'T: pavement condition, parked-car density and door-zone risk, \
sight lines, design adequacy of any bike infrastructure, blocked lanes, debris, construction.

Consider, in roughly this order:
- Bike infrastructure: presence and protection of bike lanes (none / painted / buffered / protected),
  with extra weight on condition and design adequacy when OSM tags already confirm presence.
  ABSENCE of any bike lane on a road wider than two lanes is a major negative — riders share
  general traffic lanes with cars.
- Traffic stress: number of lanes, road width, apparent speed limit, traffic volume cues.
  Wide pavement (≥ ~30 ft / 4 car-widths edge-to-edge) without lane markings or a bike lane
  invites speeding even on "residential" streets — do NOT treat width as comfort.
- Door zone: parked cars adjacent to the cycling path. Where there is no bike lane AND parked
  cars line the curb, riders are forced into the door zone or out into a moving traffic lane —
  this alone caps the score at ~5 regardless of how quiet the street looks.
- Surface: potholes, cracking, debris, gravel, drainage grates
- Sight lines: blind curves, parked vehicles obscuring driveways
- General feel: residential street vs arterial vs industrial frontage. A "residential" highway
  tag is not a safety guarantee — many residential streets in US suburbs are wide, fast, and
  have no bike infrastructure.

Score from 1 (extremely dangerous) to 10 (separated bike path, or a TRULY quiet neighborhood \
street: narrow pavement, traffic calming or visibly low traffic, no door-zone parking conflict). \
Calibrate:
- 9–10 reserved for protected/separated paths, greenways, or genuinely calm residential streets
  with little/no parked-car door-zone conflict.
- 7–8 = streets with a bike lane in good condition, OR a quiet residential street that has
  parked cars but enough width for a rider to keep clear of the door zone.
- ~5 = a typical urban arterial with a painted bike lane, OR a wide residential road with no
  bike lane and door-zone parking on both sides (mixed traffic + door risk).
- 3–4 = multi-lane road with no bike infrastructure, or a residential street with heavy parked
  cars and visible cut-through traffic.
- 1–2 = high-speed arterial, highway shoulder, or any road where a reasonable adult would
  refuse to ride.

{LOCALIZATION_NOTE}

If all images are indoors, blank, or otherwise unusable, return score=null with a single hazard \
of type "unusable_image".
"""

INTERSECTION_SYSTEM_PROMPT = f"""You are a cycling safety analyst evaluating a road INTERSECTION. \
You will be shown 4 Google Street View images (cardinal directions: north, east, south, west) \
captured AT the intersection. Score how dangerous this intersection is for a cyclist to traverse \
straight through, turn at, or wait at.

You may also receive OSM ground truth: the intersection's geometric type (T / Y / four_way / \
complex) and its OSM-tagged control (traffic_signals / stop / give_way / mini_roundabout / \
uncontrolled). When provided, TAKE THESE AS AUTHORITATIVE for what the intersection IS. Spend \
your reasoning on what photos reveal that tags can't: signal timing for cyclists, dedicated bike \
phases, paint condition, presence of damaged signals, queueing behaviour from visible traffic.

Note: even if OSM says 'traffic_signals', the intersection can still be dangerous (e.g. high \
speed approach, multi-lane slip lanes, no bike phase). Do not let the tag inflate the score.

Consider:
- Number and width of crossing lanes; presence of slip lanes (right-turn lanes that bypass the signal)
- Signal type: traffic light vs stop signs vs uncontrolled (defer to OSM control when given)
- Bike-specific infrastructure: bike box, advance stop line, dedicated signal, marked crossings
- Sight lines: parked cars, vegetation, or buildings hiding cross-traffic
- Turn-conflict potential: high-speed right-turning cars crossing bike paths
- Pedestrian density (more peds → typically slower, safer for bikes)
- Visibility / lighting cues for low-light safety

Score from 1 (avoid at all costs) to 10 (low-stress: quiet 4-way stop or bike-prioritized \
intersection). Calibrate 5 = signalized arterial with painted bike lane crossings.

{LOCALIZATION_NOTE}

If images are unusable, return score=null with a single hazard of type "unusable_image".
"""

# A hazard is either a bbox (region) or point (small feature). We allow both
# fields nullable and rely on the prompt to pick the right one.
HAZARD_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string"},
        "severity": {"type": "integer", "minimum": 1, "maximum": 5},
        "image_index": {"type": "integer", "minimum": 0},
        "bbox": {
            "type": "array",
            "items": {"type": "number"},
            "minItems": 4,
            "maxItems": 4,
            "nullable": True,
            "description": "[ymin, xmin, ymax, xmax] on a 0–1000 grid normalized to the image",
        },
        "point": {
            "type": "array",
            "items": {"type": "number"},
            "minItems": 2,
            "maxItems": 2,
            "nullable": True,
            "description": "[y, x] on a 0–1000 grid normalized to the image",
        },
        "note": {"type": "string", "nullable": True},
    },
    "required": ["type", "severity", "image_index"],
}

SEGMENT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 1, "maximum": 10, "nullable": True},
        "infrastructure": {
            "type": "string",
            "enum": ["none", "painted_lane", "buffered_lane", "protected_lane",
                     "shared_path", "off_street_path", "unknown"],
        },
        "hazards": {"type": "array", "items": HAZARD_SCHEMA},
        "reasons": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["score", "infrastructure", "hazards", "reasons"],
}

INTERSECTION_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 1, "maximum": 10, "nullable": True},
        "control": {
            "type": "string",
            "enum": ["uncontrolled", "yield", "stop", "all_way_stop",
                     "signal", "signal_with_bike_phase", "roundabout", "unknown"],
        },
        "hazards": {"type": "array", "items": HAZARD_SCHEMA},
        "reasons": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["score", "control", "hazards", "reasons"],
}


@dataclass
class SegmentAssessment:
    score: int | None
    infrastructure: str
    hazards: list[dict]
    reasons: list[str]
    raw: dict


@dataclass
class IntersectionAssessment:
    score: int | None
    control: str
    hazards: list[dict]
    reasons: list[str]
    raw: dict


def make_client() -> genai.Client:
    # Explicit per-request timeout so a hung connection auto-fails fast and
    # tenacity's retry kicks in. Without this the SDK can sit on a stalled
    # request for ~10 minutes (its internal default), bringing the whole
    # threadpool to a halt.
    return genai.Client(
        api_key=settings.gemini_api_key,
        http_options=types.HttpOptions(timeout=60_000),  # milliseconds
    )


def _image_parts(image_paths: list[Path]) -> list:
    return [types.Part.from_bytes(data=p.read_bytes(), mime_type="image/jpeg") for p in image_paths]


def _format_segment_context(ctx: dict | None) -> str:
    """Render edge OSM tags as a compact 'key=val' line for inclusion in the prompt."""
    if not ctx:
        return ""
    keys = ("highway", "cycleway", "cycleway:left", "cycleway:right", "cycleway:both",
            "bicycle", "lanes", "maxspeed", "surface", "oneway")
    parts = [f"{k}={ctx[k]}" for k in keys if ctx.get(k) not in (None, "")]
    return ", ".join(parts)


def _format_intersection_context(ctx: dict | None) -> str:
    if not ctx:
        return ""
    parts = []
    itype = ctx.get("intersection_type")
    if itype:
        type_label = {"t": "T-intersection", "y": "Y-intersection",
                      "four_way": "four-way", "complex": "complex (5+ legs)"}.get(itype, itype)
        parts.append(f"type={type_label}")
    osm_ctrl = ctx.get("osm_control")
    if osm_ctrl:
        parts.append(f"control={osm_ctrl}")
    elif ctx.get("osm_control") is None and "osm_control" in ctx:
        parts.append("control=uncontrolled")
    deg = ctx.get("degree")
    if deg is not None:
        parts.append(f"degree={deg}")
    return ", ".join(parts)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
def score_segment_images(
    client: genai.Client,
    image_paths: list[Path],
    osm_context: dict | None = None,
) -> SegmentAssessment:
    if not image_paths:
        raise ValueError("score_segment_images requires at least one image")
    parts = _image_parts(image_paths)
    user_msg = (
        f"Score this road segment for cyclist safety per the system instructions. "
        f"({len(image_paths)} image(s); image_index 0..{len(image_paths) - 1})"
    )
    ctx_str = _format_segment_context(osm_context)
    if ctx_str:
        user_msg += (
            f"\n\nOSM ground truth for this road: {ctx_str}.\n"
            f"Take these as authoritative. Focus on quality assessment from the photos."
        )
    response = client.models.generate_content(
        model=MODEL,
        contents=[*parts, user_msg],
        config=types.GenerateContentConfig(
            system_instruction=SEGMENT_SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=SEGMENT_RESPONSE_SCHEMA,
            temperature=0.2,
        ),
    )
    parsed = json.loads(response.text or "{}")
    return SegmentAssessment(
        score=parsed.get("score"),
        infrastructure=parsed.get("infrastructure", "unknown"),
        hazards=parsed.get("hazards", []) or [],
        reasons=parsed.get("reasons", []) or [],
        raw=parsed,
    )


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
def score_intersection_images(
    client: genai.Client,
    image_paths: list[Path],
    osm_context: dict | None = None,
) -> IntersectionAssessment:
    if not image_paths:
        raise ValueError("score_intersection_images requires at least one image")
    parts = _image_parts(image_paths)
    user_msg = (
        f"Score this intersection for cyclist safety per the system instructions. "
        f"({len(image_paths)} cardinal-direction image(s); image_index 0..{len(image_paths) - 1})"
    )
    ctx_str = _format_intersection_context(osm_context)
    if ctx_str:
        user_msg += (
            f"\n\nOSM ground truth for this intersection: {ctx_str}.\n"
            f"Take these as authoritative for shape and control. Focus on quality "
            f"(bike phase, slip lanes, sight lines, paint condition)."
        )
    response = client.models.generate_content(
        model=MODEL,
        contents=[*parts, user_msg],
        config=types.GenerateContentConfig(
            system_instruction=INTERSECTION_SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=INTERSECTION_RESPONSE_SCHEMA,
            temperature=0.2,
        ),
    )
    parsed = json.loads(response.text or "{}")
    return IntersectionAssessment(
        score=parsed.get("score"),
        control=parsed.get("control", "unknown"),
        hazards=parsed.get("hazards", []) or [],
        reasons=parsed.get("reasons", []) or [],
        raw=parsed,
    )
