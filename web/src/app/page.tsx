"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
} from "react";
import type { Map as MapboxMap } from "mapbox-gl";
import { toast } from "sonner";

import { MapCanvas } from "@/components/safepath/map-canvas";
import { RouteReasoningPanel } from "@/components/safepath/route-reasoning-panel";
import { SidePanel } from "@/components/safepath/side-panel";
import { useStreetViewTour } from "@/components/safepath/street-view-tour";
import { DESTINATION, ORIGIN, ROUTES } from "@/components/safepath/mock-data";
import { fetchMapboxBikeRoutes } from "@/components/safepath/mapbox-routes";
import { fetchSafePathBikeRoutes } from "@/components/safepath/safepath-routes";
import { enrichRoutesWithSafetyData } from "@/components/safepath/safepath-enrichment";

// Toggle Mapbox-Directions variants alongside our backend's safety-Dijkstra'd
// routes. Set NEXT_PUBLIC_ENABLE_MAPBOX_ROUTES=false in .env.local to drop
// Mapbox entirely (backend routing only). Defaults to enabled.
const ENABLE_MAPBOX_ROUTES = (() => {
  const v = (process.env.NEXT_PUBLIC_ENABLE_MAPBOX_ROUTES ?? "true").toLowerCase();
  return v !== "false" && v !== "0" && v !== "no";
})();

// Extra safety-lambda values to ask the backend for. Each one runs an
// additional Dijkstra at that λ, producing a variant that's more willing to
// detour around low-score edges. Higher λ = safer but longer.
//   λ ≈ 0.5 → matches env's default SAFETY_LAMBDA (the existing "safe" route)
//   λ ≈ 1.5 → up to ~2.5x detour to dodge a score-1 edge ("extra-safe")
//   λ ≈ 3.0 → up to ~4x detour ("max-safe")
//   λ ≈ 6.0 → up to ~7x detour ("ultra-safe", essentially-any-detour-goes)
// Empty list (or "off") disables — only the default safe + fast routes come back.
const EXTRA_SAFETY_LAMBDAS = (() => {
  const raw = (process.env.NEXT_PUBLIC_EXTRA_SAFETY_LAMBDAS ?? "1.5,3.0").trim();
  if (!raw || raw.toLowerCase() === "off" || raw === "0") return [];
  return raw
    .split(",")
    .map((s) => Number.parseFloat(s.trim()))
    .filter((n) => Number.isFinite(n) && n > 0);
})();
import type { Route, RoutePoint } from "@/components/safepath/types";

const subscribeToClientHydration = () => () => {};
const getClientSnapshot = () => true;
const getServerSnapshot = () => false;

export default function Home() {
  const mounted = useSyncExternalStore(
    subscribeToClientHydration,
    getClientSnapshot,
    getServerSnapshot,
  );
  const [origin, setOrigin] = useState("8th & F, Davis");
  const [destination, setDestination] = useState("965 Olive Drive, Davis");
  const [originPoint, setOriginPoint] = useState<RoutePoint>(ORIGIN);
  const [destinationPoint, setDestinationPoint] =
    useState<RoutePoint>(DESTINATION);
  // Tracks the name only after a result is picked — not while typing.
  const [confirmedDestName, setConfirmedDestName] = useState("965 Olive Drive, Davis");
  const [map, setMap] = useState<MapboxMap | null>(null);
  const [routes, setRoutes] = useState<Route[]>(ROUTES);
  const [selectedRouteId, setSelectedRouteId] = useState(ROUTES[0].id);
  const [cautiousMode, setCautiousMode] = useState(true);
  const [sheetExpanded, setSheetExpanded] = useState(false);
  const [navigationActive, setNavigationActive] = useState(false);
  const [isLoadingRoutes, setIsLoadingRoutes] = useState(true);
  const [cyclistPosition, setCyclistPosition] = useState<RoutePoint | null>(null);

  useEffect(() => {
    if (!mounted) return;
    let cancelled = false;
    setIsLoadingRoutes(true);

    // Routing strategy: pull from our backend + (optionally) Mapbox, then
    // enrich + sort by score.
    //   * fetchSafePathBikeRoutes (POST /route): our safety-Dijkstra'd safest
    //     path + distance-Dijkstra'd fastest path (1 or 2 routes).
    //   * fetchMapboxBikeRoutes: 2-4 cycling variants. Toggled by the
    //     NEXT_PUBLIC_ENABLE_MAPBOX_ROUTES env var so the demo can compare
    //     "backend only" vs "stacked" without code changes.
    //   * enrichRoutesWithSafetyData: applies per-edge scoring and the
    //     unprotected-intersection penalty uniformly to every variant so
    //     the sort is fair across sources.
    // Either source failing doesn't sink the page — we keep whatever loaded.
    const loadRoutes = async (): Promise<Route[]> => {
      const tasks: Promise<Route[]>[] = [
        fetchSafePathBikeRoutes(originPoint, destinationPoint, {
          extraLambdas: EXTRA_SAFETY_LAMBDAS,
        }),
      ];
      if (ENABLE_MAPBOX_ROUTES) {
        tasks.push(fetchMapboxBikeRoutes(originPoint, destinationPoint));
      }
      const settled = await Promise.allSettled(tasks);
      const [backendResult, mapboxResult] = settled;
      const backendRoutes =
        backendResult.status === "fulfilled" ? backendResult.value : [];
      const mapboxRoutes =
        mapboxResult && mapboxResult.status === "fulfilled"
          ? mapboxResult.value
          : [];
      if (backendResult.status === "rejected") {
        console.warn("SafePath /route unavailable", backendResult.reason);
      }
      if (mapboxResult && mapboxResult.status === "rejected") {
        console.warn("Mapbox routes unavailable", mapboxResult.reason);
      }
      const combined = [...backendRoutes, ...mapboxRoutes];
      if (combined.length === 0) {
        throw (
          backendResult.status === "rejected"
            ? backendResult.reason
            : mapboxResult && mapboxResult.status === "rejected"
            ? mapboxResult.reason
            : new Error("No routes available")
        );
      }
      try {
        return await enrichRoutesWithSafetyData(combined);
      } catch (err) {
        console.warn("SafePath enrichment unavailable, using base data", err);
        return combined;
      }
    };

    loadRoutes()
      .then((nextRoutes) => {
        if (cancelled) return;
        const sorted = [...nextRoutes].sort((a, b) => b.score - a.score);

        // Assign names by role, not by position
        const fastestId = [...sorted].sort((a, b) => a.durationMin - b.durationMin)[0]!.id;
        const safestId = sorted[0]!.id;
        const safestIsFastest = safestId === fastestId;
        let altIndex = 1;
        const renamed = sorted.map((r) => {
          if (r.id === safestId) return { ...r, name: safestIsFastest ? "Safest & Fastest" : "Safest" };
          if (r.id === fastestId) return { ...r, name: "Fastest" };
          return { ...r, name: `Alternative ${altIndex++}` };
        });

        setRoutes(renamed);
        setNavigationActive(false);
        setSelectedRouteId(renamed[0]!.id);
        setIsLoadingRoutes(false);
      })
      .catch((error) => {
        if (cancelled) return;
        console.error(error);
        setIsLoadingRoutes(false);
        toast("Routing unavailable", {
          description:
            error instanceof Error
              ? error.message
              : "Keeping the current route on screen.",
        });
      });

    return () => {
      cancelled = true;
    };
  }, [mounted, originPoint, destinationPoint]);

  const visibleRoutes = useMemo(() => routes, [routes]);

  const activeRoute = useMemo(
    () =>
      visibleRoutes.find((r) => r.id === selectedRouteId) ?? visibleRoutes[0],
    [visibleRoutes, selectedRouteId],
  );
  const inactiveRoutes = useMemo(
    () => visibleRoutes.filter((r) => r.id !== activeRoute.id),
    [visibleRoutes, activeRoute.id],
  );

  const select = (id: string) => {
    setNavigationActive(false);
    setCyclistPosition(null);
    setSelectedRouteId(id);
  };

  // Street-View "agent walking" tour. We hoist the hook here (rather than
  // owning it inside the panel) so the cyclist marker can render on the map
  // canvas while the panel re-uses the same state for the inline UI.
  const tour = useStreetViewTour({
    origin: originPoint,
    destination: destinationPoint,
    weight: /fast/i.test(activeRoute.id) ? "cost_fast" : "cost_safe",
    onCyclistPositionChange: setCyclistPosition,
    onAlert: (a) => {
      toast(a.title, { description: a.summary });
    },
  });

  const handleOriginSelect = useCallback(
    ({ label, point }: { label: string; point: RoutePoint }) => {
      setNavigationActive(false);
      setOrigin(label);
      setOriginPoint(point);
      map?.flyTo({ center: [point.lng, point.lat], zoom: 14.5 });
    },
    [map],
  );

  const handleDestinationSelect = useCallback(
    ({ label, point }: { label: string; point: RoutePoint }) => {
      setNavigationActive(false);
      setDestination(label);
      setConfirmedDestName(label);
      setDestinationPoint(point);
      map?.flyTo({ center: [point.lng, point.lat], zoom: 14.5 });
    },
    [map],
  );

  if (!mounted) {
    return <main className="relative h-dvh w-svw overflow-hidden bg-black" />;
  }

  return (
    <main className="relative h-dvh w-svw overflow-hidden bg-black">
      {/* Map: full bleed on mobile; on desktop, shifted right by the 320px panel. */}
      <div className="absolute inset-0 lg:left-[320px]">
        <MapCanvas
          activeRoute={activeRoute}
          origin={originPoint}
          destination={destinationPoint}
          inactiveRoutes={inactiveRoutes}
          onSelectRoute={select}
          onMapReady={setMap}
          cyclistPosition={cyclistPosition}
        />
      </div>

      <SidePanel
        routes={visibleRoutes}
        selectedRouteId={activeRoute.id}
        onSelectRoute={select}
        origin={origin}
        destination={destination}
        onOriginChange={setOrigin}
        onDestinationChange={setDestination}
        onOriginSelect={handleOriginSelect}
        onDestinationSelect={handleDestinationSelect}
        cautiousMode={cautiousMode}
        onCautiousModeChange={(next) => {
          if (next) {
            const safest = [...routes].sort((a, b) => b.score - a.score)[0];
            setNavigationActive(false);
            setSelectedRouteId(safest.id);
          }
          setCautiousMode(next);
        }}
        onStartRoute={() => setNavigationActive(true)}
        expanded={sheetExpanded}
        onToggleExpanded={() => setSheetExpanded((v) => !v)}
        navigationActive={navigationActive}
        navigationCues={activeRoute.navigationCues ?? []}
        onExitNavigation={() => setNavigationActive(false)}
        isLoading={isLoadingRoutes}
        destinationName={confirmedDestName}
        destinationPoint={destinationPoint}
      />

      <RouteReasoningPanel
        route={activeRoute}
        routes={visibleRoutes}
        showDirections={navigationActive}
        onBackFromDirections={() => setNavigationActive(false)}
        onStartRoute={() => setNavigationActive(true)}
        destinationPoint={destinationPoint}
        destinationName={confirmedDestName}
        isLoading={isLoadingRoutes}
        tour={tour}
      />
    </main>
  );
}
