"""分析結果を単一ファイルの HTML ダッシュボードにする。

外部ライブラリを使わず、インライン SVG と素の JS で描く。
配布物が 1 ファイルで完結し、オフラインでも開ける。

配色は検証済みのパレットを使っている（validate_palette.js で全チェック合格）。
ライトモードで一部の色が 3:1 を切るため、**直接ラベルとテーブルビュー**を
必ず出す（色だけに意味を持たせない）。

チャートの使い分け:
  配分      … ドーナツ（全体に対する構成比）
  集中度    … 横棒 + ガードレールの基準線（単系列なので 1 色）
  相関      … ヒートマップ（-1〜+1 なので発散配色。中点はグレー）
  積立      … 積み上げ棒（つみたて枠 / 成長枠）
"""

from __future__ import annotations

import json
from datetime import datetime

import pandas as pd

from .analytics import Analysis

# 検証済みパレット（categorical 1..5）
SERIES_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"]


def _series_payload(a: Analysis, plan=None) -> dict:
    total = a.total_value_base or 1.0

    # --- 配分（資産クラス別）---
    alloc = []
    for cls, row in a.by_asset_class.iterrows():
        alloc.append(
            {
                "label": str(cls),
                "value": float(row["評価額"]),
                "pct": float(row["構成比%"]),
                "pnl": float(row["損益"]),
                "pnl_pct": float(row["損益率%"]),
            }
        )

    # --- 集中度（銘柄別ウェイト。同一銘柄は口座をまたいで合算）---
    by_symbol: dict[str, dict] = {}
    for v in a.valuations:
        if not v.priced:
            continue
        s = v.position.symbol
        e = by_symbol.setdefault(
            s, {"label": s, "name": v.position.name, "value": 0.0, "accounts": []}
        )
        e["value"] += v.market_value_base
        e["accounts"].append(v.position.account)
    conc = sorted(by_symbol.values(), key=lambda d: -d["value"])
    for e in conc:
        e["pct"] = e["value"] / total * 100.0
        e["accounts"] = sorted(set(e["accounts"]))

    # --- 相関 ---
    corr = a.risk.get("相関")
    corr_payload = None
    if isinstance(corr, pd.DataFrame) and not corr.empty and len(corr) > 1:
        corr_payload = {
            "labels": [str(c) for c in corr.columns],
            "matrix": [[None if pd.isna(v) else round(float(v), 3) for v in row]
                       for row in corr.values],
        }

    # --- 積立計画 ---
    plan_payload = None
    if plan is not None and plan.classes:
        rows = [
            {
                "label": c.asset_class,
                "tsumitate": float(c.tsumitate),
                "growth": float(c.growth),
                "taxable": float(c.taxable),
                "cash": float(c.held_cash),
                "status": c.status,
            }
            for c in plan.classes
            if (c.tsumitate + c.growth + c.taxable + c.held_cash) > 0
        ]
        plan_payload = {"monthly": float(plan.monthly), "rows": rows}

    return {
        "as_of": a.as_of.strftime("%Y-%m-%d %H:%M"),
        "currency": a.base_currency,
        "total": float(a.total_value_base),
        "cost": float(a.total_cost_base),
        "pnl": float(a.total_pnl_base),
        "pnl_pct": float(a.total_pnl_pct),
        "unpriced": int(a.unpriced_count),
        "concentration": {k: (float(v) if isinstance(v, (int, float)) else v)
                          for k, v in (a.concentration or {}).items()},
        "risk": {
            k: (float(v) if isinstance(v, (int, float)) else v)
            for k, v in (a.risk or {}).items()
            if k in ("年率ボラ%", "最大DD%", "分散効果%", "ベータ", "対象期間",
                     "カバー率%", "ベンチマーク")
        },
        "guards": [
            {
                "guard": g.guard, "severity": g.severity, "subject": g.subject,
                "detail": g.detail, "current": float(g.current), "limit": float(g.limit),
            }
            for g in a.guards
        ],
        "guard_config": {k: float(v) for k, v in (a.guard_config or {}).items()
                         if isinstance(v, (int, float))},
        "warnings": list(dict.fromkeys(a.warnings)),
        "alloc": alloc,
        "conc": conc,
        "corr": corr_payload,
        "plan": plan_payload,
        "palette": {"light": SERIES_LIGHT, "dark": SERIES_DARK},
    }


def _strip_amounts(data: dict) -> None:
    """匿名モード。表示を「—」にするだけでなく、埋め込む JSON からも絶対額を消す。

    HTML のソースを開けば金額が読めてしまうので、比率に置き換える。
    グラフは相対値で描くので、比率に置き換えても形は変わらない。
    """
    data["total"] = data["cost"] = data["pnl"] = 0.0
    for d in data["alloc"]:
        d["value"] = d["pct"]
        d["pnl"] = 0.0
    for d in data["conc"]:
        d["value"] = d["pct"]
    if data["plan"]:
        monthly = data["plan"]["monthly"] or 1.0
        for r in data["plan"]["rows"]:
            for k in ("tsumitate", "growth", "taxable", "cash"):
                r[k] = r[k] / monthly * 100.0
        data["plan"]["monthly"] = 0.0


def to_html(a: Analysis, plan=None, anonymize: bool = False) -> str:
    data = _series_payload(a, plan)
    data["anonymize"] = bool(anonymize)
    if anonymize:
        _strip_amounts(data)
    payload = json.dumps(data, ensure_ascii=False)

    return _TEMPLATE.replace("__PAYLOAD__", payload).replace(
        "__GENERATED__", datetime.now().strftime("%Y-%m-%d %H:%M")
    )


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>持仓ダッシュボード</title>
<style>
  :root {
    color-scheme: light;
    --page:           #f9f9f7;
    --surface-1:      #fcfcfb;
    --text-primary:   #0b0b0b;
    --text-secondary: #52514e;
    --text-muted:     #898781;
    --grid:           #e1e0d9;
    --axis:           #c3c2b7;
    --border:         rgba(11,11,11,0.10);
    --good:           #0ca30c;
    --critical:       #d03b3b;
    --warning:        #fab219;
    --seq-lo:         #cde2fb;
    --seq-mid:        #3987e5;
    --seq-hi:         #0d366b;
    --div-neg:        #2a78d6;
    --div-mid:        #f0efec;
    --div-pos:        #d03b3b;
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --page:           #0d0d0d;
    --surface-1:      #1a1a19;
    --text-primary:   #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted:     #898781;
    --grid:           #2c2c2a;
    --axis:           #383835;
    --border:         rgba(255,255,255,0.10);
    --seq-lo:         #184f95;
    --seq-mid:        #3987e5;
    --seq-hi:         #cde2fb;
    --div-neg:        #3987e5;
    --div-mid:        #383835;
    --div-pos:        #d03b3b;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px;
    background: var(--page); color: var(--text-primary);
    font: 14px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  .wrap { max-width: 1180px; margin: 0 auto; }
  header { display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap;
           margin-bottom: 20px; }
  h1 { font-size: 20px; font-weight: 650; margin: 0; }
  .meta { color: var(--text-muted); font-size: 12px; }
  .spacer { flex: 1 1 auto; }
  button {
    font: inherit; font-size: 13px; padding: 6px 12px;
    background: var(--surface-1); color: var(--text-primary);
    border: 1px solid var(--border); border-radius: 6px; cursor: pointer;
  }
  button:hover { border-color: var(--axis); }
  .card {
    background: var(--surface-1); border: 1px solid var(--border);
    border-radius: 10px; padding: 18px 20px; margin-bottom: 16px;
  }
  .card h2 { font-size: 14px; font-weight: 650; margin: 0 0 4px; }
  .card .sub { color: var(--text-secondary); font-size: 12px; margin-bottom: 14px; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
           gap: 12px; margin-bottom: 16px; }
  .tile { background: var(--surface-1); border: 1px solid var(--border);
          border-radius: 10px; padding: 14px 16px; }
  .tile .k { color: var(--text-secondary); font-size: 12px; }
  .tile .v { font-size: 24px; font-weight: 600; margin-top: 4px; letter-spacing: -0.01em; }
  .tile .n { color: var(--text-muted); font-size: 11px; margin-top: 2px; }
  .up { color: var(--good); } .down { color: var(--critical); }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 860px) { .grid2 { grid-template-columns: 1fr; } }
  .legend { display: flex; flex-wrap: wrap; gap: 10px 18px; margin-top: 12px;
            font-size: 12px; color: var(--text-secondary); }
  .legend i { width: 10px; height: 10px; border-radius: 2px; display: inline-block;
              margin-right: 6px; vertical-align: -1px; }
  table { border-collapse: collapse; width: 100%; font-size: 13px;
          font-variant-numeric: tabular-nums; }
  th, td { text-align: right; padding: 6px 8px; border-bottom: 1px solid var(--grid); }
  th:first-child, td:first-child { text-align: left; font-variant-numeric: normal; }
  th { color: var(--text-secondary); font-weight: 600; font-size: 12px; }
  .alerts li { margin-bottom: 6px; }
  .alert-v { color: var(--critical); font-weight: 600; }
  .alert-w { color: var(--text-secondary); }
  .tip {
    position: fixed; pointer-events: none; opacity: 0; transition: opacity .1s;
    background: var(--surface-1); color: var(--text-primary);
    border: 1px solid var(--axis); border-radius: 6px; padding: 8px 10px;
    font-size: 12px; box-shadow: 0 4px 14px rgba(0,0,0,.14); z-index: 50;
    font-variant-numeric: tabular-nums; max-width: 260px;
  }
  svg text { fill: var(--text-secondary); }
  svg .lbl { fill: var(--text-primary); font-size: 11px; }
  svg .mut { fill: var(--text-muted); font-size: 10px; }
  details { margin-top: 12px; }
  summary { cursor: pointer; color: var(--text-secondary); font-size: 12px; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>持仓ダッシュボード</h1>
    <span class="meta" id="asof"></span>
    <span class="spacer"></span>
    <button id="theme">ダークモード</button>
  </header>
  <div id="app"></div>
  <p class="meta">生成: __GENERATED__ ／ 計算はすべてローカル。判断は Claude Code で。</p>
</div>
<div class="tip" id="tip"></div>

<script>
const D = __PAYLOAD__;
const tip = document.getElementById('tip');

function isDark(){ return document.documentElement.dataset.theme === 'dark'; }
function pal(){ return isDark() ? D.palette.dark : D.palette.light; }
function cssv(n){ return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }

const fmtM = v => D.anonymize ? '—' : Math.round(v).toLocaleString('ja-JP');
const fmtP = v => (v>=0?'+':'') + v.toFixed(2) + '%';
const fmtPl = v => v.toFixed(1) + '%';

function showTip(e, html){
  tip.innerHTML = html; tip.style.opacity = 1;
  const r = tip.getBoundingClientRect();
  let x = e.clientX + 14, y = e.clientY + 14;
  if (x + r.width > innerWidth - 8) x = e.clientX - r.width - 14;
  if (y + r.height > innerHeight - 8) y = e.clientY - r.height - 14;
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
}
function hideTip(){ tip.style.opacity = 0; }

function el(tag, attrs, kids){
  const n = document.createElementNS(
    ['svg','g','path','rect','circle','line','text','tspan'].includes(tag)
      ? 'http://www.w3.org/2000/svg' : 'http://www.w3.org/1999/xhtml', tag);
  for (const k in (attrs||{})) {
    if (k === 'class') n.setAttribute('class', attrs[k]);
    else if (k === 'html') n.innerHTML = attrs[k];
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), attrs[k]);
    else n.setAttribute(k, attrs[k]);
  }
  (kids||[]).forEach(c => n.appendChild(c));
  return n;
}
function svg(w, h, kids){
  const s = el('svg', {viewBox:`0 0 ${w} ${h}`, width:'100%',
                       height:h, role:'img'}, kids);
  return s;
}

/* ---------------------------------------------- 配分ドーナツ */
function donut(){
  const R = 96, r = 60, cx = 120, cy = 116, gap = 0.016;  /* 2px 相当の隙間 */
  const P = pal(), items = D.alloc.filter(d => d.value > 0);
  const tot = items.reduce((s,d)=>s+d.value,0) || 1;
  let ang = -Math.PI/2;
  const kids = [];
  items.forEach((d,i) => {
    const frac = d.value/tot, sweep = frac*Math.PI*2;
    const a0 = ang + gap/2, a1 = ang + sweep - gap/2;
    if (a1 > a0) {
      const big = (a1-a0) > Math.PI ? 1 : 0;
      const p = [
        'M', cx+R*Math.cos(a0), cy+R*Math.sin(a0),
        'A', R, R, 0, big, 1, cx+R*Math.cos(a1), cy+R*Math.sin(a1),
        'L', cx+r*Math.cos(a1), cy+r*Math.sin(a1),
        'A', r, r, 0, big, 0, cx+r*Math.cos(a0), cy+r*Math.sin(a0), 'Z'
      ].join(' ');
      const seg = el('path', {d:p, fill:P[i % P.length]});
      seg.addEventListener('mousemove', e => showTip(e,
        `<b>${d.label}</b><br>構成比 ${fmtPl(d.pct)}<br>評価額 ${fmtM(d.value)} ${D.currency}`
        + `<br>損益 ${D.anonymize?'—':fmtM(d.pnl)} (${fmtP(d.pnl_pct)})`));
      seg.addEventListener('mouseleave', hideTip);
      kids.push(seg);
    }
    ang += sweep;
  });
  kids.push(el('text', {x:cx, y:cy-4, 'text-anchor':'middle', class:'lbl',
                        style:'font-size:13px;font-weight:600'},
               [document.createTextNode(items.length + ' クラス')]));
  kids.push(el('text', {x:cx, y:cy+14, 'text-anchor':'middle', class:'mut'},
               [document.createTextNode(D.anonymize ? '比率のみ' : fmtM(D.total)+' '+D.currency)]));

  /* 直接ラベル: ライトモードで一部の色が 3:1 未満なので必須 */
  let a2 = -Math.PI/2;
  items.forEach((d,i) => {
    const sweep = (d.value/(tot))*Math.PI*2, mid = a2 + sweep/2;
    if (sweep > 0.22) {
      const lx = cx + (R+16)*Math.cos(mid), ly = cy + (R+16)*Math.sin(mid);
      kids.push(el('text', {x:lx, y:ly, class:'lbl',
        'text-anchor': Math.cos(mid) < -0.2 ? 'end' : (Math.cos(mid) > 0.2 ? 'start':'middle'),
        'dominant-baseline':'middle'}, [document.createTextNode(fmtPl(d.pct))]));
    }
    a2 += sweep;
  });
  const s = svg(320, 250, kids);
  s.setAttribute('aria-label', '資産クラス別の構成比');
  const leg = el('div', {class:'legend'});
  items.forEach((d,i) => leg.appendChild(el('span', {html:
    `<i style="background:${P[i%P.length]}"></i>${d.label} ${fmtPl(d.pct)}`})));
  return [s, leg];
}

/* ---------------------------------------------- 集中度バー */
function concBars(){
  const items = D.conc.slice(0, 12);
  if (!items.length) return [el('p', {class:'meta', html:'保有銘柄がありません。'})];
  const cap = D.guard_config.max_single_position_pct || null;
  const W = 560, rowH = 26, padL = 108, padR = 46;
  const H = items.length*rowH + 34;
  const maxV = Math.max(...items.map(d=>d.pct), cap || 0) * 1.08;
  const sx = v => padL + (v/maxV)*(W-padL-padR);
  const kids = [];

  if (cap) {  /* ガードレールの基準線 */
    kids.push(el('line', {x1:sx(cap), y1:6, x2:sx(cap), y2:H-26,
      stroke:'var(--critical)', 'stroke-width':1.5, 'stroke-dasharray':'4 3'}));
    kids.push(el('text', {x:sx(cap), y:H-12, 'text-anchor':'middle', class:'mut',
      style:'fill:var(--critical)'}, [document.createTextNode('上限 '+cap+'%')]));
  }
  items.forEach((d,i) => {
    const y = 10 + i*rowH, w = Math.max(sx(d.pct)-padL, 2);
    const over = cap && d.pct > cap;
    /* 単系列なので 1 色。順位で色を変えない */
    const bar = el('rect', {x:padL, y:y, width:w, height:14, rx:4,
      fill:'var(--seq-mid)', opacity: over ? 1 : 0.86});
    bar.addEventListener('mousemove', e => showTip(e,
      `<b>${d.label}</b><br>${d.name}<br>ウェイト ${fmtPl(d.pct)}`
      + `<br>評価額 ${fmtM(d.value)} ${D.currency}`
      + `<br>口座 ${d.accounts.join(' / ')}`
      + (over ? `<br><span style="color:var(--critical)">上限 ${cap}% 超過</span>`:'')));
    bar.addEventListener('mouseleave', hideTip);
    kids.push(bar);
    kids.push(el('text', {x:padL-8, y:y+11, 'text-anchor':'end', class:'lbl'},
      [document.createTextNode(d.label.length>13 ? d.label.slice(0,12)+'…' : d.label)]));
    kids.push(el('text', {x:padL+w+7, y:y+11, class:'lbl',
      style: over ? 'fill:var(--critical);font-weight:600' : ''},
      [document.createTextNode(fmtPl(d.pct))]));
  });
  const s = svg(W, H, kids);
  s.setAttribute('aria-label','銘柄別ウェイトと上限');
  return [s];
}

/* ---------------------------------------------- 相関ヒートマップ */
function heat(){
  if (!D.corr) return [el('p', {class:'meta',
    html:'相関を計算するには履歴が 2 銘柄以上必要です。'})];
  const L = D.corr.labels, M = D.corr.matrix, n = L.length;
  const cell = Math.max(26, Math.min(46, Math.floor(420/n)));
  const padL = 96, padT = 84;
  const W = padL + n*cell + 14, H = padT + n*cell + 14;
  const kids = [];
  const neg = cssv('--div-neg'), mid = cssv('--div-mid'), pos = cssv('--div-pos');

  /* 発散配色: -1=青 / 0=グレー / +1=赤。中点に色相を置かない */
  function col(v){
    if (v === null) return 'var(--grid)';
    const t = Math.min(Math.abs(v),1);
    return `color-mix(in oklab, ${v<0?neg:pos} ${Math.round(t*100)}%, ${mid})`;
  }
  for (let i=0;i<n;i++){
    kids.push(el('text', {x:padL-8, y:padT+i*cell+cell/2, 'text-anchor':'end',
      'dominant-baseline':'middle', class:'lbl'}, [document.createTextNode(L[i])]));
    kids.push(el('text', {x:padL+i*cell+cell/2, y:padT-10,
      class:'lbl', 'text-anchor':'start',
      transform:`rotate(-45 ${padL+i*cell+cell/2} ${padT-10})`},
      [document.createTextNode(L[i])]));
    for (let j=0;j<n;j++){
      const v = M[i][j];
      const c = el('rect', {x:padL+j*cell+1, y:padT+i*cell+1,
        width:cell-2, height:cell-2, rx:3, fill:col(v),
        stroke:'var(--surface-1)', 'stroke-width':1});
      c.addEventListener('mousemove', e => showTip(e,
        `<b>${L[i]} / ${L[j]}</b><br>相関 ${v===null?'—':v.toFixed(2)}`
        + (v!==null && Math.abs(v)>=0.8 && i!==j
           ? '<br><span style="color:var(--critical)">実質同じ資産</span>' : '')));
      c.addEventListener('mouseleave', hideTip);
      kids.push(c);
      if (cell >= 34 && v !== null) {
        kids.push(el('text', {x:padL+j*cell+cell/2, y:padT+i*cell+cell/2+4,
          'text-anchor':'middle', class:'mut'},
          [document.createTextNode(v.toFixed(2))]));
      }
    }
  }
  const s = svg(W, H, kids);
  s.setAttribute('aria-label','銘柄間の相関行列');
  const leg = el('div', {class:'legend', html:
    `<span><i style="background:${neg}"></i>-1（逆相関）</span>`
    + `<span><i style="background:${mid};border:1px solid var(--axis)"></i>0</span>`
    + `<span><i style="background:${pos}"></i>+1（同じ動き）</span>`});
  return [s, leg];
}

/* ---------------------------------------------- 積立計画（積み上げ棒） */
function planBars(){
  if (!D.plan) return [el('p', {class:'meta', html:'積立計画が未設定です。'})];
  const rows = D.plan.rows, P = pal();
  const keys = [['tsumitate','つみたて枠',P[0]], ['growth','成長枠',P[1]],
                ['taxable','課税口座',P[3]], ['cash','現金留保',P[4]]];
  const W = 560, padL = 118, padB = 30, rowH = 34;
  const H = rows.length*rowH + padB + 10;
  const maxV = Math.max(...rows.map(r => keys.reduce((s,k)=>s+r[k[0]],0))) * 1.05 || 1;
  const sx = v => (v/maxV)*(W-padL-70);
  const kids = [];
  rows.forEach((r,i) => {
    const y = 8 + i*rowH;
    let x = padL;
    keys.forEach(([k,name,c]) => {
      const w = sx(r[k]);
      if (w <= 0.5) return;
      const seg = el('rect', {x:x, y:y, width:Math.max(w-2,1), height:16, rx:4, fill:c});
      seg.addEventListener('mousemove', e => showTip(e,
        `<b>${r.label}</b><br>${name} ${fmtM(r[k])} ${D.currency}<br>状態 ${r.status}`));
      seg.addEventListener('mouseleave', hideTip);
      kids.push(seg);
      x += w;   /* 2px の隙間は width 側で確保 */
    });
    const tot = keys.reduce((s,k)=>s+r[k[0]],0);
    kids.push(el('text', {x:padL-8, y:y+12, 'text-anchor':'end', class:'lbl'},
      [document.createTextNode(r.label.length>15?r.label.slice(0,14)+'…':r.label)]));
    kids.push(el('text', {x:x+7, y:y+12, class:'lbl'},
      [document.createTextNode(fmtM(tot))]));
  });
  const s = svg(W, H, kids);
  s.setAttribute('aria-label','毎月の投入額の内訳');
  const leg = el('div', {class:'legend'});
  keys.forEach(([k,name,c]) => {
    if (rows.some(r => r[k] > 0))
      leg.appendChild(el('span', {html:`<i style="background:${c}"></i>${name}`}));
  });
  return [s, leg];
}

/* ---------------------------------------------- 組み立て */
function card(title, sub, nodes, tableHtml){
  const c = el('div', {class:'card'}, [
    el('h2', {html:title}), el('div', {class:'sub', html:sub})
  ]);
  nodes.forEach(n => c.appendChild(n));
  if (tableHtml) {
    const d = el('details', {}, [el('summary', {html:'テーブルで見る'})]);
    d.appendChild(el('div', {html:tableHtml}));
    c.appendChild(d);
  }
  return c;
}

function render(){
  const app = document.getElementById('app');
  app.innerHTML = '';
  document.getElementById('asof').textContent =
    D.as_of + ' 時点 ／ 基準通貨 ' + D.currency;

  /* --- 警告とガードレール --- */
  const viol = D.guards.filter(g=>g.severity==='violation');
  const warn = D.guards.filter(g=>g.severity==='warning');
  if (viol.length || warn.length || D.warnings.length || D.unpriced) {
    let h = '<ul class="alerts">';
    if (D.unpriced) h += `<li class="alert-v">${D.unpriced} 件のポジションが評価不能（集計から除外）</li>`;
    viol.forEach(g => h += `<li class="alert-v">🚨 ${g.subject}: ${g.detail}（現在 ${g.current.toFixed(2)}% / 基準 ${g.limit}%）</li>`);
    warn.forEach(g => h += `<li class="alert-w">⚠ ${g.subject}: ${g.detail}</li>`);
    D.warnings.forEach(w => h += `<li class="alert-w">· ${w}</li>`);
    h += '</ul>';
    app.appendChild(card('先に確認すべきこと',
      '数字を読む前に。評価不能なポジションがあると比率が実態からずれます。',
      [el('div', {html:h})]));
  }

  /* --- スタットタイル --- */
  const t = el('div', {class:'tiles'});
  const tile = (k,v,n,cls) => t.appendChild(el('div', {class:'tile', html:
    `<div class="k">${k}</div><div class="v ${cls||''}">${v}</div>`
    + (n?`<div class="n">${n}</div>`:'')}));
  tile('評価額', fmtM(D.total) + ' ' + D.currency, '取得原価 ' + fmtM(D.cost));
  tile('損益', (D.anonymize?'—':(D.pnl>=0?'+':'')+fmtM(D.pnl)), fmtP(D.pnl_pct),
       D.pnl_pct>=0?'up':'down');
  if (D.concentration['実質銘柄数'])
    tile('実質銘柄数', D.concentration['実質銘柄数'].toFixed(2),
         '実 ' + D.concentration['実銘柄数'] + ' 銘柄 / HHI ' + D.concentration.hhi.toFixed(3));
  if (D.risk['年率ボラ%'])
    tile('年率ボラティリティ', D.risk['年率ボラ%'].toFixed(1)+'%',
         '最大DD ' + D.risk['最大DD%'].toFixed(1) + '%');
  app.appendChild(t);

  /* --- 配分 + 集中度 --- */
  const g2 = el('div', {class:'grid2'});
  let tbl = '<table><thead><tr><th>資産クラス</th><th>構成比</th><th>評価額</th><th>損益率</th></tr></thead><tbody>';
  D.alloc.forEach(d => tbl += `<tr><td>${d.label}</td><td>${fmtPl(d.pct)}</td><td>${fmtM(d.value)}</td><td>${fmtP(d.pnl_pct)}</td></tr>`);
  tbl += '</tbody></table>';
  g2.appendChild(card('資産クラス別の配分', '構成比。直接ラベルと凡例の両方を出しています。',
    donut(), tbl));

  let tbl2 = '<table><thead><tr><th>銘柄</th><th>ウェイト</th><th>評価額</th></tr></thead><tbody>';
  D.conc.forEach(d => tbl2 += `<tr><td>${d.label}</td><td>${fmtPl(d.pct)}</td><td>${fmtM(d.value)}</td></tr>`);
  tbl2 += '</tbody></table>';
  g2.appendChild(card('銘柄別の集中度',
    '同一銘柄は口座をまたいで合算。破線はガードレールの上限。', concBars(), tbl2));
  app.appendChild(g2);

  /* --- 積立計画 --- */
  app.appendChild(card('毎月の積立の割り振り',
    D.plan ? (D.anonymize ? '毎月の投入額に対する割合（%）を NISA 枠に当てはめた結果。'
                          : `月 ${fmtM(D.plan.monthly)} ${D.currency} を NISA 枠に当てはめた結果。`)
           : '', planBars()));

  /* --- 相関 --- */
  let tbl3 = '';
  if (D.corr) {
    tbl3 = '<table><thead><tr><th></th>' + D.corr.labels.map(l=>`<th>${l}</th>`).join('') + '</tr></thead><tbody>';
    D.corr.matrix.forEach((row,i) => {
      tbl3 += `<tr><td>${D.corr.labels[i]}</td>` +
        row.map(v=>`<td>${v===null?'—':v.toFixed(2)}</td>`).join('') + '</tr>';
    });
    tbl3 += '</tbody></table>';
  }
  app.appendChild(card('銘柄間の相関',
    '基準通貨建て日次リターン。0.8 を超えるペアは実質同じ資産です。'
    + (D.risk['対象期間'] ? ' 対象期間 ' + D.risk['対象期間'] : ''),
    heat(), tbl3));
}

document.getElementById('theme').addEventListener('click', () => {
  const dark = isDark();
  document.documentElement.dataset.theme = dark ? 'light' : 'dark';
  document.getElementById('theme').textContent = dark ? 'ダークモード' : 'ライトモード';
  render();
});
render();
</script>
</body>
</html>
"""
