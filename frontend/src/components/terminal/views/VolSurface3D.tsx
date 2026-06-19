/**
 * VolSurface3D — hardware-accelerated WebGL surface via React Three Fiber.
 *
 * Axes follow the volatility infrastructure spec:
 *   X — log-moneyness  ln(K/F)  (the natural SVI argument)
 *   Z — maturity tenor
 *   Y — total variance σ²T      (SVI representation space; monotone in T ⟹ calendar-arb-free)
 *
 * Lazy-imported by DataOverview (React.lazy) so Three.js is never bundled
 * into the SSR pass and never touches server-side Node globals.
 */

import { useMemo, useEffect, useRef } from "react";
import { Canvas } from "@react-three/fiber";
import { OrbitControls, Text, Billboard } from "@react-three/drei";
import * as THREE from "three";

// ─── Type ─────────────────────────────────────────────────────────────────────

type VolSurface = {
  log_moneyness: number[];      // ln(K/F) grid, e.g. [-0.22, -0.16, ..., 0.18]
  maturities: string[];
  total_variances: number[][];  // σ²T grid  [maturity_idx][moneyness_idx]
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
  const { log_moneyness, maturities, total_variances } = surface;
  const nS = log_moneyness.length;
  const nM = maturities.length;

  const [surfGeo, wireGeo] = useMemo(() => {
    const outM = Math.max(nM, UPSAMPLE_M);
    const outS = Math.max(nS, UPSAMPLE_S);
    const vols = bicubicUpsample(total_variances, outM, outS);
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
        positions[vi * 3    ] = (si / (rS - 1)) * 2 - 1;   // log-moneyness  X: -1 → 1
        positions[vi * 3 + 1] = t * 0.8;                    // total variance Y:  0 → 0.8
        positions[vi * 3 + 2] = (mi / (rM - 1)) * 2 - 1;   // maturity       Z: -1 → 1
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
      -1, 0, -1,    1, 0, -1,      // X (log-moneyness, front)
      -1, 0, -1,   -1, 0,  1,      // Z (maturity, left)
      -1, 0, -1,   -1, 0.88, -1,   // Y (total variance, vertical)
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

function niceTVTicks(tvMin: number, tvMax: number): number[] {
  const range   = tvMax - tvMin;
  const rawStep = range / 4;
  const snap    = rawStep < 0.005 ? 0.001 : rawStep < 0.05 ? 0.005 : 0.01;
  const step    = Math.max(snap, Math.round(rawStep / snap) * snap);
  const start   = Math.ceil(tvMin / step) * step;
  const ticks: number[] = [];
  for (let v = start; v <= tvMax + 1e-9; v = parseFloat((v + step).toFixed(6))) {
    ticks.push(v);
  }
  return ticks;
}

// ─── 3D axis labels — Billboard (lockX keeps text upright at all camera angles) ─

function AxisLabels({
  logMoneyness,
  maturities,
  vMin,
  vMax,
  sMin,
  sRange,
}: {
  logMoneyness: number[];
  maturities: string[];
  vMin: number;
  vMax: number;
  sMin: number;
  sRange: number;
}) {
  const nS     = logMoneyness.length;
  const nM     = maturities.length;
  const vRange = (vMax - vMin) || 0.01;

  const lmIdxs       = useMemo(() => pickIndices(nS, Math.min(nS, 6)), [nS]);
  const maturityIdxs = useMemo(() => pickIndices(nM, Math.min(nM, 5)), [nM]);
  const tvTicks      = useMemo(() => niceTVTicks(vMin, vMax), [vMin, vMax]);

  const tickGeo = useMemo(() => {
    const pts: number[] = [];
    lmIdxs.forEach(si => {
      const x = ((logMoneyness[si] - sMin) / sRange) * 2 - 1;
      pts.push(x, 0, 1.0,  x, -0.06, 1.0);
    });
    maturityIdxs.forEach(mi => {
      const z = nM > 1 ? (mi / (nM - 1)) * 2 - 1 : 0;
      pts.push(-1.0, 0, z,  -1.10, 0, z);
    });
    tvTicks.forEach(v => {
      const y = ((v - vMin) / vRange) * 0.8;
      pts.push(-1.0, y, -1.0,  -1.15, y, -1.0);
    });
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
    return g;
  }, [lmIdxs, maturityIdxs, tvTicks, logMoneyness, sMin, sRange, nM, vMin, vRange]); // eslint-disable-line

  useEffect(() => () => tickGeo.dispose(), [tickGeo]);

  const TICK_FS  = 0.088;
  const TITLE_FS = 0.108;
  const OW = 0.012;
  const OC = "#09090b";

  return (
    <group>
      <lineSegments geometry={tickGeo}>
        <lineBasicMaterial color="#52525b" />
      </lineSegments>

      {/* ── X axis: log-moneyness labels ── */}
      {lmIdxs.map(si => {
        const x = ((logMoneyness[si] - sMin) / sRange) * 2 - 1;
        return (
          <Billboard key={`lm${si}`} position={[x, -0.20, 1.16]} lockX={true}>
            <Text fontSize={TICK_FS} color="#e4e4e7" anchorX="center" anchorY="middle"
                  outlineWidth={OW} outlineColor={OC}>
              {logMoneyness[si].toFixed(2)}
            </Text>
          </Billboard>
        );
      })}
      <Billboard position={[0, -0.34, 1.16]} lockX={true}>
        <Text fontSize={TITLE_FS} color="#ffffff" anchorX="center" anchorY="middle"
              letterSpacing={0.06} outlineWidth={OW} outlineColor={OC}>
          LN(K/F)
        </Text>
      </Billboard>

      {/* ── Z axis: maturity labels ── */}
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

      {/* ── Y axis: total variance labels ── */}
      {tvTicks.map(v => {
        const y = ((v - vMin) / vRange) * 0.8;
        return (
          <Billboard key={`tv${v}`} position={[-1.50, y + 0.02, -1.0]} lockX={true}>
            <Text fontSize={TICK_FS} color="#e4e4e7" anchorX="center" anchorY="middle"
                  outlineWidth={OW} outlineColor={OC}>
              {v.toFixed(3)}
            </Text>
          </Billboard>
        );
      })}
      <Billboard position={[-1.50, 0.98, -1.0]} lockX={true}>
        <Text fontSize={TITLE_FS} color="#ffffff" anchorX="center" anchorY="middle"
              letterSpacing={0.06} outlineWidth={OW} outlineColor={OC}>
          σ²T
        </Text>
      </Billboard>
    </group>
  );
}

// ─── Colour legend (CSS overlay) ──────────────────────────────────────────────

function TVLegend({ vMin, vMax }: { vMin: number; vMax: number }) {
  const ticks = niceTVTicks(vMin, vMax);
  return (
    <div
      className="absolute right-2 inset-y-3 flex flex-col pointer-events-none"
      style={{ width: 56 }}
    >
      <span className="font-mono text-right mb-1 block" style={{ fontSize: 9, color: "#67e8f9" }}>
        {vMax.toFixed(3)}
      </span>
      <div className="flex flex-row flex-1 items-stretch gap-1.5">
        <div className="flex flex-col justify-between items-end flex-1">
          {[...ticks].reverse().map(v => (
            <span key={v} className="font-mono leading-none" style={{ fontSize: 8, color: "#a1a1aa" }}>
              {v.toFixed(3)}
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
      <span className="font-mono text-right mt-1 block" style={{ fontSize: 9, color: "#a1a1aa" }}>
        {vMin.toFixed(3)}
      </span>
    </div>
  );
}

// ─── Root export ──────────────────────────────────────────────────────────────

export default function VolSurface3D({ surface }: { surface: VolSurface }) {
  const allTV  = surface.total_variances.flat();
  const vMin   = Math.min(...allTV);
  const vMax   = Math.max(...allTV);
  const sMin   = surface.log_moneyness[0];
  const sRange = (surface.log_moneyness[surface.log_moneyness.length - 1] - sMin) || 1;

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
        <FloorGrid nS={surface.log_moneyness.length} nM={surface.maturities.length} />
        <AxisLines />
        <AxisLabels
          logMoneyness={surface.log_moneyness}
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

      <TVLegend vMin={vMin} vMax={vMax} />

      {/* Bottom bar — axis key left, orbit hints right */}
      <div className="absolute bottom-0 inset-x-0 flex items-center justify-between px-2.5 py-1 pointer-events-none"
           style={{ background: "linear-gradient(to top, #09090b 60%, transparent)" }}>
        <div className="flex gap-4">
          {([["x · ", "LN(K/F)"], ["z · ", "MATURITY"], ["y · ", "σ²T"]] as const).map(([pre, title]) => (
            <span key={title} className="font-mono" style={{ fontSize: 8 }}>
              <span style={{ color: "#52525b" }}>{pre}</span>
              <span style={{ color: "#a1a1aa" }}>{title}</span>
            </span>
          ))}
        </div>
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
