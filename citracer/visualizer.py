"""Render a TracerGraph as an interactive HTML file using pyvis."""
from __future__ import annotations
import html
import json
from importlib import resources
from pathlib import Path

from pyvis.network import Network

from . import keyword_matcher
from .constants import NODE_INITIAL_SIZE, NODE_ROOT_MIN_SIZE
from .models import PaperNode, TracerGraph


def _load_overlay_template() -> str:
    """Load the HTML/CSS/JS overlay template bundled with the package.

    The template uses {{PLACEHOLDER}} tokens instead of Python f-string syntax,
    so we don't have to escape every literal '{' and '}' (of which JS has
    thousands).
    """
    try:
        return resources.files("citracer").joinpath(
            "templates/overlay.html.tmpl"
        ).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        # Fallback: relative to this file (useful when running from source
        # without a proper install).
        here = Path(__file__).parent / "templates" / "overlay.html.tmpl"
        return here.read_text(encoding="utf-8")

STATUS_COLORS = {
    "root":        {"background": "#1f77b4", "border": "#0b3d61"},
    "analyzed":    {"background": "#2ca02c", "border": "#155715"},
    "no_match":    {"background": "#9e9e9e", "border": "#5a5a5a"},
    "unavailable": {"background": "#d62728", "border": "#7a1313"},
    "pending":     {"background": "#cccccc", "border": "#888888"},
}


def render(
    graph: TracerGraph,
    output: str | Path,
    keyword: str | list[str] = "",
    show_details: bool = False,
) -> Path:
    # Normalize: always work with a list of keywords internally.
    keywords: list[str] = [keyword] if isinstance(keyword, str) else list(keyword)
    keywords = [k for k in keywords if k]
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    net = Network(
        height="100vh",
        width="100%",
        directed=True,
        bgcolor="#ffffff",
        font_color="#222222",
        notebook=False,
    )
    # Layout/physics are configured via set_options below — no need to call
    # net.barnes_hut() since we're switching to a hierarchical (Sugiyama)
    # layout with the hierarchicalRepulsion solver.

    # Precompute year-based levels: oldest papers get level 0 (top of graph),
    # unknown years are pushed to the bottom. This gives the user an
    # at-a-glance way to spot which paper first introduced the concept.
    year_levels = _compute_year_levels(graph)

    # Detail payload keyed by node id, used by the click-info panel.
    node_details: dict[str, dict] = {}

    for node in graph.nodes.values():
        color = STATUS_COLORS.get(node.status, STATUS_COLORS["pending"])
        label = _short_label(node)
        payload = _node_payload(node)
        payload["depth_level"] = node.depth
        payload["year_level"] = year_levels[node.paper_id]
        node_details[node.paper_id] = payload

        # Initial size is a placeholder — actual size is computed in JS based
        # on in-degree from visible edges, so it adapts live when bibliographic
        # links are toggled.
        net.add_node(
            node.paper_id,
            label=label,
            title="",  # disable vis.js native tooltip (unstable on hover)
            color=color,
            size=NODE_INITIAL_SIZE,
            level=year_levels[node.paper_id],
            shape="dot",
            borderWidth=3 if node.status == "root" else 1,
        )

    has_secondary_edges = False
    for edge in graph.edges:
        if edge.source_id not in graph.nodes or edge.target_id not in graph.nodes:
            continue
        if edge.edge_type == "secondary":
            has_secondary_edges = True
            # physics=False keeps secondary cross-edges from distorting the
            # Sugiyama layer assignment computed from the primary edges.
            net.add_edge(
                edge.source_id,
                edge.target_id,
                title="",
                arrows="to",
                color={"color": "#5c87b5", "opacity": 0.75},
                dashes=[6, 6],
                width=1.5,
                smooth={"type": "curvedCW", "roundness": 0.25},
                physics=False,
            )
        else:
            net.add_edge(
                edge.source_id,
                edge.target_id,
                title="",
                arrows="to",
                color={"color": "#444444", "opacity": 0.9},
                width=2.5,
                smooth={"type": "cubicBezier", "forceDirection": "vertical", "roundness": 0.4},
            )

    # Initial options: Sugiyama by year, physics OFF. vis.js will place
    # nodes deterministically via the hierarchical algorithm; the JS
    # overlay then disables the hierarchy constraint so the user can drag
    # nodes freely without vis.js snapping them back to their layer.
    options = {
        "nodes": {
            "font": {
                "color": "#222222",
                "size": 14,
                "face": "system-ui, -apple-system, Segoe UI, sans-serif",
                "background": "rgba(255,255,255,0.85)",
                "strokeWidth": 0,
                "vadjust": 4,
            },
            "margin": 10,
        },
        "layout": {
            "hierarchical": {
                "enabled": True,
                "direction": "UD",
                "sortMethod": "directed",
                "shakeTowards": "leaves",
                "levelSeparation": 220,
                "nodeSpacing": 220,
                "treeSpacing": 280,
                "blockShifting": True,
                "edgeMinimization": True,
                "parentCentralization": True,
            },
        },
        "physics": {
            "enabled": False,
        },
        "interaction": {
            "hover": True,
            "hoverConnectedEdges": False,
            "navigationButtons": True,
            "tooltipDelay": 100,
            "dragNodes": True,
        },
    }
    net.set_options(json.dumps(options))

    net.write_html(str(output), notebook=False, open_browser=False)
    _inject_overlay(output, keywords, graph, node_details, has_secondary_edges)
    return output


def _compute_year_levels(graph: TracerGraph) -> dict[str, int]:
    """Assign each node a level based on the rank of its year among the
    unique years in the graph. Oldest year -> level 0 (top of the Sugiyama
    layout). Nodes without a year are pushed to a single 'unknown' level at
    the bottom.
    """
    years = sorted({n.year for n in graph.nodes.values() if n.year})
    year_to_level = {y: i for i, y in enumerate(years)}
    unknown_level = len(years)
    out: dict[str, int] = {}
    for node in graph.nodes.values():
        if node.year and node.year in year_to_level:
            out[node.paper_id] = year_to_level[node.year]
        else:
            out[node.paper_id] = unknown_level
    return out


#: Distinct highlight colours per keyword, in display order. Chosen for
#: accessibility against a white background and decent contrast with the
#: dark body text. Cycles if the user provides more than 6 keywords.
KEYWORD_HIGHLIGHT_COLORS = [
    "#fff3a0",  # soft yellow (the default single-keyword colour)
    "#c5e3a5",  # soft green
    "#ffc9b9",  # soft salmon
    "#b9d9ff",  # soft blue
    "#e0c2ff",  # soft purple
    "#ffdd99",  # soft orange
]


def _keyword_patterns_for_js(keywords: list[str] | str) -> list[dict]:
    """Return one `{keyword, pattern, color}` dict per keyword for the JS
    highlighter. Each dict carries the same morphological pattern the
    matcher uses, so highlighting agrees with what was actually matched.
    """
    if isinstance(keywords, str):
        keywords = [keywords]
    out = []
    for i, kw in enumerate(keywords):
        if not kw:
            continue
        out.append({
            "keyword": kw,
            "pattern": keyword_matcher.build_pattern(kw).pattern,
            "color": KEYWORD_HIGHLIGHT_COLORS[i % len(KEYWORD_HIGHLIGHT_COLORS)],
        })
    return out


def _node_payload(node: PaperNode) -> dict:
    return {
        "title": node.title or "(untitled)",
        "authors": node.authors,
        "year": node.year,
        "status": node.status,
        "depth": node.depth,
        "url": node.url,
        "doi": node.doi,
        "arxiv_id": node.arxiv_id,
        "abstract": node.abstract,
        "keyword_hits": node.keyword_hits,
    }


def _short_label(node) -> str:
    title = (node.title or "(untitled)").strip()
    if len(title) > 50:
        title = title[:49] + "…"
    year = f" ({node.year})" if node.year else ""
    return title + year


def _inject_overlay(
    html_path: Path,
    keywords: list[str],
    graph: TracerGraph,
    node_details: dict[str, dict],
    has_secondary_edges: bool = False,
) -> None:
    """Inject the control panel, legend, and side info panel into the pyvis
    HTML output. The template lives in templates/overlay.html.tmpl and uses
    {{PLACEHOLDER}} substitutions so we don't have to escape JS braces.
    """
    n_nodes = len(graph.nodes)
    n_edges = len(graph.edges)

    # All statuses are enabled by default — the user can toggle them off via
    # the legend if they want to focus on a subset.
    default_disabled: list[str] = []

    legend_rows = []
    for status, label in [
        ("root", "root"),
        ("analyzed", "analyzed (keyword found)"),
        ("no_match", "analyzed (no match)"),
        ("unavailable", "unavailable"),
    ]:
        bg = STATUS_COLORS[status]["background"]
        cls = "legend-item disabled" if status in default_disabled else "legend-item"
        legend_rows.append(
            f'  <div class="{cls}" data-status="{status}">'
            f'<span class="legend-dot" style="background:{bg}"></span>{label}</div>'
        )
    legend_rows_html = "\n".join(legend_rows)

    edges_legend_html = ""
    if has_secondary_edges:
        edges_legend_html = (
            '\n  <hr style="border:none;border-top:1px solid #ddd;margin:8px 0;">\n'
            '  <div style="font-size:11px;color:#888;margin-bottom:4px;">edges (click to toggle)</div>\n'
            '  <div class="legend-edge legend-edge-toggle" data-edge-type="primary">\n'
            '    <span class="edge-solid"></span>keyword-associated</div>\n'
            '  <div class="legend-edge legend-edge-toggle disabled" data-edge-type="secondary">\n'
            '    <span class="edge-dashed"></span>bibliographic link</div>'
        )

    kw_specs = _keyword_patterns_for_js(keywords)

    # Render the keyword list as tiny styled pills in the header, each
    # with its own highlight colour so the legend matches the passages.
    chip_parts = []
    for spec in kw_specs:
        chip_parts.append(
            f'<code class="keyword-chip" '
            f'style="background:{spec["color"]};color:#222;'
            f'padding:1px 6px;border-radius:3px;">'
            f'{html.escape(spec["keyword"])}</code>'
        )
    keyword_chips = " ".join(chip_parts) or '<span style="color:#888">(none)</span>'

    substitutions = {
        "{{KEYWORD_CHIPS}}":          keyword_chips,
        "{{N_NODES}}":                str(n_nodes),
        "{{N_EDGES}}":                str(n_edges),
        "{{LEGEND_ROWS}}":            legend_rows_html,
        "{{EDGES_LEGEND}}":           edges_legend_html,
        "{{NODE_DETAILS_JSON}}":      json.dumps(node_details),
        "{{KEYWORD_SPECS_JSON}}":     json.dumps(kw_specs),
        "{{DEFAULT_DISABLED_JSON}}":  json.dumps(default_disabled),
    }

    overlay = _load_overlay_template()
    for placeholder, value in substitutions.items():
        overlay = overlay.replace(placeholder, value)

    content = html_path.read_text(encoding="utf-8")
    content = content.replace("</body>", overlay + "</body>")
    html_path.write_text(content, encoding="utf-8")
