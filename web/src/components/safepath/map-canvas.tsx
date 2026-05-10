"use client";

import { useEffect, useRef } from "react";
import mapboxgl, { type Map as MapboxMap } from "mapbox-gl";
import "mapbox-gl/dist/mapbox-gl.css";

import type { Route, RoutePoint } from "./types";

// Set once on the client. NEXT_PUBLIC_* is inlined at build time.
const TOKEN =
  process.env.NEXT_PUBLIC_MAPBOX_API_KEY ??
  process.env.NEXT_PUBLIC_MAPBOX_TOKEN ??
  "";
if (TOKEN) {
  mapboxgl.accessToken = TOKEN;
}

const ACTIVE_SOURCE = "sp-active-route";
const INACTIVE_SOURCE = "sp-inactive-routes";
const INACTIVE_HIT_LAYER = "sp-inactive-hit";
const INACTIVE_LINE_LAYER = "sp-inactive-line";
const ACTIVE_HALO_LAYER = "sp-active-halo";
const ACTIVE_LINE_LAYER = "sp-active-line";

type MapCanvasProps = {
  activeRoute: Route;
  origin: RoutePoint;
  destination: RoutePoint;
  inactiveRoutes?: Route[];
  onSelectRoute?: (id: string) => void;
  onMapReady?: (map: MapboxMap) => void;
  /** Optional live cyclist position from the Street-View tour. Renders as a
   *  white-haloed dot when set; hides when null. */
  cyclistPosition?: RoutePoint | null;
};

/**
 * Mapbox GL JS map for SafePath. Active route is rendered as colored
 * line segments (safe = green, caution = orange, danger = red) with a
 * black halo for legibility. Inactive routes are dashed grey lines that
 * the rider can tap to switch.
 *
 * https://docs.mapbox.com/mapbox-gl-js/api/
 */
export function MapCanvas({
  activeRoute,
  origin,
  destination,
  inactiveRoutes = [],
  onSelectRoute,
  onMapReady,
  cyclistPosition,
}: MapCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapboxMap | null>(null);
  const styleLoadedRef = useRef(false);
  const originMarkerRef = useRef<mapboxgl.Marker | null>(null);
  const destinationMarkerRef = useRef<mapboxgl.Marker | null>(null);
  const cyclistMarkerRef = useRef<mapboxgl.Marker | null>(null);

  // Hold the latest select handler in a ref so the click listener attached
  // once at map init always sees the current callback.
  const onSelectRef = useRef(onSelectRoute);
  const onMapReadyRef = useRef(onMapReady);

  useEffect(() => {
    onSelectRef.current = onSelectRoute;
  }, [onSelectRoute]);

  useEffect(() => {
    onMapReadyRef.current = onMapReady;
  }, [onMapReady]);

  // ---- Map init (runs once) -----------------------------------------------
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    if (!TOKEN) {
      console.warn(
        "Missing NEXT_PUBLIC_MAPBOX_TOKEN. The map will not render.",
      );
      return;
    }

    const map = new mapboxgl.Map({
      container: containerRef.current,
      // Cycling nav style used before Mapbox Standard + night preset looked darker on route.
      style: "mapbox://styles/mapbox/navigation-night-v1",
      center: [origin.lng, origin.lat],
      zoom: 15.2,
      attributionControl: false,
      pitch: pitchForWidth(containerRef.current.clientWidth),
      bearing: -28,
      antialias: true,
      cooperativeGestures: false,
      projection: { name: "globe" },
    });
    mapRef.current = map;
    onMapReadyRef.current?.(map);

    requestAnimationFrame(() => {
      map.resize();
    });

    map.addControl(
      new mapboxgl.AttributionControl({ compact: true }),
      "top-right",
    );
    map.addControl(
      new mapboxgl.NavigationControl({
        showCompass: true,
        showZoom: false,
        visualizePitch: true,
      }),
      "top-right",
    );

    map.on("error", (event) => {
      console.error("Mapbox GL error", event.error);
    });

    map.on("load", () => {
      map.resize();
      enable3DScene(map);

      // Sources start empty; the second effect sets data on every change.
      map.addSource(INACTIVE_SOURCE, {
        type: "geojson",
        data: emptyFC(),
      });
      map.addSource(ACTIVE_SOURCE, {
        type: "geojson",
        data: emptyFC(),
      });

      // Wide invisible hit-line under the visible inactive line so taps
      // are forgiving on touchscreens.
      map.addLayer({
        id: INACTIVE_HIT_LAYER,
        type: "line",
        source: INACTIVE_SOURCE,
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": "#000", "line-opacity": 0, "line-width": 26 },
      });

      map.addLayer({
        id: INACTIVE_LINE_LAYER,
        type: "line",
        source: INACTIVE_SOURCE,
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": "#7a7a7a",
          "line-width": 4,
          "line-opacity": 0.7,
          "line-dasharray": [2, 2],
        },
      });

      map.addLayer({
        id: ACTIVE_HALO_LAYER,
        type: "line",
        source: ACTIVE_SOURCE,
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": "#000", "line-opacity": 0.9, "line-width": 9 },
      });

      map.addLayer({
        id: ACTIVE_LINE_LAYER,
        type: "line",
        source: ACTIVE_SOURCE,
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-width": 5,
          "line-color": [
            "match",
            ["get", "level"],
            "safe",
            "#06C167",
            "caution",
            "#F5A623",
            "danger",
            "#E83B3B",
            "#06C167",
          ],
        },
      });

      map.on("click", INACTIVE_HIT_LAYER, (e) => {
        const id = e.features?.[0]?.properties?.id;
        if (typeof id === "string") onSelectRef.current?.(id);
      });
      map.on("mouseenter", INACTIVE_HIT_LAYER, () => {
        map.getCanvas().style.cursor = "pointer";
      });
      map.on("mouseleave", INACTIVE_HIT_LAYER, () => {
        map.getCanvas().style.cursor = "";
      });

      // Origin pin pulsing white dot.
      const originEl = document.createElement("div");
      originEl.className = "sp-marker-origin";
      originMarkerRef.current = new mapboxgl.Marker({
        element: originEl,
        anchor: "center",
      })
        .setLngLat([origin.lng, origin.lat])
        .addTo(map);

      // Destination pin black dot ringed in white (target style).
      const destEl = document.createElement("div");
      destEl.className = "sp-marker-dest";
      destinationMarkerRef.current = new mapboxgl.Marker({
        element: destEl,
        anchor: "center",
      })
        .setLngLat([destination.lng, destination.lat])
        .addTo(map);

      styleLoadedRef.current = true;
    });

    return () => {
      styleLoadedRef.current = false;
      map.remove();
      mapRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- Push route data + fit bounds whenever they change ------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;

    const apply = () => {
      const active = map.getSource(ACTIVE_SOURCE) as
        | mapboxgl.GeoJSONSource
        | undefined;
      const inactive = map.getSource(INACTIVE_SOURCE) as
        | mapboxgl.GeoJSONSource
        | undefined;
      active?.setData(activeFC(activeRoute));
      inactive?.setData(inactiveFC(inactiveRoutes));

      originMarkerRef.current?.setLngLat([origin.lng, origin.lat]);
      destinationMarkerRef.current?.setLngLat([
        destination.lng,
        destination.lat,
      ]);

      const b = boundsFor([activeRoute, ...inactiveRoutes], origin, destination);
      const sw = b.getSouthWest();
      const ne = b.getNorthEast();
      if (
        Number.isFinite(sw?.lng) &&
        Number.isFinite(sw?.lat) &&
        Number.isFinite(ne?.lng) &&
        Number.isFinite(ne?.lat)
      ) {
        map.resize();
        const { clientWidth, clientHeight } = map.getContainer();
        if (clientWidth > 0 && clientHeight > 0) {
          const isBottomSheetLayout = clientWidth < 1100;
          const padding = {
            top: isBottomSheetLayout ? 24 : Math.min(64, clientHeight * 0.12),
            right: Math.min(56, clientWidth * 0.12),
            bottom: isBottomSheetLayout
              ? Math.min(clientHeight * 0.54, clientHeight - 140)
              : Math.min(80, clientHeight * 0.14),
            left: Math.min(56, clientWidth * 0.12),
          };

          map.fitBounds(b, {
            padding,
            duration: 600,
            maxZoom: 15.5,
            pitch: pitchForWidth(clientWidth),
            bearing: -28,
          });
        }
      }
    };

    if (styleLoadedRef.current) {
      apply();
    } else {
      map.once("load", apply);
    }
  }, [activeRoute, inactiveRoutes, origin, destination]);

  // Cyclist marker — created/moved when a position is supplied; removed when
  // the position drops back to null (tour stopped).
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;

    if (!cyclistPosition) {
      cyclistMarkerRef.current?.remove();
      cyclistMarkerRef.current = null;
      return;
    }

    const lngLat: [number, number] = [cyclistPosition.lng, cyclistPosition.lat];
    if (!cyclistMarkerRef.current) {
      const el = document.createElement("div");
      el.className = "sp-marker-cyclist";
      // Lucide "bike" icon, inlined so it lives in the DOM marker without
      // needing React. currentColor lets us pick the stroke via CSS.
      el.innerHTML =
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<circle cx="18.5" cy="17.5" r="3.5"/>' +
        '<circle cx="5.5" cy="17.5" r="3.5"/>' +
        '<circle cx="15" cy="5" r="1"/>' +
        '<path d="M12 17.5V14l-3-3 4-3 2 3h2"/>' +
        "</svg>";
      cyclistMarkerRef.current = new mapboxgl.Marker({ element: el, anchor: "center" })
        .setLngLat(lngLat)
        .addTo(map);
    } else {
      cyclistMarkerRef.current.setLngLat(lngLat);
    }
  }, [cyclistPosition]);

  return (
    <div className="safepath-map-shell absolute inset-0 overflow-hidden bg-black">
      <div ref={containerRef} className="safepath-mapbox-live h-full w-full" />
    </div>
  );
}

/* ----------------------------- helpers ---------------------------------- */

function emptyFC(): GeoJSON.FeatureCollection {
  return { type: "FeatureCollection", features: [] };
}

function pitchForWidth(width: number) {
  return width < 1100 ? 0 : 58;
}

function enable3DScene(map: MapboxMap) {
  if (!map.getSource("mapbox-dem")) {
    map.addSource("mapbox-dem", {
      type: "raster-dem",
      url: "mapbox://mapbox.mapbox-terrain-dem-v1",
      tileSize: 512,
      maxzoom: 14,
    });
    map.setTerrain({ source: "mapbox-dem", exaggeration: 1.08 });
  }

  const layers = map.getStyle().layers ?? [];
  const labelLayer = layers.find((layer) => {
    const layout = (layer as { layout?: Record<string, unknown> }).layout;
    return layer.type === "symbol" && Boolean(layout?.["text-field"]);
  });
  if (!map.getLayer("sp-3d-buildings")) {
    map.addLayer(
      {
        id: "sp-3d-buildings",
        source: "composite",
        "source-layer": "building",
        filter: ["==", "extrude", "true"],
        type: "fill-extrusion",
        minzoom: 14,
        paint: {
          "fill-extrusion-color": "#252525",
          "fill-extrusion-height": [
            "interpolate",
            ["linear"],
            ["zoom"],
            14,
            0,
            16,
            ["coalesce", ["get", "height"], 14],
          ],
          "fill-extrusion-base": [
            "interpolate",
            ["linear"],
            ["zoom"],
            14,
            0,
            16,
            ["coalesce", ["get", "min_height"], 0],
          ],
          "fill-extrusion-opacity": 0.72,
        },
      },
      labelLayer?.id,
    );
  }
}

function activeFC(route: Route): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: route.segments
      .filter((seg) => seg.points.length >= 2)
      .map((seg) => ({
        type: "Feature",
        properties: { level: seg.level, segId: seg.id, routeId: route.id },
        geometry: {
          type: "LineString",
          coordinates: seg.points.map((p) => [p.lng, p.lat]),
        },
      })),
  };
}

function inactiveFC(routes: Route[]): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: routes.flatMap((r) => {
      const coordinates = r.segments.flatMap((s) =>
        s.points.map((p) => [p.lng, p.lat] as [number, number]),
      );
      if (coordinates.length < 2) return [];
      return [
        {
          type: "Feature" as const,
          properties: { id: r.id, name: r.name },
          geometry: {
            type: "LineString" as const,
            coordinates,
          },
        },
      ];
    }),
  };
}

function boundsFor(
  routes: Route[],
  origin: RoutePoint,
  destination: RoutePoint,
): mapboxgl.LngLatBounds {
  // Always extend; never pass a sw/ne pair to the constructor as that
  // requires the caller to pre-sort lng/lat into south-west / north-east
  // corners. `extend` does the right thing regardless of order.
  const b = new mapboxgl.LngLatBounds();
  b.extend([origin.lng, origin.lat] as [number, number]);
  b.extend([destination.lng, destination.lat] as [number, number]);
  for (const r of routes) {
    for (const s of r.segments) {
      for (const p of s.points) {
        b.extend([p.lng, p.lat] as [number, number]);
      }
    }
  }
  return b;
}
