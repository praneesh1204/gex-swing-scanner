// JS engine parity: web/static/engine.js vs the scipy reference in web/static/golden.json (tolerance 1e-6).
// Run by tests/test_pricing.py::test_js_engine_parity:  node tests/js/parity.mjs
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const statics = join(here, "..", "..", "src", "gexscan", "web", "static");
const golden = JSON.parse(readFileSync(join(statics, "golden.json"), "utf8"));
const engine = await import(join(statics, "engine.js"));

const res = engine.selfTest(golden);
const leg = { side: -1, type: "P", strike: 237.5, expiry: "2026-11-13", qty: 2, fill: 3.15, iv: 0.44 };
const back = engine.decodeLeg(engine.encodeLeg(leg));
if (JSON.stringify(back) !== JSON.stringify(leg)) res.fails.push("leg codec round trip");
for (const bad of ["X:C:1:2026-11-13:1:1:", "B:Z:1:2026-11-13:1:1:", "B:C:0:2026-11-13:1:1:", "B:C:100::1:1:"]) {
  try { engine.decodeLeg(bad); res.fails.push(`decodeLeg accepted ${bad}`); } catch { /* expected */ }
}
const t0 = performance.now();   // budget: a full table re-price well under 50 ms (spec §6.6)
const legs = golden.positions[1].legs, prices = Array.from({ length: 41 }, (_, i) => 200 + i * 2.5);
const dates = Array.from({ length: 30 }, (_, i) => engine.addDays("2026-10-02", i));
engine.table(legs, prices, dates, "2026-10-02", 0.04, 0.012);
const ms = performance.now() - t0;
console.log(`checked ${res.checked} values, worst relative error ${res.worst.toExponential(2)}, table ${ms.toFixed(1)} ms`);
if (res.fails.length || ms > 50) {
  for (const f of res.fails.slice(0, 20)) console.log("FAIL", f);
  if (ms > 50) console.log("FAIL table re-price too slow");
  process.exit(1);
}
console.log("PASS");
