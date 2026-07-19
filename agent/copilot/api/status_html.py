"""Human-facing HTML for the ``/ready`` and ``/v1/status`` pages.

These two routes are *machine contracts* first: the deploy readiness gate, the
OpenAPI contract test, and the frozen acceptance suite all parse their JSON and
must keep working byte-for-byte. This module adds a purely additive, browser-only
HTML rendering on top of that contract via **content negotiation** — it never
changes what a programmatic consumer receives.

The rule (:func:`prefers_html`): a client gets the HTML page only when its
``Accept`` header ranks ``text/html`` strictly above ``application/json`` — which
is what a browser sends (``text/html`` at ``q=1.0`` vs ``application/json`` only
via ``*/*;q=0.8``). Everything else — an explicit ``application/json``, the
``*/*`` that curl / httpx / Starlette ``TestClient`` send by default, or no
``Accept`` at all — resolves to the *existing* JSON response, unchanged in status
code, body bytes, and ``content-type``. When in doubt we serve JSON, because JSON
is the contract and HTML is the convenience.

The pages are fully self-contained: one inline ``<style>`` block, no external
CSS / JS / fonts / images / CDNs, and no inline JavaScript event handlers. The
deploy sits behind Caddy with no host ports and blocks external assets; the
project sets no ``Content-Security-Policy`` (verified against ``Caddyfile*`` and
the app middleware), so an inline ``<style>`` renders, and the page is written to
stay valid even under a strict ``style-src 'self' 'unsafe-inline'`` policy should
one ever be added. Both pages are responsive and adapt to the viewer's
light/dark preference via ``prefers-color-scheme``.

@package   OpenEMR
@link      https://www.open-emr.org
@license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import Any

from fastapi import Response
from fastapi.responses import HTMLResponse, JSONResponse

from copilot.domain.contracts import ReadinessDependency, ReadinessResponse

# --- Content negotiation -----------------------------------------------------

_HTML_MEDIA_TYPE = "text/html"
_JSON_MEDIA_TYPE = "application/json"


def _media_quality(accept: str, media_type: str) -> float:
    """The best ``q`` value an ``Accept`` header assigns to ``media_type``.

    Honours exact matches (``text/html``), type wildcards (``text/*``), and the
    full wildcard (``*/*`` / ``*``), taking the highest ``q`` among matching
    ranges. Returns ``0.0`` when nothing matches (including an empty/absent
    header), so an unmatched type never appears preferred.
    """
    main_type, _, _sub_type = media_type.partition("/")
    best = 0.0
    matched = False
    for part in accept.split(","):
        token = part.strip()
        if not token:
            continue
        segments = token.split(";")
        candidate = segments[0].strip().lower()
        quality = 1.0
        for param in segments[1:]:
            key, sep, value = param.strip().partition("=")
            if sep and key.strip().lower() == "q":
                try:
                    quality = float(value.strip())
                except ValueError:
                    quality = 0.0
        candidate_main, _, candidate_sub = candidate.partition("/")
        matches = (
            candidate == media_type
            or (candidate_main == main_type and candidate_sub == "*")
            or candidate in ("*/*", "*")
        )
        if matches:
            matched = True
            best = max(best, quality)
    return best if matched else 0.0


def prefers_html(accept: str) -> bool:
    """True only when the client ranks ``text/html`` strictly above JSON.

    A browser (``text/html`` at ``q=1.0``; JSON reachable only through
    ``*/*;q=0.8``) is the sole common client for which this holds. ``*/*``,
    ``application/json``, and an absent header all tie or favour JSON, so they
    keep the machine contract.
    """
    return _media_quality(accept, _HTML_MEDIA_TYPE) > _media_quality(accept, _JSON_MEDIA_TYPE)


# --- Response builders (keep the route handlers thin) ------------------------


def ready_response(
    payload: ReadinessResponse, accept: str, correlation_id: str | None
) -> Response:
    """JSON readiness contract, or the HTML page when the client prefers HTML.

    The JSON branch is byte-identical to the pre-HTML handler: same status code
    (200/503) and the same ``model_dump(mode="json")`` body.
    """
    if not prefers_html(accept):
        return JSONResponse(
            status_code=payload.to_status_code(),
            content=payload.model_dump(mode="json"),
        )
    return HTMLResponse(
        content=render_ready_html(
            payload, correlation_id=correlation_id, generated_at=datetime.now(UTC)
        ),
        status_code=payload.to_status_code(),
    )


def status_response(payload: dict[str, Any], accept: str) -> Response:
    """JSON status aggregates, or the HTML page when the client prefers HTML.

    The JSON branch serialises the exact ``dict`` the handler already computed —
    the same bytes FastAPI produced before, since the payload is composed solely
    of JSON-native values.
    """
    if not prefers_html(accept):
        return JSONResponse(content=payload)
    return HTMLResponse(content=render_status_html(payload, generated_at=datetime.now(UTC)))


# --- Shared HTML scaffolding -------------------------------------------------

_STYLE = """
:root {
  color-scheme: light dark;
  --bg: #f4f6fb;
  --panel: #ffffff;
  --panel-2: #f8fafc;
  --border: #e2e8f0;
  --text: #0f172a;
  --muted: #64748b;
  --accent: #2563eb;
  --ok-bg: #dcfce7; --ok-fg: #166534; --ok-line: #22c55e;
  --warn-bg: #fef3c7; --warn-fg: #92400e; --warn-line: #f59e0b;
  --down-bg: #fee2e2; --down-fg: #991b1b; --down-line: #ef4444;
  --info-bg: #e0e7ff; --info-fg: #3730a3;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0b1120;
    --panel: #111827;
    --panel-2: #0f172a;
    --border: #1f2937;
    --text: #e5e7eb;
    --muted: #94a3b8;
    --accent: #60a5fa;
    --ok-bg: #052e1a; --ok-fg: #86efac; --ok-line: #22c55e;
    --warn-bg: #3a2a06; --warn-fg: #fcd34d; --warn-line: #f59e0b;
    --down-bg: #3b0d0d; --down-fg: #fca5a5; --down-line: #ef4444;
    --info-bg: #1e1b4b; --info-fg: #c7d2fe;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 1.5rem 1rem 3rem;
  background: var(--bg);
  color: var(--text);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  line-height: 1.5;
  -webkit-text-size-adjust: 100%;
}
.wrap { max-width: 880px; margin: 0 auto; }
header.page { margin-bottom: 1.25rem; }
h1 { font-size: 1.5rem; margin: 0 0 0.25rem; letter-spacing: -0.01em; }
.sub { color: var(--muted); font-size: 0.9rem; margin: 0; }
.banner {
  display: flex; align-items: center; gap: 0.75rem;
  padding: 1rem 1.25rem; border-radius: 12px; margin: 1rem 0 1.25rem;
  border: 1px solid var(--border); font-weight: 600; font-size: 1.1rem;
}
.banner .dot { width: 0.85rem; height: 0.85rem; border-radius: 50%; flex: none; }
.banner.ready { background: var(--ok-bg); color: var(--ok-fg); }
.banner.ready .dot { background: var(--ok-line); }
.banner.notready { background: var(--down-bg); color: var(--down-fg); }
.banner.notready .dot { background: var(--down-line); }
.meta { display: flex; flex-wrap: wrap; gap: 0.5rem 1.5rem; margin: 0 0 1.5rem; padding: 0; list-style: none; }
.meta li { font-size: 0.82rem; color: var(--muted); }
.meta b { color: var(--text); font-weight: 600; }
.meta code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.8rem; }
.grid { display: grid; grid-template-columns: 1fr; gap: 0.75rem; }
.card {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 12px; padding: 1rem 1.15rem;
}
.dep { border-left: 4px solid var(--border); }
.dep.ok { border-left-color: var(--ok-line); }
.dep.degraded { border-left-color: var(--warn-line); }
.dep.down { border-left-color: var(--down-line); }
.dep-head { display: flex; align-items: center; gap: 0.6rem; flex-wrap: wrap; }
.dep-name { font-weight: 600; font-size: 1rem; }
.pill {
  display: inline-block; padding: 0.12rem 0.6rem; border-radius: 999px;
  font-size: 0.72rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em;
}
.pill.ok { background: var(--ok-bg); color: var(--ok-fg); }
.pill.degraded { background: var(--warn-bg); color: var(--warn-fg); }
.pill.down { background: var(--down-bg); color: var(--down-fg); }
.tag {
  display: inline-block; padding: 0.1rem 0.5rem; border-radius: 6px;
  font-size: 0.68rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em;
  background: var(--info-bg); color: var(--info-fg);
}
.detail { margin: 0.5rem 0 0; color: var(--muted); font-size: 0.88rem; word-break: break-word; }
h2.section { font-size: 1.05rem; margin: 1.75rem 0 0.75rem; }
.metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 0.75rem; }
.metric .label { font-size: 0.78rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
.metric .value { font-size: 1.6rem; font-weight: 700; margin: 0.15rem 0; letter-spacing: -0.01em; }
.metric .value.na { font-size: 1.1rem; color: var(--muted); font-weight: 600; }
.prov { margin: 0.4rem 0 0; font-size: 0.76rem; color: var(--muted); word-break: break-word; }
.prov .kind {
  display: inline-block; margin-right: 0.4rem; padding: 0.02rem 0.42rem; border-radius: 5px;
  font-size: 0.66rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em;
}
.kind.measured { background: var(--ok-bg); color: var(--ok-fg); }
.kind.recorded { background: var(--info-bg); color: var(--info-fg); }
.kind.unavailable { background: var(--warn-bg); color: var(--warn-fg); }
table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--border); }
th { font-size: 0.74rem; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.empty { color: var(--muted); font-size: 0.88rem; font-style: italic; }
.table-scroll { overflow-x: auto; }
footer.page { margin-top: 2rem; color: var(--muted); font-size: 0.78rem; }
"""


def _document(title: str, body: str) -> str:
    """Wrap page ``body`` in a complete, self-contained HTML5 document."""
    safe_title = html.escape(title)
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="robots" content="noindex">\n'
        f"<title>{safe_title}</title>\n"
        f"<style>{_STYLE}</style>\n"
        "</head>\n"
        '<body>\n<div class="wrap">\n'
        f"{body}\n"
        "</div>\n</body>\n</html>\n"
    )


def _fmt_timestamp(moment: datetime) -> str:
    """UTC instant to a compact, unambiguous human string."""
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC")


# --- Readiness page ----------------------------------------------------------


def _dependency_card(dep: ReadinessDependency) -> str:
    grade = dep.status  # "ok" | "degraded" | "down"
    name = html.escape(dep.name)
    pill = f'<span class="pill {grade}">{html.escape(grade)}</span>'
    advisory = '<span class="tag">advisory</span>' if dep.advisory else ""
    detail = (
        f'<p class="detail">{html.escape(dep.detail)}</p>' if dep.detail.strip() else ""
    )
    return (
        f'<div class="card dep {grade}">'
        f'<div class="dep-head"><span class="dep-name">{name}</span>{pill}{advisory}</div>'
        f"{detail}"
        "</div>"
    )


def render_ready_html(
    payload: ReadinessResponse, correlation_id: str | None, generated_at: datetime
) -> str:
    """The ``/ready`` HTML dashboard, rendered from the same payload the JSON uses."""
    ready = payload.ready
    banner_class = "ready" if ready else "notready"
    banner_text = "READY" if ready else "NOT READY"

    migration = next((d for d in payload.dependencies if d.name == "migrations"), None)
    migration_head = migration.detail if migration is not None and migration.detail else "unknown"

    meta_items = [
        f"<li><b>Schema:</b> {html.escape(migration_head)}</li>",
        f"<li><b>Generated:</b> {html.escape(_fmt_timestamp(generated_at))}</li>",
    ]
    if correlation_id:
        meta_items.append(
            f"<li><b>Correlation ID:</b> <code>{html.escape(correlation_id)}</code></li>"
        )

    cards = "\n".join(_dependency_card(dep) for dep in payload.dependencies)
    if not cards:
        cards = '<p class="empty">No dependencies were probed.</p>'

    body = (
        '<header class="page">'
        "<h1>Clinical Co-Pilot &mdash; Readiness</h1>"
        '<p class="sub">Deployment dependency health for the agent service.</p>'
        "</header>"
        f'<div class="banner {banner_class}"><span class="dot"></span>{banner_text}</div>'
        f'<ul class="meta">{"".join(meta_items)}</ul>'
        f'<div class="grid">{cards}</div>'
        '<footer class="page">Advisory dependencies are shown for visibility and never '
        "gate readiness. This page mirrors the JSON at this URL; request it with "
        "<code>Accept: application/json</code> for the machine contract.</footer>"
    )
    return _document(f"Readiness — {banner_text}", body)


# --- Status page -------------------------------------------------------------


def _as_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _pct(fraction: float) -> str:
    return f"{fraction * 100:.1f}%"


def _provenance(sources: dict[str, str], key: str) -> str:
    """Render the ``metric_sources`` provenance label for ``key`` as a footer line."""
    label = sources.get(key, "")
    if not label:
        return ""
    kind = label.split(":", 1)[0].strip().lower()
    badge = ""
    if kind in ("measured", "recorded", "unavailable"):
        badge = f'<span class="kind {kind}">{html.escape(kind)}</span>'
        remainder = label.split(":", 1)[1].strip()
    else:
        remainder = label
    return f'<p class="prov">{badge}{html.escape(remainder)}</p>'


def _metric_card(label: str, value_html: str, sources: dict[str, str], key: str) -> str:
    return (
        '<div class="card metric">'
        f'<div class="label">{html.escape(label)}</div>'
        f"{value_html}"
        f"{_provenance(sources, key)}"
        "</div>"
    )


def _routing_table(routing: dict[str, Any]) -> str:
    rows = []
    for action, count in routing.items():
        count_int = _as_int(count)
        count_text = str(count_int) if count_int is not None else html.escape(str(count))
        rows.append(
            f"<tr><td>{html.escape(str(action))}</td>"
            f'<td class="num">{count_text}</td></tr>'
        )
    if not rows:
        return '<p class="empty">No routing decisions recorded yet.</p>'
    return (
        '<div class="table-scroll"><table>'
        '<thead><tr><th>Decision</th><th class="num">Count</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _eval_table(eval_by_category: dict[str, Any]) -> str:
    rows = []
    for name, node in eval_by_category.items():
        if not isinstance(node, dict):
            continue
        passed = _as_number(node.get("passed"))
        total = _as_number(node.get("total"))
        rate = _as_number(node.get("pass_rate"))
        passed_text = f"{passed:.0f}" if passed is not None else "&mdash;"
        total_text = f"{total:.0f}" if total is not None else "&mdash;"
        rate_text = _pct(rate) if rate is not None else "&mdash;"
        rows.append(
            f"<tr><td>{html.escape(str(name))}</td>"
            f'<td class="num">{passed_text}</td>'
            f'<td class="num">{total_text}</td>'
            f'<td class="num">{rate_text}</td></tr>'
        )
    if not rows:
        return '<p class="empty">No eval categories recorded.</p>'
    return (
        '<div class="table-scroll"><table>'
        '<thead><tr><th>Rubric</th><th class="num">Passed</th>'
        '<th class="num">Total</th><th class="num">Pass rate</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def render_status_html(payload: dict[str, Any], generated_at: datetime) -> str:
    """The ``/v1/status`` HTML dashboard, rendered from the same payload the JSON uses."""
    sources_raw = payload.get("metric_sources")
    sources: dict[str, str] = {}
    if isinstance(sources_raw, dict):
        for key, value in sources_raw.items():
            if isinstance(key, str) and isinstance(value, str):
                sources[key] = value

    ingestion = _as_int(payload.get("ingestion_count"))
    ingestion_html = (
        f'<div class="value">{ingestion}</div>'
        if ingestion is not None
        else '<div class="value na">n/a</div>'
    )

    error_rate = _as_number(payload.get("error_rate"))
    error_html = (
        f'<div class="value">{_pct(error_rate)}</div>'
        if error_rate is not None
        else '<div class="value na">n/a</div>'
    )

    extraction = _as_number(payload.get("extraction_field_pass_rate"))
    extraction_html = (
        f'<div class="value">{_pct(extraction)}</div>'
        if extraction is not None
        else '<div class="value na">n/a</div>'
    )

    # Retrieval hit rate is a contract placeholder, not a measurement — surface it
    # as "not recorded" rather than a misleading numeral, per its provenance label.
    retrieval_available = payload.get("retrieval_hit_rate_available") is True
    if retrieval_available:
        retrieval_value = _as_number(payload.get("retrieval_hit_rate"))
        retrieval_html = (
            f'<div class="value">{_pct(retrieval_value)}</div>'
            if retrieval_value is not None
            else '<div class="value na">n/a</div>'
        )
    else:
        retrieval_html = '<div class="value na">not recorded</div>'

    latency_raw = payload.get("latency_ms")
    if isinstance(latency_raw, dict):
        p50 = _as_number(latency_raw.get("p50"))
        p95 = _as_number(latency_raw.get("p95"))
    else:
        p50 = p95 = None
    p50_text = f"{p50:.1f}" if p50 is not None else "n/a"
    p95_text = f"{p95:.1f}" if p95 is not None else "n/a"
    latency_html = (
        f'<div class="value">{p95_text}<span class="label"> ms p95</span></div>'
        f'<div class="prov">p50 {p50_text} ms</div>'
    )

    routing_raw = payload.get("routing_decisions")
    routing = routing_raw if isinstance(routing_raw, dict) else {}

    eval_raw = payload.get("eval_by_category")
    eval_by_category = eval_raw if isinstance(eval_raw, dict) else {}

    dataset_raw = payload.get("eval_dataset")
    dataset = dataset_raw if isinstance(dataset_raw, dict) else {}
    dataset_name = dataset.get("name")
    dataset_cases = _as_int(dataset.get("case_count"))
    dataset_rate = _as_number(dataset.get("pass_rate"))
    dataset_captured = dataset.get("captured_at")
    dataset_bits = []
    if isinstance(dataset_name, str) and dataset_name:
        dataset_bits.append(f"<li><b>Dataset:</b> {html.escape(dataset_name)}</li>")
    if dataset_cases is not None:
        dataset_bits.append(f"<li><b>Cases:</b> {dataset_cases}</li>")
    if dataset_rate is not None:
        dataset_bits.append(f"<li><b>Overall pass rate:</b> {_pct(dataset_rate)}</li>")
    if isinstance(dataset_captured, str) and dataset_captured:
        dataset_bits.append(f"<li><b>Captured:</b> {html.escape(dataset_captured)}</li>")
    dataset_meta = f'<ul class="meta">{"".join(dataset_bits)}</ul>' if dataset_bits else ""

    metric_cards = "".join(
        [
            _metric_card("Documents ingested", ingestion_html, sources, "ingestion_count"),
            _metric_card("Ingestion error rate", error_html, sources, "error_rate"),
            _metric_card(
                "Extraction field pass rate",
                extraction_html,
                sources,
                "extraction_field_pass_rate",
            ),
            _metric_card("Retrieval hit rate", retrieval_html, sources, "retrieval_hit_rate"),
            _metric_card("Latency", latency_html, sources, "latency_ms"),
        ]
    )

    body = (
        '<header class="page">'
        "<h1>Clinical Co-Pilot &mdash; Status</h1>"
        '<p class="sub">Agent health aggregates from the agent database and committed '
        "eval artifacts. Each metric carries its provenance.</p>"
        "</header>"
        f'<ul class="meta"><li><b>Generated:</b> {html.escape(_fmt_timestamp(generated_at))}</li></ul>'
        f'<div class="metrics">{metric_cards}</div>'
        '<h2 class="section">Routing decisions</h2>'
        f'<div class="card">{_routing_table(routing)}{_provenance(sources, "routing_decisions")}</div>'
        '<h2 class="section">Eval by rubric</h2>'
        f'<div class="card">{dataset_meta}{_eval_table(eval_by_category)}'
        f'{_provenance(sources, "eval_by_category")}</div>'
        '<footer class="page">Provenance: <b>measured</b> = computed from live agent-DB '
        "rows per request; <b>recorded</b> = read from a committed offline artifact; "
        "<b>unavailable</b> = not recorded, so no number is published. This page mirrors "
        "the JSON at this URL; request it with <code>Accept: application/json</code> for "
        "the machine contract.</footer>"
    )
    return _document("Clinical Co-Pilot — Status", body)
