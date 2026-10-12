// tests/test_future.py から呼ぶ。合成データでブートストラップを回し、結果を JSON で返す
const fs = require("fs");
const E = require("../web/simengine.js");
const [dataPath, optsJson] = process.argv.slice(2);
const p = new E.Prices(JSON.parse(fs.readFileSync(dataPath, "utf8")));
const r = E.bootstrap(p, JSON.parse(optsJson));
const o = JSON.parse(optsJson);
const recon = Array.from(r.paths.gI, (g, j) => o.initial * g + o.monthly * r.paths.gM[j]).sort((a, b) => a - b);
const goal = o.target ? E.goalSolve(r.paths, o.initial, o.monthly, o.target) : null;
process.stdout.write(JSON.stringify({ final: r.final, pLoss: r.pLoss, paid: r.paid, months: r.months, dd: r.dd, last: r.bands[r.bands.length - 1],
                                      reconMedian: recon[Math.floor(0.5 * (recon.length - 1))], goal }));
