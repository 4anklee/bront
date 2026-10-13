"""Offline report styling; no fonts, scripts, or assets fetched from a network."""

CSS = """
:root { color-scheme: light; font-family: ui-sans-serif, system-ui, sans-serif;
  color: #182b40; background: #f4f6f8; line-height: 1.55; }
* { box-sizing: border-box; }
body { margin: 0; }
main { max-width: 1120px; margin: auto; padding: 48px 24px; }
a { color: #135c9c; text-underline-offset: 3px; }
h1 { font-size: clamp(2rem, 5vw, 3rem); letter-spacing: -.04em; margin: 12px 0; }
h2 { font-size: 1.1rem; margin: 24px 0 8px; }
p { margin: 8px 0 20px; }
.brand { font-weight: 800; letter-spacing: .18em; font-size: .8rem; }
.subtitle, .note { color: #506174; }
section, .run { background: white; border: 1px solid #dce3ea; border-radius: 12px;
  padding: 24px; margin-top: 24px; }
section section { border: 0; padding: 0; border-radius: 0; }
.run > summary { font-weight: 700; }
.badge { display: inline-block; padding: 3px 10px; border-radius: 5px; font-size: .8rem;
  font-weight: 750; text-transform: uppercase; color: #35485d; background: #e9eef3; }
.passed { color: #12603d; background: #e1f3e9; }
.failed, .error { color: #9c2830; background: #fce8e8; }
.inconclusive, .incompatible, .cancelled, .running, .incomplete {
  color: #7c5107; background: #fff0cc; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(145px, 1fr));
  gap: 16px; margin-top: 24px; }
.stat { border-left: 3px solid #386f9f; padding-left: 14px; }
.stat strong { display: block; font-size: 1.6rem; font-variant-numeric: tabular-nums; }
.stat span { font-size: .85rem; color: #506174; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: .875rem; }
caption { text-align: left; margin-bottom: 12px; color: #506174; }
th, td { padding: 10px 12px; border-bottom: 1px solid #e5eaf0;
  text-align: left; vertical-align: top; overflow-wrap: anywhere; min-width: 96px; }
th { font-weight: 650; background: #f4f6f8; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; background: #f4f6f8;
  padding: 16px; border-radius: 6px; font-size: .8rem; }
code { font-family: ui-monospace, monospace; overflow-wrap: anywhere; }
summary { cursor: pointer; padding: 8px 0; }
svg { min-width: 580px; width: 100%; height: auto; display: block; margin: 12px 0; }
svg text { font: 12px ui-sans-serif, system-ui, sans-serif; fill: #506174; }
.warning { border-left: 4px solid #b17813; padding: 12px 16px; background: #fff8e6; }
.downloads { display: flex; flex-wrap: wrap; gap: 12px; margin: 16px 0; }
footer { margin-top: 32px; color: #506174; font-size: .85rem; }
@media (max-width: 600px) { main { padding: 24px 12px; } section, .run { padding: 16px; } }
@media print { :root { background: white; } main { max-width: none; padding: 0; }
  section, .run { break-inside: avoid; } .downloads { display: none; } }
"""
