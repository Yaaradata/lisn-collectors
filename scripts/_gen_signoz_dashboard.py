"""One-shot generator for docs/deployed/signoz/dashboard_lisn_ops.json (SigNoz v6)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path


def uid() -> str:
    return str(uuid.uuid4())


def group_by(*attrs: str) -> list[dict]:
    return [
        {
            "name": a,
            "signal": "",
            "fieldContext": "attribute",
            "fieldDataType": "string",
        }
        for a in attrs
    ]


def builder_query(
    name: str,
    metric: str,
    *,
    time_agg: str,
    space_agg: str,
    reduce_to: str = "avg",
    filter_expr: str = "",
    group: list[str] | None = None,
    legend: str = "",
    disabled: bool = False,
    step: int = 60,
    limit: int | None = None,
) -> dict:
    spec: dict = {
        "name": name,
        "stepInterval": step,
        "signal": "metrics",
        "source": "",
        "aggregations": [
            {
                "metricName": metric,
                "temporality": "",
                "timeAggregation": time_agg,
                "spaceAggregation": space_agg,
                "reduceTo": reduce_to,
            }
        ],
        "disabled": disabled,
        "filter": {"expression": filter_expr},
        "groupBy": group_by(*(group or [])),
        "order": [],
        "having": {"expression": ""},
        "functions": [],
        "legend": legend,
    }
    if limit is not None:
        spec["limit"] = limit
    return {"type": "builder_query", "spec": spec}


def timeseries_plugin(*, unit: str = "none") -> dict:
    return {
        "kind": "signoz/TimeSeriesPanel",
        "spec": {
            "visualization": {"timePreference": "global_time", "fillSpans": False},
            "formatting": {"unit": unit, "decimalPrecision": "2"},
            "chartAppearance": {
                "lineInterpolation": "spline",
                "showPoints": False,
                "lineStyle": "solid",
                "fillMode": "none",
                "spanGaps": {"fillOnlyBelow": False, "fillLessThan": ""},
            },
            "axes": {"softMin": 0, "softMax": 0, "isLogScale": False},
            "legend": {"position": "bottom", "mode": "list", "customColors": None},
            "thresholds": None,
        },
    }


def number_plugin(*, unit: str = "none") -> dict:
    return {
        "kind": "signoz/NumberPanel",
        "spec": {
            "visualization": {"timePreference": "global_time"},
            "formatting": {"unit": unit, "decimalPrecision": "2"},
            "thresholds": None,
        },
    }


def panel(name: str, description: str, plugin: dict, query_kind: str, queries: list) -> dict:
    return {
        "kind": "Panel",
        "spec": {
            "display": {"name": name, "description": description or ""},
            "plugin": plugin,
            "queries": [
                {
                    "kind": query_kind,
                    "spec": {
                        "plugin": {
                            "kind": "signoz/CompositeQuery",
                            "spec": {"queries": queries},
                        }
                    },
                }
            ],
            "links": [],
        },
    }


def grid(title: str, items: list[dict]) -> dict:
    return {
        "kind": "Grid",
        "spec": {
            "display": {"title": title, "collapse": {"open": True}},
            "items": items,
        },
    }


def item(x: int, y: int, w: int, h: int, panel_id: str) -> dict:
    return {
        "x": x,
        "y": y,
        "width": w,
        "height": h,
        "content": {"$ref": f"#/spec/panels/{panel_id}"},
    }


def main() -> None:
    panels: dict[str, dict] = {}
    layouts: list[dict] = []

    p_live, p_hb, p_age = uid(), uid(), uid()
    panels[p_live] = panel(
        "lisn.workers.live (red if < 5)",
        "Heartbeating workers. THE metric for 27/29 Aug four-day silence. Expect ~5 with all workers up.",
        number_plugin(),
        "scalar",
        [
            builder_query(
                "A",
                "lisn.workers.live",
                time_agg="avg",
                space_agg="max",
                reduce_to="max",
                filter_expr="source = 'all'",
                legend="live",
            )
        ],
    )
    panels[p_hb] = panel(
        "worker heartbeat age (max, seconds)",
        (
            "Stalling worker before death. WARNING alert at > 60s. "
            "Uses lisn.worker.heartbeat_age.max (OTel histogram export), not the bare histogram name."
        ),
        number_plugin(unit="s"),
        "scalar",
        [
            builder_query(
                "A",
                "lisn.worker.heartbeat_age.max",
                time_agg="max",
                space_agg="max",
                reduce_to="max",
                legend="max_age_s",
            )
        ],
    )
    panels[p_age] = panel(
        "seconds since last completed page (per source)",
        "lisn.page.seconds_since_complete — INFO alert at > 3600s.",
        timeseries_plugin(unit="s"),
        "time_series",
        [
            builder_query(
                "A",
                "lisn.page.seconds_since_complete",
                time_agg="avg",
                space_agg="max",
                group=["source"],
                legend="{{source}}",
                limit=30,
            )
        ],
    )
    layouts.append(
        grid(
            "Row 1 — Is it alive",
            [
                item(0, 0, 3, 4, p_live),
                item(3, 0, 3, 4, p_hb),
                item(6, 0, 6, 4, p_age),
            ],
        )
    )

    p_pages, p_pending, p_calls, p_dur = uid(), uid(), uid(), uid()
    panels[p_pages] = panel(
        "lisn.pages.completed rate",
        "",
        timeseries_plugin(),
        "time_series",
        [
            builder_query(
                "A",
                "lisn.pages.completed",
                time_agg="rate",
                space_agg="sum",
                group=["source", "status"],
                legend="{{source}} {{status}}",
                limit=30,
            )
        ],
    )
    panels[p_pending] = panel(
        "lisn.jobs.pending (queue depth)",
        "",
        timeseries_plugin(),
        "time_series",
        [
            builder_query(
                "A",
                "lisn.jobs.pending",
                time_agg="avg",
                space_agg="sum",
                group=["source"],
                legend="{{source}}",
                limit=30,
            )
        ],
    )
    panels[p_calls] = panel(
        "lisn.source.calls /s (ceiling 3 for sentinel)",
        "Agreed enrichment ceiling is 3.0 req/s (3 tasks x 1/s). Discovery is slower — do not use the same ceiling.",
        timeseries_plugin(),
        "time_series",
        [
            builder_query(
                "A",
                "lisn.source.calls",
                time_agg="rate",
                space_agg="sum",
                group=["source"],
                legend="{{source}}",
                limit=30,
            )
        ],
    )
    panels[p_dur] = panel(
        "lisn.page.duration p50 / p95 by stage",
        "Histogram percentiles — query lisn.page.duration.bucket (SigNoz splits OTel histograms).",
        timeseries_plugin(unit="ms"),
        "time_series",
        [
            builder_query(
                "A",
                "lisn.page.duration.bucket",
                time_agg="",
                space_agg="p50",
                group=["stage"],
                legend="p50 {{stage}}",
                limit=30,
            ),
            builder_query(
                "B",
                "lisn.page.duration.bucket",
                time_agg="",
                space_agg="p95",
                group=["stage"],
                legend="p95 {{stage}}",
                limit=30,
            ),
        ],
    )
    layouts.append(
        grid(
            "Row 2 — Is it keeping up",
            [
                item(0, 0, 3, 6, p_pages),
                item(3, 0, 3, 6, p_pending),
                item(6, 0, 3, 6, p_calls),
                item(9, 0, 3, 6, p_dur),
            ],
        )
    )

    p_unloaded, p_gaps, p_dl, p_short = uid(), uid(), uid(), uid()
    panels[p_unloaded] = panel(
        "lisn.reconcile.unloaded (must stay 0)",
        "Raw without load — silent failure. CRITICAL if >0 for 15m.",
        timeseries_plugin(),
        "time_series",
        [
            builder_query(
                "A",
                "lisn.reconcile.unloaded",
                time_agg="avg",
                space_agg="max",
                filter_expr="source = 'all'",
                legend="unloaded",
                limit=30,
            )
        ],
    )
    panels[p_gaps] = panel(
        "lisn.discovery.gaps (must stay 0)",
        "104 incidents lost with green health surfaces. CRITICAL if >0.",
        timeseries_plugin(),
        "time_series",
        [
            builder_query(
                "A",
                "lisn.discovery.gaps",
                time_agg="avg",
                space_agg="max",
                legend="gaps",
                limit=30,
            )
        ],
    )
    panels[p_dl] = panel(
        "lisn.jobs.dead_lettered rate",
        "Empty when no pages were dead-lettered in the window — expected in healthy ops.",
        timeseries_plugin(),
        "time_series",
        [
            builder_query(
                "A",
                "lisn.jobs.dead_lettered",
                time_agg="rate",
                space_agg="sum",
                group=["source"],
                legend="{{source}}",
                limit=30,
            )
        ],
    )
    panels[p_short] = panel(
        "shortfall pages (returned < requested)",
        "Counter rate + stock gauge. Anomaly, not always error.",
        timeseries_plugin(),
        "time_series",
        [
            builder_query(
                "A",
                "lisn.pages.shortfall",
                time_agg="rate",
                space_agg="sum",
                group=["source"],
                legend="rate {{source}}",
                limit=30,
            ),
            builder_query(
                "B",
                "lisn.pages.shortfall_stock",
                time_agg="avg",
                space_agg="sum",
                group=["source"],
                legend="stock {{source}}",
                limit=30,
            ),
        ],
    )
    layouts.append(
        grid(
            "Row 3 — Is it losing anything",
            [
                item(0, 0, 3, 6, p_unloaded),
                item(3, 0, 3, 6, p_gaps),
                item(6, 0, 3, 6, p_dl),
                item(9, 0, 3, 6, p_short),
            ],
        )
    )

    p_lat, p_status, p_retry = uid(), uid(), uid()
    panels[p_lat] = panel(
        "lisn.source.latency p50 / p95",
        (
            "Real answer to how fast Sentinel is (mock to real). "
            "Uses lisn.source.latency.bucket for histogram percentiles."
        ),
        timeseries_plugin(unit="ms"),
        "time_series",
        [
            builder_query(
                "A",
                "lisn.source.latency.bucket",
                time_agg="",
                space_agg="p50",
                group=["source"],
                legend="p50 {{source}}",
                limit=30,
            ),
            builder_query(
                "B",
                "lisn.source.latency.bucket",
                time_agg="",
                space_agg="p95",
                group=["source"],
                legend="p95 {{source}}",
                limit=30,
            ),
        ],
    )
    panels[p_status] = panel(
        "lisn.source.calls by HTTP status",
        "Group by http.status_code — matches OTel attribute from collector/metrics.py.",
        timeseries_plugin(),
        "time_series",
        [
            builder_query(
                "A",
                "lisn.source.calls",
                time_agg="rate",
                space_agg="sum",
                group=["source", "http.status_code"],
                legend="{{source}} {{http.status_code}}",
                limit=30,
            )
        ],
    )
    panels[p_retry] = panel(
        "retry rate (pages.completed status=failed)",
        (
            "Failed attempts before dead-letter. Correlates with WARNING retries in logs. "
            "Empty when every page succeeded — expected after clean collects."
        ),
        timeseries_plugin(),
        "time_series",
        [
            builder_query(
                "A",
                "lisn.pages.completed",
                time_agg="rate",
                space_agg="sum",
                filter_expr="status = 'failed'",
                group=["source"],
                legend="retry {{source}}",
                limit=30,
            )
        ],
    )
    layouts.append(
        grid(
            "Row 4 — What is the source doing",
            [
                item(0, 0, 4, 6, p_lat),
                item(4, 0, 4, 6, p_status),
                item(8, 0, 4, 6, p_retry),
            ],
        )
    )

    doc = {
        "schemaVersion": "v6",
        "image": "/assets/Icons/eight-ball",
        "generateName": True,
        "tags": [
            {"key": "tag", "value": "lisn"},
            {"key": "tag", "value": "collector"},
            {"key": "tag", "value": "pilot"},
        ],
        "spec": {
            "display": {
                "name": "LiSN collector — is it working right now",
                "description": (
                    "Four-row ops surface: alive / keeping up / losing anything / source. "
                    "Thresholds and ceilings match the acceptance-run failures. "
                    "SigNoz Cloud / community schemaVersion v6 (Perses)."
                ),
            },
            "variables": [],
            "panels": panels,
            "layouts": layouts,
            "duration": "1h",
            "refreshInterval": "30s",
            "links": [],
        },
    }

    out = Path(__file__).resolve().parents[1] / "docs/deployed/signoz/dashboard_lisn_ops.json"
    out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["schemaVersion"] == "v6"
    assert "title" not in loaded
    assert len(loaded["spec"]["panels"]) == 14
    assert len(loaded["spec"]["layouts"]) == 4
    print(f"wrote {out} panels={len(panels)} bytes={out.stat().st_size}")


if __name__ == "__main__":
    main()
