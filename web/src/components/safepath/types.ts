export type SafetyLevel = "safe" | "caution" | "danger";

export type RoutePoint = { lng: number; lat: number };

export type RouteSegment = {
  id: string;
  level: SafetyLevel;
  points: RoutePoint[];
  edgeIds?: string[];
  /** Legacy single-line blurbs (mock routes & fallback splits) */
  reason?: string;
  /** One sanitized line per Directions step merged here (accordion) */
  stressNotes?: string[];
};

/** Turn-by-turn from Mapbox Directions (cycling steps) — shown after “Start route”. */
export type NavigationCue = {
  id: string;
  instruction: string;
  distanceM: number;
  durationSec: number;
};

export type RouteIntel = {
  mapbox: {
    lines: string[];
    scorePenalty: number;
    peakPostedMph: number | null;
    motorwayTouches: number;
    tunnelTouches: number;
    congestion?: {
      lowPct: number;
      moderatePct: number;
      heavyPct: number;
      severePct: number;
      available: boolean;
    };
  };
  conditions?: {
    lines: string[];
    attribution: string;
    items?: Array<{
      hint: string;
      safety: "safe" | "caution" | "danger";
      detail: string;
    }>;
  };
  crime?: {
    totalIncidents: number;
    violentCount: number;
    topCategories: string[];
    violentCategories: string[];
    scorePenalty: number;
    lines: string[];
    level: "safe" | "caution" | "danger";
    attribution: string;
    outsideCoverage: boolean;
    city?: string;
  };
};

export type Route = {
  id: string;
  name: string;
  subtitle: string;
  durationMin: number;
  distanceMi: number;
  score: number; // 0-100
  segments: RouteSegment[];
  navigationCues?: NavigationCue[];
  intel?: RouteIntel;
};


export type StreetAnalysis = {
  id: string;
  street: string;
  imageHue: number;
  level: SafetyLevel;
  tags: { label: string; level: SafetyLevel }[];
  description: string;
};
