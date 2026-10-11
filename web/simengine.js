/* ブラウザ版のペーパートレード・エンジン。src/sim.py の Book / 戦略 / backtest をそのまま移したもの。
 *
 * 決まり（Python 版と同じ。tests/test_sim_parity.py で同じ数字になることを確かめる）:
 *   - 判断はその日の終値まで、約定は銘柄ごとに「判断日より後の最初の足」の終値
 *   - 手数料 0.10%、スリッページ 0.05%、為替スプレッド 0.25 円/ドル（片道）、端数は買わない
 *   - 価格は配当調整後終値（分配金は再投資扱い）
 *
 * データ形式（tools/export_sim_data.py）:
 *   { dates: ["2008-04-11", ...], symbols: [...], adj: {SYM: [price|null, ...]}, fx: [USDJPY|null, ...] }
 */
(function (root) {
  "use strict";

  const COSTS = { fee: 0.0010, slip: 0.0005, spread: 0.25 };
  const isJpy = s => s.endsWith(".T");
  const lotOf = () => 1;

  // ---------------------------------------------------------------- 価格

  class Prices {
    constructor(data) {
      this.dates = data.dates;
      this.symbols = data.symbols;
      this.adj = data.adj;
      this.fxRaw = data.fx;
      this.n = data.dates.length;
      // 「その日以前で最新」を O(1) で引くため、前方埋めした配列と、
      // 実際に足がある位置の一覧（シグナル計算用）を作っておく
      this.ff = {};
      this.idx = {};
      for (const s of this.symbols) {
        const a = this.adj[s], f = new Array(this.n), ix = [];
        let last = null;
        for (let i = 0; i < this.n; i++) {
          if (a[i] !== null && a[i] !== undefined) { last = a[i]; ix.push(i); }
          f[i] = last;
        }
        this.ff[s] = f; this.idx[s] = ix;
      }
      this.fxff = new Array(this.n);
      let lf = null;
      for (let i = 0; i < this.n; i++) { if (this.fxRaw[i] != null) lf = this.fxRaw[i]; this.fxff[i] = lf; }
    }
    hasBar(s, i) { const a = this.adj[s]; return !!a && a[i] !== null && a[i] !== undefined; }
    last(s, i) { const f = this.ff[s]; return f ? f[i] : null; }
    fxAt(i) { return this.fxff[i]; }
    jpy(s, i) {
      const p = this.last(s, i);
      if (p === null || p === undefined) return null;
      if (isJpy(s)) return p;
      const fx = this.fxAt(i);
      return fx === null ? null : p * fx;
    }
    // 位置 i までの実際の足（古い順）。戦略の判断に使う
    upto(s, i) {
      const ix = this.idx[s];
      if (!ix) return [];
      let lo = 0, hi = ix.length;               // ix[k] <= i となる最後の k を探す
      while (lo < hi) { const m = (lo + hi) >> 1; if (ix[m] <= i) lo = m + 1; else hi = m; }
      return { ix, end: lo, a: this.adj[s] };
    }
    indexOf(date) {                              // date 以上の最初の位置
      let lo = 0, hi = this.n;
      while (lo < hi) { const m = (lo + hi) >> 1; if (this.dates[m] < date) lo = m + 1; else hi = m; }
      return lo;
    }
  }

  // シグナル用の小さな道具（Python の _ret / _above_sma と同じ判定）
  function ret(p, s, i, days) {
    const u = p.upto(s, i);
    if (!u.ix || u.end <= days) return null;
    const a = u.a, ix = u.ix;
    return a[ix[u.end - 1]] / a[ix[u.end - 1 - days]] - 1;
  }
  function aboveSma(p, s, i, days = 200) {
    const u = p.upto(s, i);
    if (!u.ix || u.end < days) return null;
    let sum = 0;
    for (let k = u.end - days; k < u.end; k++) sum += u.a[u.ix[k]];
    return u.a[u.ix[u.end - 1]] > sum / days;
  }

  // ---------------------------------------------------------------- 口座

  class Book {
    constructor(name, cash) {
      this.name = name; this.cash = cash;
      this.qty = new Map(); this.cost = new Map();
      this.pending = []; this.trades = []; this.nav = []; this.notes = [];
      this.realized = 0; this.fees = 0; this.lastDecision = null;
    }
    value(p, i) {
      let t = this.cash;
      for (const [s, q] of this.qty) {
        if (!q) continue;
        const px = p.jpy(s, i);
        if (px === null) throw new Error(`${this.name}: ${s} の ${p.dates[i]} 時点の価格がありません`);
        t += q * px;
      }
      return t;
    }
    weights(p, i) {
      const v = this.value(p, i), out = {};
      for (const [s, q] of this.qty) if (q) out[s] = q * p.jpy(s, i) / v;
      out.CASH = this.cash / v;
      return out;
    }
    decide(p, i, target, band = 0.005, reason = "") {
      const nav = this.value(p, i);
      const orders = [];
      const syms = [...new Set([...Object.keys(target), ...this.qty.keys()])].filter(s => s !== "CASH").sort();
      for (const s of syms) {
        const w = target[s] || 0;
        const px = p.jpy(s, i);
        if (px === null) { this.notes.push(`${p.dates[i]} ${s}: 価格が無いため注文を作りませんでした`); continue; }
        const lot = lotOf(s);
        const want = Math.floor(w * nav / px / lot) * lot;
        const have = this.qty.get(s) || 0;
        const diff = want - have;
        if (diff === 0) continue;
        if (want !== 0 && Math.abs(diff) * px < band * nav) continue;
        orders.push({ symbol: s, side: diff > 0 ? "buy" : "sell", qty: Math.abs(diff), decided: i, reason });
      }
      // 売りを先に（安定ソート）
      this.pending = orders.filter(o => o.side === "sell").concat(orders.filter(o => o.side !== "sell"));
      this.lastDecision = i;
    }
    processDay(p, i) {
      const remaining = [];
      for (const o of this.pending) {
        if (i > o.decided && p.hasBar(o.symbol, i)) this._fill(o, p, i);
        else remaining.push(o);
      }
      this.pending = remaining;
      this.nav.push([i, this.value(p, i)]);
    }
    _fill(o, p, i) {
      const s = o.symbol;
      let qty = o.qty;
      const px = p.adj[s][i];
      const fx = isJpy(s) ? 1 : p.fxAt(i);
      if (fx === null) { this.notes.push(`${p.dates[i]} ${s}: 為替が無いため約定を見送りました`); return; }
      let gross, fee;
      if (o.side === "buy") {
        const unit = px * (1 + COSTS.slip) * (fx + (isJpy(s) ? 0 : COSTS.spread));
        const lot = lotOf(s);
        const afford = Math.floor(this.cash / (unit * (1 + COSTS.fee)) / lot) * lot;
        if (afford < qty) { this.notes.push(`${p.dates[i]} ${s}: 現金不足で買付を ${qty} → ${afford} に減らしました`); qty = afford; }
        if (qty <= 0) return;
        gross = qty * unit; fee = gross * COSTS.fee;
        this.cash -= gross + fee;
        this.qty.set(s, (this.qty.get(s) || 0) + qty);
        this.cost.set(s, (this.cost.get(s) || 0) + gross + fee);
      } else {
        const have = this.qty.get(s) || 0;
        qty = Math.min(qty, have);
        if (qty <= 0) return;
        const unit = px * (1 - COSTS.slip) * (fx - (isJpy(s) ? 0 : COSTS.spread));
        gross = qty * unit; fee = gross * COSTS.fee;
        const basis = (this.cost.get(s) || 0) * qty / have;
        this.cash += gross - fee;
        this.realized += gross - fee - basis;
        this.qty.set(s, have - qty);
        this.cost.set(s, (this.cost.get(s) || 0) - basis);
        if (this.qty.get(s) === 0) { this.qty.delete(s); this.cost.delete(s); }
      }
      this.fees += fee;
      this.trades.push({ i, decided: o.decided, symbol: s, side: o.side, qty, price: px, fx: isJpy(s) ? null : fx, jpy: gross, fee, reason: o.reason });
    }
  }

  // ---------------------------------------------------------------- 戦略（src/sim.py と同じ）

  const TREND_ASSETS = ["VT", "QQQ", "1348.T", "AGG", "GLD"];
  const STRATEGIES = {
    allcountry: { freq: "once", fn: () => ({ VT: 1.0 }) },
    core_satellite: { freq: "monthly", fn: () => ({ VT: 0.55, QQQ: 0.20, "1348.T": 0.05, AGG: 0.15 }) },
    trend: {
      freq: "monthly",
      fn: (p, i) => {
        const out = {};
        for (const s of TREND_ASSETS) if (aboveSma(p, s, i) === true) out[s] = 1.0 / TREND_ASSETS.length;
        return out;
      },
    },
    dual_momentum: {
      freq: "monthly",
      fn: (p, i) => {
        const r = { SPY: ret(p, "SPY", i, 252), VEU: ret(p, "VEU", i, 252), BIL: ret(p, "BIL", i, 252) };
        if (Object.values(r).some(v => v === null)) return { AGG: 1.0 };
        const best = r.SPY >= r.VEU ? "SPY" : "VEU";
        return r[best] > r.BIL ? { [best]: 1.0 } : { AGG: 1.0 };
      },
    },
  };

  const newMonth = (p, i, prev) => prev === null || p.dates[i].slice(5, 7) !== p.dates[prev].slice(5, 7);

  /** strategies: {name: {freq, fn(p, i) -> weights}}。monthly > 0 で毎月初に入金（積立）。 */
  function backtest(p, startDate, capital, opts = {}) {
    const strategies = opts.strategies || STRATEGIES;
    const monthly = opts.monthly || 0;
    const i0 = p.indexOf(startDate);
    const i1 = opts.endDate ? p.indexOf(opts.endDate + "￿") - 1 : p.n - 1;
    const books = {}, flows = {};
    for (const k of Object.keys(strategies)) { books[k] = new Book(k, capital); flows[k] = [[i0, -capital]]; }
    let prev = null;
    for (let i = i0; i <= i1; i++) {
      const nm = newMonth(p, i, prev);
      for (const [k, st] of Object.entries(strategies)) {
        const b = books[k];
        if (monthly && nm && prev !== null) { b.cash += monthly; flows[k].push([i, -monthly]); }
        b.processDay(p, i);
        const due = (st.freq === "once" && !monthly) ? b.lastDecision === null : nm;
        if (due) b.decide(p, i, st.fn(p, i), 0.005, k);
      }
      prev = i;
    }
    for (const k of Object.keys(books)) books[k].flows = flows[k];
    return books;
  }

  // 年率（XIRR）。src/transactions.py の xirr と同じ二分法
  function xirr(flows, dates) {
    if (flows.length < 2) return null;
    if (!(flows.some(f => f[1] < 0) && flows.some(f => f[1] > 0))) return null;
    const t0 = Date.parse(dates[flows[0][0]]);
    const yrs = flows.map(f => (Date.parse(dates[f[0]]) - t0) / 86400000 / 365);
    const npv = r => flows.reduce((a, f, k) => a + f[1] / Math.pow(1 + r, yrs[k]), 0);
    let lo = -0.9999, hi = 10, flo = npv(lo), fhi = npv(hi);
    if (flo * fhi > 0) return null;
    for (let k = 0; k < 200; k++) {
      const mid = (lo + hi) / 2, fm = npv(mid);
      if (Math.abs(fm) < 1e-7) return mid;
      if (flo * fm < 0) { hi = mid; fhi = fm; } else { lo = mid; flo = fm; }
    }
    return (lo + hi) / 2;
  }

  function stats(b, p) {
    const nav = b.nav.map(x => x[1]);
    let peak = -Infinity, dd = 0;
    for (const v of nav) { peak = Math.max(peak, v); dd = Math.min(dd, v / peak - 1); }
    const paid = -(b.flows || []).reduce((a, f) => a + f[1], 0);
    const end = nav[nav.length - 1];
    const fl = (b.flows || []).concat([[b.nav[b.nav.length - 1][0], end]]);
    return { final: end, paid, ret: paid ? end / paid - 1 : 0, maxDD: dd, trades: b.trades.length, fees: b.fees,
             realized: b.realized, xirr: xirr(fl, p.dates) };
  }

  // 21 営業日リターンの分布。比べる口座と同じ期間の基準と比べる
  function windowReturns(nav, days = 21) {
    const out = [];
    for (let k = 0; k + days < nav.length; k++) out.push(nav[k + days][1] / nav[k][1] - 1);
    return out;
  }

  const api = { Prices, Book, STRATEGIES, TREND_ASSETS, COSTS, backtest, stats, xirr, windowReturns, ret, aboveSma };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.SimEngine = api;
})(typeof window !== "undefined" ? window : globalThis);
