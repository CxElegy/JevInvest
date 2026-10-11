// tests/test_future.py から呼ぶ。合成データでブートストラップを回し、結果を JSON で返す
const fs = require("fs");
const E = require("../web/simengine.js");
const [dataPath, optsJson] = process.argv.slice(2);
const p = new E.Prices(JSON.parse(fs.readFileSync(dataPath, "utf8")));
const r = E.bootstrap(p, JSON.parse(optsJson));
process.stdout.write(JSON.stringify({ final: r.final, pLoss: r.pLoss, paid: r.paid, months: r.months, dd: r.dd, last: r.bands[r.bands.length - 1] }));
