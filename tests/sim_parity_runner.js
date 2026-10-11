// tests/test_sim_parity.py から呼ぶ。JSON のデータで JS 版のバックテストを回し、結果を JSON で返す
const fs = require("fs");
const E = require("../web/simengine.js");
const [dataPath, start, capital, monthly] = process.argv.slice(2);
const p = new E.Prices(JSON.parse(fs.readFileSync(dataPath, "utf8")));
const books = E.backtest(p, start, Number(capital), { monthly: Number(monthly), strategies: E.ALL_STRATEGIES });
const out = {};
for (const [k, b] of Object.entries(books)) {
  out[k] = { nav: b.nav.map(([i, v]) => [p.dates[i], v]), trades: b.trades.map(t => [p.dates[t.i], t.symbol, t.side, t.qty]),
             realized: b.realized, fees: b.fees, xirr: E.stats(b, p).xirr };
}
process.stdout.write(JSON.stringify(out));
