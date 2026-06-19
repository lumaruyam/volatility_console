/**
 * VolSurface3D — hardware-accelerated WebGL surface via React Three Fiber.
 *
 * Lazy-imported by DataOverview (React.lazy) so Three.js is never bundled
 * into the SSR pass and never touches server-side Node globals.
 *
 * Data contract: accepts the same `VolSurface` shape used throughout
 * DataOverview — strikes[], maturities[], implied_vols[mi][si].
 */

import { useMemo, useEffect, useRef } from "react";
import { Canvas } from "@react-three/fiber";
import { OrbitControls, Text, Billboard } from "@react-three/drei";
import * as THREE from "three";

// ─── Type ─────────────────────────────────────────────────────────────────────

type VolSurface = {
  strikes: number[];
  maturities: string[];
  implied_vols: number[][];
  smile_slice_30d: {
    strikes: number[];
    call_ivs: number[];
    put_ivs: number[];
    cal_arb: string;
    bfly_arb: string;
  };
  calibration: { rmse: number; status: string; model: string };
};

// ─── Colour gradient ──────────────────────────────────────────────────────────

type GradStop = readonly [t: number, r: number, g: number, b: number];

const GRAD: GradStop[] = [
  [0.00, 0.059, 0.090, 0.271],   // slate-900  #0f1745
  [0.28, 0.118, 0.251, 0.686],   // blue-700   #1e40af
  [0.55, 0.016, 0.518, 0.698],   // sky-600    #0284c7
  [0.78, 0.082, 0.714, 0.831],   // cyan-500   #15b6d4
  [1.00, 0.398, 0.910, 0.976],   // cyan-200   #65e8f9
];

function tToColor(t: number): THREE.Color {
  const ct = Math.max(0, Math.min(1, t));
  let lo = GRAD[0], hi = GRAD[GRAD.length - 1];
  for (let i = 0; i < GRAD.length - 1; i++) {
    if (ct >= GRAD[i][0] && ct <= GRAD[i + 1][0]) { lo = GRAD[i]; hi = GRAD[i + 1]; break; }
  }
  const a = (ct - lo[0]) / (hi[0] - lo[0] || 1);
  return new THREE.Color(
    lo[1] + a * (hi[1] - lo[1]),
    lo[2] + a * (hi[2] - lo[2]),
    lo[3] + a * (hi[3] - lo[3]),
  );
}

// ─── Bicubic (Catmull-Rom) upsampling ─────────────────────────────────────────

const UPSAMPLE_M = 64;
const UPSAMPLE_S = 96;

function catmullRom(p0: number, p1: number, p2: number, p3: number, t: number): number {
  return 0.5 * (
    2 * p1 +
    (-p0 + p2) * t +
    (2 * p0 - 5 * p1 + 4 * p2 - p3) * t * t +
    (-p0 + 3 * p1 - 3 * p2 + p3) * t * t * t
  );
}

function bicubicUpsample(grid: number[][], outM: number, outS: number): number[][] {
  const nM = grid.length;
  const nS = grid[0].length;
  const g = (mi: number, si: number) =>
    grid[Math.max(0, Math.min(nM - 1, mi))][Math.max(0, Math.min(nS - 1, si))];

  const out: number[][] = [];
  for (let i = 0; i < outM; i++) {
    const fi  = (i / (outM - 1)) * (nM - 1);
    const mi1 = Math.floor(fi);
    const ty  = fi - mi1;
    const row: number[] = [];
    for (let j = 0; j < outS; j++) {
      const fj  = (j / (outS - 1)) * (nS - 1);
      const si1 = Math.floor(fj);
      const tx  = fj - si1;
      // interpolate along S for each of 4 surrounding M rows, then along M
      const cols = [-1, 0, 1, 2].map(dm =>
        catmullRom(
          g(mi1 + dm, si1 - 1), g(mi1 + dm, si1),
          g(mi1 + dm, si1 + 1), g(mi1 + dm, si1 + 2),
          tx,
        ),
      );
      row.push(catmullRom(cols[0], cols[1], cols[2], cols[3], ty));
    }
    out.push(row);
  }
  return out;
}

// ─── Surface mesh (upsampled + smooth normals) ────────────────────────────────

function SurfaceMesh({ surface }: { surface: VolSurface }) {
  const { strikes, maturities, implied_vols } = surface;
  const nS = strikes.length;
  const nM = maturities.length;

  const [surfGeo, wireGeo] = useMemo(() => {
    const outM = Math.max(nM, UPSAMPLE_M);
    const outS = Math.max(nS, UPSAMPLE_S);
    const vols = bicubicUpsample(implied_vols, outM, outS);
    const rM   = vols.length;
    const rS   = vols[0].length;

    let vMin = Infinity, vMax = -Infinity;
    for (const row of vols) for (const v of row) {
      if (v < vMin) vMin = v;
      if (v > vMax) vMax = v;
    }
    const vRange = vMax - vMin || 0.01;

    const positions = new Float32Array(rM * rS * 3);
    const colors    = new Float32Array(rM * rS * 3);
    const indices: number[] = [];

    for (let mi = 0; mi < rM; mi++) {
      for (let si = 0; si < rS; si++) {
        const vi = mi * rS + si;
        const t  = (vols[mi][si] - vMin) / vRange;
        positions[vi * 3    ] = (si / (rS - 1)) * 2 - 1;   // strike  X: -1 → 1
        positions[vi * 3 + 1] = t * 0.8;                    // IV      Y:  0 → 0.8
        positions[vi * 3 + 2] = (mi / (rM - 1)) * 2 - 1;   // maturity Z: -1 → 1
        const c = tToColor(t);
        colors[vi * 3    ] = c.r;
        colors[vi * 3 + 1] = c.g;
        colors[vi * 3 + 2] = c.b;
      }
    }

    for (let mi = 0; mi < rM - 1; mi++) {
      for (let si = 0; si < rS - 1; si++) {
        const a = mi * rS + si,       b = mi * rS + si + 1;
        const c = (mi + 1) * rS + si, d = (mi + 1) * rS + si + 1;
        indices.push(a, b, d,  a, d, c);
      }
    }

    const sGeo = new THREE.BufferGeometry();
    sGeo.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    sGeo.setAttribute("color",    new THREE.BufferAttribute(colors,    3));
    sGeo.setIndex(indices);
    sGeo.computeVertexNormals();

    // Sparse wireframe — one line per original sample row/column only
    const wPts: number[] = [];
    const mStep = Math.max(1, Math.round((rM - 1) / (nM - 1)));
    const sStep = Math.max(1, Math.round((rS - 1) / (nS - 1)));

    for (let mi = 0; mi < rM; mi += mStep) {
      for (let si = 0; si < rS - 1; si++) {
        const a = (mi * rS + si) * 3, b = (mi * rS + si + 1) * 3;
        wPts.push(positions[a], positions[a+1], positions[a+2],
                  positions[b], positions[b+1], positions[b+2]);
      }
    }
    for (let si = 0; si < rS; si += sStep) {
      for (let mi = 0; mi < rM - 1; mi++) {
        const a = (mi * rS + si) * 3, b = ((mi + 1) * rS + si) * 3;
        wPts.push(positions[a], positions[a+1], positions[a+2],
                  positions[b], positions[b+1], positions[b+2]);
      }
    }

    const wGeo = new THREE.BufferGeometry();
    wGeo.setAttribute("position", new THREE.Float32BufferAttribute(wPts, 3));

    return [sGeo, wGeo] as const;
  }, [surface]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => () => { surfGeo.dispose(); wireGeo.dispose(); }, [surfGeo, wireGeo]);

  return (
    <group>
      <mesh geometry={surfGeo}>
        <meshStandardMaterial
          vertexColors
          side={THREE.DoubleSide}
          roughness={0.15}
          metalness={0.18}
        />
      </mesh>
      <lineSegments geometry={wireGeo}>
        <lineBasicMaterial color="#0c4a6e" transparent opacity={0.50} depthWrite={false} />
      </lineSegments>
    </group>
  );
}

// ─── Floor grid ───────────────────────────────────────────────────────────────

function FloorGrid({ nS, nM }: { nS: number; nM: number }) {
  const geo = useMemo(() => {
    const pts: number[] = [];
    for (let mi = 0; mi < nM; mi++) {
      const z = (mi / (nM - 1)) * 2 - 1;
      pts.push(-1, 0, z,  1, 0, z);
    }
    for (let si = 0; si < nS; si++) {
      const x = (si / (nS - 1)) * 2 - 1;
      pts.push(x, 0, -1,  x, 0, 1);
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
    return g;
  }, [nS, nM]);

  useEffect(() => () => geo.dispose(), [geo]);

  return (
    <lineSegments geometry={geo}>
      <lineBasicMaterial color="#27272a" transparent opacity={0.60} />
    </lineSegments>
  );
}

// ─── Axis lines ───────────────────────────────────────────────────────────────

function AxisLines() {
  const geo = useMemo(() => {
    const pts = [
      -1, 0, -1,    1, 0, -1,      // X (strike, front)
      -1, 0, -1,   -1, 0,  1,      // Z (maturity, left)
      -1, 0, -1,   -1, 0.88, -1,   // Y (IV, vertical)
    ];
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
    return g;
  }, []);

  useEffect(() => () => geo.dispose(), [geo]);

  return (
    <lineSegments geometry={geo}>
      <lineBasicMaterial color="#52525b" />
    </lineSegments>
  );
}

// ─── Axis label helpers ───────────────────────────────────────────────────────

function pickIndices(total: number, count: number): number[] {
  if (total <= count) return Array.from({ length: total }, (_, i) => i);
  return Array.from({ length: count }, (_, i) =>
    Math.round((i / (count - 1)) * (total - 1)),
  );
}

function niceIVTicks(vMin: number, vMax: number): number[] {
  const range   = vMax - vMin;
  const rawStep = range / 4;
  // Snap to nearest 0.5% (0.005 decimal); clamp to at least one step
  const step  = Math.max(0.005, Math.round(rawStep / 0.005) * 0.005);
  const start = Math.ceil(vMin / step) * step;
  const ticks: number[] = [];
  for (let v = start; v <= vMax + 1e-9; v = parseFloat((v + step).toFixed(6))) {
    ticks.push(v);
  }
  return ticks;
}

// ─── 3D axis labels — Billboard (lockX keeps text upright at all camera angles) ─

function AxisLabels({
  strikes,
  maturities,
  vMin,
  vMax,
  sMin,
  sRange,
}: {
  strikes: number[];
  maturities: string[];
  vMin: number;
  vMax: number;
  sMin: number;
  sRange: number;
}) {
  const nS     = strikes.length;
  const nM     = maturities.length;
  const vRange = (vMax - vMin) || 0.01;

  const strikeIdxs   = useMemo(() => pickIndices(nS, Math.min(nS, 6)), [nS]);
  const maturityIdxs = useMemo(() => pickIndices(nM, Math.min(nM, 5)), [nM]);
  const ivTicks      = useMemo(() => niceIVTicks(vMin, vMax), [vMin, vMax]);

  // Short tick marks perpendicular to each axis edge
  const tickGeo = useMemo(() => {
    const pts: number[] = [];
    strikeIdxs.forEach(si => {
      const x = ((strikes[si] - sMin) / sRange) * 2 - 1;
      pts.push(x, 0, 1.0,  x, -0.06, 1.0);
    });
    maturityIdxs.forEach(mi => {
      const z = nM > 1 ? (mi / (nM - 1)) * 2 - 1 : 0;
      pts.push(-1.0, 0, z,  -1.10, 0, z);
    });
    ivTicks.forEach(v => {
      const y = ((v - vMin) / vRange) * 0.8;
      // Extend further left so tick bridges from axis edge to the pushed-out labels
      pts.push(-1.0, y, -1.0,  -1.15, y, -1.0);
    });
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
    return g;
  }, [strikeIdxs, maturityIdxs, ivTicks, strikes, sMin, sRange, nM, vMin, vRange]); // eslint-disable-line

  useEffect(() => () => tickGeo.dispose(), [tickGeo]);

  const TICK_FS  = 0.088;
  const TITLE_FS = 0.108;
  // lockX={true} keeps every label upright — prevents the 180° vertical flip
  // when the camera crosses the horizontal plane during orbit.
  // outlineWidth/Color punches a dark halo so text stays legible over the mesh.
  const OW = 0.012;   // outline width
  const OC = "#09090b"; // outline colour

  return (
    <group>
      {/* Tick mark lines */}
      <lineSegments geometry={tickGeo}>
        <lineBasicMaterial color="#52525b" />
      </lineSegments>

      {/* ── X axis: Strike labels — below front floor edge ── */}
      {strikeIdxs.map(si => {
        const x = ((strikes[si] - sMin) / sRange) * 2 - 1;
        return (
          <Billboard key={`sk${si}`} position={[x, -0.20, 1.16]} lockX={true}>
            <Text fontSize={TICK_FS} color="#e4e4e7" anchorX="center" anchorY="middle"
                  outlineWidth={OW} outlineColor={OC}>
              {strikes[si] >= 100 ? strikes[si].toFixed(0) : strikes[si].toFixed(2)}
            </Text>
          </Billboard>
        );
      })}
      <Billboard position={[0, -0.34, 1.16]} lockX={true}>
        <Text fontSize={TITLE_FS} color="#ffffff" anchorX="center" anchorY="middle"
              letterSpacing={0.06} outlineWidth={OW} outlineColor={OC}>
          STRIKE
        </Text>
      </Billboard>

      {/* ── Z axis: Maturity labels — outside left floor edge ── */}
      {maturityIdxs.map(mi => {
        const z = nM > 1 ? (mi / (nM - 1)) * 2 - 1 : 0;
        return (
          <Billboard key={`mt${mi}`} position={[-1.26, -0.20, z]} lockX={true}>
            <Text fontSize={TICK_FS} color="#e4e4e7" anchorX="center" anchorY="middle"
                  outlineWidth={OW} outlineColor={OC}>
              {maturities[mi]}
            </Text>
          </Billboard>
        );
      })}
      <Billboard position={[-1.26, -0.34, 0]} lockX={true}>
        <Text fontSize={TITLE_FS} color="#ffffff" anchorX="center" anchorY="middle"
              letterSpacing={0.06} outlineWidth={OW} outlineColor={OC}>
          MATURITY
        </Text>
      </Billboard>

      {/* ── Y axis: IV% labels ──
           Pushed to x=-1.50 (clear of maturity labels at x=-1.26) and z=-1.0
           (on the back-edge plane, not buried behind z=-1.16).
           +0.02 y-lift stops the vMin tick from sitting on the floor grid line. */}
      {ivTicks.map(v => {
        const y = ((v - vMin) / vRange) * 0.8;
        return (
          <Billboard key={`iv${v}`} position={[-1.50, y + 0.02, -1.0]} lockX={true}>
            <Text fontSize={TICK_FS} color="#e4e4e7" anchorX="center" anchorY="middle"
                  outlineWidth={OW} outlineColor={OC}>
              {(v * 100).toFixed(0)}%
            </Text>
          </Billboard>
        );
      })}
      <Billboard position={[-1.50, 0.98, -1.0]} lockX={true}>
        <Text fontSize={TITLE_FS} color="#ffffff" anchorX="center" anchorY="middle"
              letterSpacing={0.06} outlineWidth={OW} outlineColor={OC}>
          IV %
        </Text>
      </Billboard>
    </group>
  );
}

// ─── Colour legend (CSS overlay) ──────────────────────────────────────────────

function IVLegend({ vMin, vMax }: { vMin: number; vMax: number }) {
  const ticks = niceIVTicks(vMin, vMax);
  return (
    <div
      className="absolute right-2 inset-y-3 flex flex-col pointer-events-none"
      style={{ width: 52 }}
    >
      {/* Max label */}
      <span className="font-mono text-right mb-1 block" style={{ fontSize: 9, color: "#67e8f9" }}>
        {(vMax * 100).toFixed(1)}%
      </span>
      {/* Bar + tick labels side by side */}
      <div className="flex flex-row flex-1 items-stretch gap-1.5">
        <div className="flex flex-col justify-between items-end flex-1">
          {[...ticks].reverse().map(v => (
            <span key={v} className="font-mono leading-none" style={{ fontSize: 8, color: "#a1a1aa" }}>
              {(v * 100).toFixed(0)}%
            </span>
          ))}
        </div>
        <div
          className="w-2 rounded-sm flex-shrink-0"
          style={{
            background:
              "linear-gradient(to bottom, #65e8f9 0%, #22d3ee 20%, #0284c7 55%, #1e40af 80%, #0f1745 100%)",
          }}
        />
      </div>
      {/* Min label */}
      <span className="font-mono text-right mt-1 block" style={{ fontSize: 9, color: "#a1a1aa" }}>
        {(vMin * 100).toFixed(1)}%
      </span>
    </div>
  );
}

// ─── Root export ──────────────────────────────────────────────────────────────

export default function VolSurface3D({ surface }: { surface: VolSurface }) {
  const allVols = surface.implied_vols.flat();
  const vMin    = Math.min(...allVols);
  const vMax    = Math.max(...allVols);
  const sMin    = surface.strikes[0];
  const sRange  = (surface.strikes[surface.strikes.length - 1] - sMin) || 1;

  const controlsRef = useRef<{ target: THREE.Vector3; update(): void } | null>(null);

  useEffect(() => {
    const ctrl = controlsRef.current;
    if (ctrl) { ctrl.target.set(0, 0.40, 0); ctrl.update(); }
  }, []);

  return (
    <div className="relative w-full h-full">
      <Canvas
        camera={{ position: [0.0, 1.2, 3.6], fov: 44 }}
        gl={{ antialias: true, alpha: false }}
        dpr={[1, 2]}
        style={{ background: "#09090b" }}
      >
        <ambientLight intensity={0.55} color="#d0e0ff" />
        <directionalLight position={[4, 6, 3]} intensity={1.8} />
        <directionalLight position={[-3, 2, -2]} intensity={0.35} color="#4466bb" />

        <SurfaceMesh surface={surface} />
        <FloorGrid nS={surface.strikes.length} nM={surface.maturities.length} />
        <AxisLines />
        <AxisLabels
          strikes={surface.strikes}
          maturities={surface.maturities}
          vMin={vMin}
          vMax={vMax}
          sMin={sMin}
          sRange={sRange}
        />

        <OrbitControls
          ref={controlsRef as React.RefObject<any>}
          enableDamping
          dampingFactor={0.07}
          minDistance={1.8}
          maxDistance={8}
          maxPolarAngle={Math.PI * 0.88}
          enablePan
          screenSpacePanning
        />
      </Canvas>

      <IVLegend vMin={vMin} vMax={vMax} />

      {/* Bottom bar — axis key left, orbit hints right */}
      <div className="absolute bottom-0 inset-x-0 flex items-center justify-between px-2.5 py-1 pointer-events-none"
           style={{ background: "linear-gradient(to top, #09090b 60%, transparent)" }}>
        {/* Axis mapping */}
        <div className="flex gap-4">
          {([["x · ", "STRIKE"], ["z · ", "MATURITY"], ["y · ", "IV %"]] as const).map(([pre, title]) => (
            <span key={title} className="font-mono" style={{ fontSize: 8 }}>
              <span style={{ color: "#52525b" }}>{pre}</span>
              <span style={{ color: "#a1a1aa" }}>{title}</span>
            </span>
          ))}
        </div>
        {/* Interaction hints */}
        <div className="flex gap-3">
          {([["drag", "orbit"], ["scroll", "zoom"], ["right-drag", "pan"]] as const).map(([key, label]) => (
            <span key={key} className="font-mono" style={{ fontSize: 8 }}>
              <span style={{ color: "#3f3f46" }}>{key} </span>
              <span style={{ color: "#52525b" }}>{label}</span>
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}
