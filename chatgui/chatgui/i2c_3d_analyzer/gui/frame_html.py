"""HTML for the I²C Communication Data Frame breakdown and the error / arbitration banners (analysis.frame_view)."""
from __future__ import annotations

import html

from analysis.frame_view import arbitration_details, arbitration_events, error_cause, frame_rows
from config import COLORS as C
from gui.theme import MONO_CSS

KIND_COLOR = {"cond": "amber", "addr": "blue", "rw": "blue", "ack": "green", "nack": "red", "data": "cyan",
              "status": "amber"}


def frame_table_html(engine, txn, title=True) -> str:
    """Every protocol field of the packet, in bus order (START, ADDRESS + bits, R/W, ACK/NACK, DATA + bits, Sr, STOP)."""
    rows = frame_rows(engine, txn)
    out = []
    if title:
        out.append(f"<p style='margin:6px 0 2px 0'><b>I²C Communication Data Frame</b> · packet #{txn.id}"
                   + (f" <span style='color:{C['demo']}'>· SIMULATED</span>" if getattr(txn, "demo", False) else "")
                   + "</p>")
    out.append(f"<table cellspacing=0 cellpadding=3 style=\"{MONO_CSS}\">"
               f"<tr style='color:{C['text2']}'><td>#</td><td>field</td><td>value</td><td>detail</td></tr>")
    for i, r in enumerate(rows, 1):
        col = C.get(KIND_COLOR.get(r["kind"], "text"), C["text"])
        out.append(f"<tr><td style='color:{C['text2']}'>{i}</td><td style='color:{col}'><b>{html.escape(r['field'])}</b></td>"
                   f"<td><b>{html.escape(r['value'])}</b></td><td style='color:{C['red'] if not r['ok'] else C['text']}'>"
                   f"{html.escape(r['detail'])}</td></tr>")
    out.append("</table>")
    return "".join(out)


def alert_html(engine, txn) -> tuple[str, str | None]:
    """(html, kind) of the banner above a packet: the error cause (red) or the arbitration it won (amber); ('', None)."""
    if txn is None:
        return "", None
    parts, kind = [], None
    err = error_cause(engine, txn)
    if err is not None:
        kind = "error"
        parts.append(f"<b style='color:{C['red']}'>{html.escape(err['text'])}</b>")
    evs = arbitration_events(engine, txn)
    if evs:
        kind = kind or "arbitration"
        for ev in evs[-2:]:
            lines = arbitration_details(engine, ev)
            parts.append(f"<b style='color:{C['amber']}'>{html.escape(lines[0])}</b><br>"
                         + "<br>".join(html.escape(x) for x in lines[1:]))
    return "<br>".join(parts), kind
