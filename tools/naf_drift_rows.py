#!/usr/bin/env python3
"""Regenerate the NAF workshop rows at the bottom of naf/dashboard-naf.json.

Idempotent: removes everything from the first NAF row ("Drift (NAF loop)" or "NAF ·") to the end
of the dashboard, then appends the rows below. Panels above (device health, interfaces, traffic,
admin-up/oper-down drift) are never touched.

    python3 tools/naf_drift_rows.py && tar czf naf.tgz naf/definition.json naf/crpd_naf.json \
        naf/csrx_naf.json naf/dashboard-naf.json naf/commit_change.tick && cp naf.tgz ~/JTS/compose/jtso/profiles/

Data sources (all InfluxDB jtsdb, datasource uid jtsuid):
  openJTS / naf profile : NETWORK_INSTANCE_BGP, COMMIT, NAF_CONFIG, NAF_TRACE, NAF_LOGFILES
  NAF workshop code     : naf_intent (seed_intent), naf_event (orchestrator), naf_commit /
                          naf_secpolicy (netconf_poller), naf_syslog (syslog Collector swap)
"""
from __future__ import annotations

import json
from pathlib import Path

DASH = Path(__file__).resolve().parent.parent / "naf" / "dashboard-naf.json"
DS = {"type": "influxdb", "uid": "jtsuid"}
DEV = '"SHORTNAME" =~ /^$device$/'
NOW = "time > now() - 90s"  # "current" config view: absent leaves must not show stale values


class Layout:
    def __init__(self, y: int, first_id: int):
        self.y, self.x, self.row_h, self.id = y, 0, 0, first_id
        self.panels: list[dict] = []

    def _next_id(self) -> int:
        self.id += 1
        return self.id - 1

    def row(self, title: str) -> None:
        self.newline()
        self.panels.append({"type": "row", "title": title, "id": self._next_id(), "collapsed": False,
                            "panels": [], "gridPos": {"h": 1, "w": 24, "x": 0, "y": self.y}})
        self.y += 1

    def newline(self) -> None:
        if self.x:
            self.y += self.row_h
        self.x, self.row_h = 0, 0

    def add(self, panel: dict, w: int, h: int) -> None:
        if self.x + w > 24:
            self.newline()
        panel.update({"id": self._next_id(), "gridPos": {"h": h, "w": w, "x": self.x, "y": self.y}})
        panel.setdefault("datasource", DS)
        panel.setdefault("pluginVersion", "10.3.1")
        self.panels.append(panel)
        self.x += w
        self.row_h = max(self.row_h, h)


def q(ref: str, query: str, fmt: str = "table", alias: str | None = None) -> dict:
    t = {"datasource": DS, "query": query, "rawQuery": True, "refId": ref, "resultFormat": fmt}
    if alias:
        t["alias"] = alias
    return t


def table(title: str, desc: str, targets: list, transformations: list | None = None, overrides: list | None = None,
          no_value: str | None = None) -> dict:
    defaults: dict = {"unit": "none", "custom": {"align": "auto", "cellOptions": {"type": "auto"}}}
    overrides = list(overrides or [])
    if no_value:
        # Grafana applies defaults.noValue to every empty cell too; keep the message for an empty
        # panel only, and show a dash in empty cells (later overrides still win).
        defaults["noValue"] = no_value
        overrides.insert(0, {"matcher": {"id": "byRegexp", "options": ".*"}, "properties": [{"id": "noValue", "value": "—"}]})
    return {"type": "table", "title": title, "description": desc, "targets": targets,
            "transformations": transformations or [{"id": "merge", "options": {}}],
            "fieldConfig": {"defaults": defaults, "overrides": overrides},
            "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False, "reducer": ["sum"], "fields": ""}}}


def timeseries(title: str, desc: str, targets: list, unit: str = "none", thresholds: list | None = None,
               decimals: int | None = None) -> dict:
    steps = thresholds or [{"color": "green", "value": None}]
    custom = {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 10, "showPoints": "never", "spanNulls": True,
              "thresholdsStyle": {"mode": "line+area" if thresholds else "off"}}
    defaults = {"unit": unit, "min": 0, "color": {"mode": "palette-classic"},
                "thresholds": {"mode": "absolute", "steps": steps}, "custom": custom}
    if decimals is not None:
        defaults["decimals"] = decimals
    return {"type": "timeseries", "title": title, "description": desc, "targets": targets,
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"legend": {"showLegend": True, "displayMode": "list", "placement": "bottom",
                                   "calcs": ["lastNotNull", "max"]}, "tooltip": {"mode": "multi", "sort": "desc"}}}


def color_map(field: str, values: dict[str, str], other: str | None = None) -> dict:
    mappings: list = []
    if values:
        mappings.append({"type": "value", "options": {v: {"color": c, "index": i} for i, (v, c) in enumerate(values.items())}})
    if other:
        mappings.append({"type": "regex", "options": {"pattern": ".+", "result": {"color": other, "index": len(values)}}})
    return {"matcher": {"id": "byName", "options": field}, "properties": [
        {"id": "mappings", "value": mappings}, {"id": "custom.cellOptions", "value": {"type": "color-text"}}]}


LEVEL_MAP = {"OK": "green", "INFO": "blue", "WARNING": "orange", "CRITICAL": "red"}
STATE_TL_OPTS = {"showValue": "auto", "mergeValues": True, "rowHeight": 0.8, "alignValue": "left",
                 "legend": {"showLegend": False, "displayMode": "list", "placement": "bottom"},
                 "tooltip": {"mode": "single", "sort": "none"}}


def bgp_session_state(desc: str) -> dict:
    """BGP session state timeline per device and neighbour (used by bgp-peer-drift and prefix-limit)."""
    return {"type": "state-timeline", "title": "BGP session state", "description": desc,
            "targets": [q("A", f'SELECT last("session_state") FROM "NETWORK_INSTANCE_BGP" WHERE {DEV} AND $timeFilter GROUP BY time($__interval), "SHORTNAME", "neighbor_address" fill(previous)',
                          "time_series", "$tag_SHORTNAME → $tag_neighbor_address")],
            "fieldConfig": {"defaults": {"color": {"mode": "thresholds"}, "thresholds": {"mode": "absolute", "steps": [{"color": "red", "value": None}]},
                                         "mappings": [{"type": "value", "options": {"ESTABLISHED": {"color": "green", "index": 0}}}],
                                         "custom": {"fillOpacity": 80, "lineWidth": 0}}, "overrides": []},
            "options": STATE_TL_OPTS}


def build(y: int, first_id: int) -> list[dict]:
    L = Layout(y, first_id)

    # ---------------- orchestrator ----------------
    L.row("NAF loop · Orchestrator decisions (all use cases)")
    L.add({"type": "stat", "title": "Autonomy level",
           "description": "Autonomy of the orchestrator at its latest decision (naf_event.autonomy_level). Change it with PUT /autonomy?level=L1|L2|L3.",
           "targets": [q("A", 'SELECT last("autonomy_level") FROM "naf_event" WHERE time > now() - 30d', "time_series")],
           "fieldConfig": {"defaults": {"noValue": "orchestrator not seen", "unit": "none",
                                        "mappings": [{"type": "value", "options": {
                                            "1": {"text": "L1 Observe", "color": "blue", "index": 0},
                                            "2": {"text": "L2 Assist", "color": "orange", "index": 1},
                                            "3": {"text": "L3 Autonomous", "color": "purple", "index": 2}}}],
                                        "color": {"mode": "thresholds"},
                                        "thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": None}]}}, "overrides": []},
           "options": {"colorMode": "background", "graphMode": "none", "justifyMode": "center", "textMode": "value",
                       "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}}, 5, 7)
    L.add({"type": "bargauge", "title": "Decisions by kind",
           "description": "Orchestrator event-log kinds in the selected time range (notified, pending, remediated, no-drift, within-policy, recovered, ...).",
           "targets": [q("A", 'SELECT count("count") FROM "naf_event" WHERE $timeFilter GROUP BY "kind"', "time_series", "$tag_kind")],
           "fieldConfig": {"defaults": {"unit": "none", "decimals": 0, "min": 0, "color": {"mode": "palette-classic"},
                                        "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}]}}, "overrides": []},
           "options": {"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True, "valueMode": "color",
                       "reduceOptions": {"calcs": ["sum"], "fields": "", "values": False}}}, 7, 7)
    L.add({"type": "annolist", "title": "Remediations (annotations)",
           "description": "Grafana annotations posted by the orchestrator on every 'remediated' event (GRAFANA_ANNOTATIONS=true), tag naf.",
           "options": {"onlyFromThisDashboard": False, "onlyInTimeRange": False, "tags": ["naf"], "limit": 10,
                       "showUser": False, "showTime": True, "showTags": True, "navigateToPanel": False,
                       "navigateBefore": "10m", "navigateAfter": "10m"}}, 12, 7)
    L.add({"type": "state-timeline", "title": "Alert level per alert type and device",
           "description": "Level of the latest Kapacitor alert handled by the orchestrator (naf_event.level), per alert type "
                          "(first part of the alert id) / device. config-change means 'a commit happened'; the logging-drift "
                          "planner checks it. Decisions where the planner found nothing to fix (no-drift, within-policy) are left out.",
           "targets": [q("A", 'SELECT last("level") FROM "naf_event" WHERE $timeFilter AND "event" != \'\' '
                              'AND "kind" !~ /^(autonomy-changed|no-drift|within-policy)$/ '
                              'GROUP BY time($__interval), "event", "device" fill(previous)', "time_series", "$tag_event · $tag_device")],
           "fieldConfig": {"defaults": {"color": {"mode": "thresholds"}, "thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": None}]},
                                        "mappings": [{"type": "value", "options": {k: {"color": c, "index": i} for i, (k, c) in enumerate(LEVEL_MAP.items())}}],
                                        "custom": {"fillOpacity": 80, "lineWidth": 0}}, "overrides": []},
           "options": STATE_TL_OPTS}, 24, 6)
    L.add(table("Decision log (latest first)",
                "Every orchestrator decision (naf_event, ORCH_INFLUX_EVENTS=true): kind, alert id and level, autonomy, the executor and intent backend that acted.",
                [q("A", 'SELECT "kind", "event_id", "level", "autonomy", "executor", "intent_backend", "compensating" FROM "naf_event" '
                        'WHERE $timeFilter ORDER BY time DESC LIMIT 50')],
                [{"id": "organize", "options": {"renameByName": {"event_id": "Alert id", "kind": "Decision", "level": "Level",
                                                                  "autonomy": "Autonomy", "executor": "Executor",
                                                                  "intent_backend": "Intent", "compensating": "Compensating"}}}],
                [color_map("Level", LEVEL_MAP),
                 color_map("Decision", {"remediated": "purple", "pending": "orange", "notified": "blue", "no-drift": "green",
                                        "within-policy": "green", "recovered": "green", "rejected": "red", "error": "red"}),
                 {"matcher": {"id": "byName", "options": "Time"}, "properties": [{"id": "unit", "value": "dateTimeAsIso"}, {"id": "custom.width", "value": 190}]}],
                no_value="no decisions in range"), 24, 8)

    # ---------------- bgp-peer-drift ----------------
    L.row("NAF · bgp-peer-drift")
    L.add(table("BGP peer-as: actual vs intent",
                "Actual = openJTS gNMI NETWORK_INSTANCE_BGP.session_peer_as; intent = naf_intent (usecase=bgp-peer-drift). Matched on the neighbour. Kapacitor task bgp_peer_drift.",
                [q("A", f'SELECT last("session_peer_as") AS "Actual peer-as" FROM "NETWORK_INSTANCE_BGP" WHERE {DEV} AND $timeFilter GROUP BY "SHORTNAME", "neighbor_address"'),
                 q("B", f'SELECT last("session_state") AS "State" FROM "NETWORK_INSTANCE_BGP" WHERE {DEV} AND time > now() - 30d GROUP BY "neighbor_address"'),
                 q("C", 'SELECT last("peer_as") AS "Intended peer-as" FROM "naf_intent" WHERE "usecase" = \'bgp-peer-drift\' GROUP BY "device", "neighbor_address"')],
                [{"id": "organize", "options": {"excludeByName": {"Time": True}}},
                 {"id": "merge", "options": {}},
                 {"id": "filterByValue", "options": {"filters": [{"config": {"id": "isNotNull", "options": {}}, "fieldName": "SHORTNAME"}], "match": "all", "type": "include"}},
                 {"id": "calculateField", "options": {"alias": "Drift", "binary": {"left": "Actual peer-as", "operator": "-", "right": "Intended peer-as"}, "mode": "binary", "reduce": {"reducer": "sum"}}},
                 {"id": "organize", "options": {"excludeByName": {"device": True},
                                                "indexByName": {"SHORTNAME": 0, "neighbor_address": 1, "State": 2, "Actual peer-as": 3, "Intended peer-as": 4, "Drift": 5},
                                                "renameByName": {"SHORTNAME": "Device", "neighbor_address": "Neighbor"}}}],
                [{"matcher": {"id": "byName", "options": "Drift"}, "properties": [
                    {"id": "mappings", "value": [{"type": "value", "options": {"0": {"text": "in sync", "color": "green", "index": 0}}},
                                                 {"type": "range", "options": {"from": -1e12, "to": 1e12, "result": {"text": "DRIFT", "color": "red", "index": 1}}}]},
                    {"id": "custom.cellOptions", "value": {"type": "color-background"}}]},
                 color_map("State", {"ESTABLISHED": "green"}, other="red")]), 14, 6)
    L.add(table("BGP group config (NETCONF)",
                "naf profile NAF_CONFIG (get-configuration protocols bgp, 30 s): configured peer-as and import policy per group. Last 90 s only, so removed leaves disappear.",
                [q("A", f'SELECT last("cfg_peer_as") AS "Configured peer-as", last("cfg_import") AS "Import policy" FROM "NAF_CONFIG" WHERE {DEV} AND {NOW} GROUP BY "SHORTNAME", "bgp_group"')],
                [{"id": "merge", "options": {}},
                 {"id": "organize", "options": {"excludeByName": {"Time": True},
                                                "indexByName": {"SHORTNAME": 0, "bgp_group": 1, "Configured peer-as": 2, "Import policy": 3},
                                                "renameByName": {"SHORTNAME": "Device", "bgp_group": "BGP group"}}}],
                [{"matcher": {"id": "byName", "options": "Import policy"}, "properties": [{"id": "noValue", "value": "none"}]}],
                no_value="no BGP config collected"), 10, 6)
    L.add(bgp_session_state("gNMI on_change session-state per device and neighbour."), 24, 5)

    # ---------------- logging-drift ----------------
    L.row("NAF · logging-drift")
    L.add(table("Debug config present (BGP traceoptions)",
                "naf profile NAF_TRACE (get-configuration protocols bgp traceoptions, 30 s). A row means traceoptions are configured on that device. Intent: traceoptions_allowed=false unless a maintenance window is open.",
                [q("A", f'SELECT last("bgp_trace_file") AS "Trace file" FROM "NAF_TRACE" WHERE {DEV} AND {NOW} GROUP BY "SHORTNAME"')],
                [{"id": "merge", "options": {}},
                 {"id": "organize", "options": {"excludeByName": {"Time": True}, "renameByName": {"SHORTNAME": "Device"}}}],
                [color_map("Trace file", {}, other="red")], no_value="✔ no BGP traceoptions on any device"), 8, 6)
    L.add(table("Logging intent and maintenance windows",
                "naf_intent usecase=logging-drift (re-seed after every intent change). window_open=true → the orchestrator answers within-policy.",
                [q("A", 'SELECT last("traceoptions_allowed") AS "Traceoptions allowed", last("window_open") AS "Window open" FROM "naf_intent" WHERE "usecase" = \'logging-drift\' GROUP BY "device"')],
                [{"id": "merge", "options": {}},
                 {"id": "organize", "options": {"excludeByName": {"Time": True}, "renameByName": {"device": "Device"}}}],
                [color_map("Window open", {"true": "orange", "false": "green"}),
                 color_map("Traceoptions allowed", {"true": "orange", "false": "green"})]), 8, 6)
    L.add({"type": "bargauge", "title": "Log file size",
           "description": "naf profile NAF_LOGFILES (file list /var/log, 30 s): messages, bgp.log (exists once BGP traceoptions were enabled), interactive-commands. Scale 0-10 MB (archive size). Current files only (last 90 s, independent of the time range): a deleted file disappears.",
           "targets": [q("A", f'SELECT last("log_file_size") FROM "NAF_LOGFILES" WHERE {DEV} AND {NOW} GROUP BY "SHORTNAME", "log_file"',
                         "time_series", "$tag_SHORTNAME $tag_log_file")],
           "fieldConfig": {"defaults": {"unit": "bytes", "min": 0, "max": 10485760, "color": {"mode": "thresholds"},
                                        "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "orange", "value": 5242880},
                                                                                     {"color": "red", "value": 9437184}]}}, "overrides": []},
           "options": {"orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True, "valueMode": "color",
                       "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}}, 8, 6)
    L.add(timeseries("Log growth (bytes/s)",
                     "Rate of NAF_LOGFILES.log_file_size. bgp.log growing = traceoptions active; messages jumps with 'any any'. Baseline messages growth comes from openJTS's own NETCONF polling (interactive-commands any). Negative rates (rotation) are hidden.",
                     [q("A", f'SELECT mean("log_file_size_rate") FROM "NAF_LOGFILES" WHERE {DEV} AND $timeFilter AND "log_file_size_rate" >= 0 '
                             'GROUP BY time($__interval), "SHORTNAME", "log_file" fill(none)', "time_series", "$tag_SHORTNAME $tag_log_file")],
                     unit="Bps"), 16, 7)
    L.add(table("Latest commit",
                "openJTS naf profile COMMIT (get-commit-information, newest entry, 30 s). Every change of commit time raises config-change:<device>.",
                [q("A", f'SELECT last("commit_epoch") * 1000 AS "Last commit", last("commit_epoch") * 1000 AS "Age", last("commit_user") AS "User", last("commit_client") AS "Client" FROM "COMMIT" WHERE {DEV} AND $timeFilter GROUP BY "SHORTNAME"')],
                [{"id": "merge", "options": {}},
                 {"id": "organize", "options": {"excludeByName": {"Time": True}, "renameByName": {"SHORTNAME": "Device"}}}],
                [{"matcher": {"id": "byName", "options": "Last commit"}, "properties": [{"id": "unit", "value": "dateTimeAsIso"}]},
                 {"matcher": {"id": "byName", "options": "Age"}, "properties": [{"id": "unit", "value": "dateTimeFromNow"}]}]), 8, 7)

    # ---------------- prefix-limit ----------------
    L.row("NAF · prefix-limit")
    rx = timeseries("RX prefixes vs. MAX RX prefixes",
                    "RX prefixes: openJTS gNMI safi_received (accepted after import policy, IPV4_UNICAST), one line per session. "
                    "MAX RX prefixes (dashed red): the maximum RX prefixes in intent (naf_intent, hidden dashboard variable $max_rx_prefixes). "
                    "Threshold lines: 40 = WARNING (80 % of MAX RX prefixes), 50 = CRITICAL (MAX RX prefixes). "
                    "Above 50 the router tears the session down (accepted-prefix-limit), so the line drops to 0 while the session flaps: see Session resets sent. Kapacitor task prefix_limit.",
                    [q("A", f'SELECT last("safi_received") FROM "NETWORK_INSTANCE_BGP" WHERE {DEV} AND "neighbor_afi_safi_name" = \'IPV4_UNICAST\' AND $timeFilter '
                            'GROUP BY time($__interval), "SHORTNAME", "neighbor_address" fill(previous)', "time_series", "$tag_SHORTNAME ← $tag_neighbor_address"),
                     q("B", 'SELECT last("safi_received") * 0 + $max_rx_prefixes AS "MAX RX prefixes" FROM "NETWORK_INSTANCE_BGP" '
                            'WHERE "neighbor_afi_safi_name" = \'IPV4_UNICAST\' AND $timeFilter GROUP BY time($__interval) fill(previous)',
                       "time_series", "MAX RX prefixes")],
                    thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 40}, {"color": "red", "value": 50}], decimals=0)
    rx["fieldConfig"]["defaults"]["custom"]["axisLabel"] = "RX prefixes"
    rx["fieldConfig"]["overrides"] = [{"matcher": {"id": "byName", "options": "MAX RX prefixes"}, "properties": [
        {"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}},
        {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}},
        {"id": "custom.fillOpacity", "value": 0}]}]
    L.add(rx, 12, 8)
    L.add(timeseries("Pre-policy vs accepted (clamp effect)",
                     "safi_received_pre_policy (what the neighbour sends) vs safi_received (what passes the import policy). After the IMPORT-CLAMP the lines split: ~205 sent, 3 accepted.",
                     [q("A", f'SELECT last("safi_received_pre_policy") FROM "NETWORK_INSTANCE_BGP" WHERE {DEV} AND "neighbor_afi_safi_name" = \'IPV4_UNICAST\' AND $timeFilter '
                             'GROUP BY time($__interval), "SHORTNAME", "neighbor_address" fill(previous)', "time_series", "$tag_SHORTNAME ← $tag_neighbor_address pre-policy"),
                      q("B", f'SELECT last("safi_received") FROM "NETWORK_INSTANCE_BGP" WHERE {DEV} AND "neighbor_afi_safi_name" = \'IPV4_UNICAST\' AND $timeFilter '
                             'GROUP BY time($__interval), "SHORTNAME", "neighbor_address" fill(previous)', "time_series", "$tag_SHORTNAME ← $tag_neighbor_address accepted")],
                     decimals=0), 12, 8)
    L.add(table("Import clamp (compensating action)",
                "NAF_CONFIG.cfg_import (last 90 s). IMPORT-CLAMP-<group> (orange) = the orchestrator's compensating action is in place; it stays until a human removes it. "
                "INPUT-POLICY-ONLY-<peer> (green) = the intended import policy of intent v2 (scripts/update_intent.py).",
                [q("A", f'SELECT last("cfg_peer_as") AS "peer-as", last("cfg_import") AS "Import policy" FROM "NAF_CONFIG" WHERE {DEV} AND {NOW} GROUP BY "SHORTNAME", "bgp_group"')],
                [{"id": "merge", "options": {}},
                 {"id": "organize", "options": {"excludeByName": {"Time": True, "peer-as": True},
                                                "indexByName": {"SHORTNAME": 0, "bgp_group": 1, "Import policy": 2},
                                                "renameByName": {"SHORTNAME": "Device", "bgp_group": "BGP group"}}}],
                [{"matcher": {"id": "byName", "options": "Import policy"}, "properties": [
                    {"id": "noValue", "value": "none"},
                    {"id": "thresholds", "value": {"mode": "absolute", "steps": [{"color": "transparent", "value": None}]}},
                    {"id": "mappings", "value": [{"type": "regex", "options": {"pattern": "^IMPORT-CLAMP.*", "result": {"color": "orange", "index": 0}}},
                                                 {"type": "regex", "options": {"pattern": "^INPUT-POLICY.*", "result": {"color": "green", "index": 1}}}]},
                    {"id": "custom.cellOptions", "value": {"type": "color-background"}}]}]), 12, 6)
    L.add(table("Prefix intent",
                "naf_intent usecase=prefix-limit (minimum and maximum RX prefixes per device and neighbour) and the intended import policy "
                "(usecase=bgp-peer-drift; 'none' in intent v1, INPUT-POLICY-ONLY-<peer> after scripts/update_intent.py).",
                [q("A", 'SELECT last("rx_min") AS "Minimum RX prefixes", last("rx_max") AS "Maximum RX prefixes" FROM "naf_intent" WHERE "usecase" = \'prefix-limit\' GROUP BY "device", "neighbor_address"'),
                 q("B", 'SELECT last("import_policy") AS "Intended import policy" FROM "naf_intent" WHERE "usecase" = \'bgp-peer-drift\' GROUP BY "device", "neighbor_address"')],
                [{"id": "organize", "options": {"excludeByName": {"Time": True}}}, {"id": "merge", "options": {}},
                 {"id": "organize", "options": {"indexByName": {"device": 0, "neighbor_address": 1, "Minimum RX prefixes": 2,
                                                                "Maximum RX prefixes": 3, "Intended import policy": 4},
                                                "renameByName": {"device": "Device", "neighbor_address": "Neighbor"}}}]), 12, 6)
    resets = timeseries("Session resets sent (prefix-limit teardown)",
                        "BGP NOTIFICATIONs this router sent per minute (session_notif_sent = neighbor/state/messages/sent/NOTIFICATION). "
                        "Only the router that tears the session down sends them: pe1 when its accepted-prefix-limit is exceeded. "
                        "Kapacitor prefix_limit raises CRITICAL at 2 or more in 2 min, if the session also reached Established 2 or more times (session_flaps).",
                        [q("A", f'SELECT non_negative_difference(last("session_notif_sent")) FROM "NETWORK_INSTANCE_BGP" WHERE {DEV} AND $timeFilter '
                                'GROUP BY time(1m), "SHORTNAME", "neighbor_address" fill(none)', "time_series", "$tag_SHORTNAME ← $tag_neighbor_address")],
                        thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 2}], decimals=0)
    resets["fieldConfig"]["defaults"]["custom"].update(drawStyle="bars", fillOpacity=60, spanNulls=False)
    L.add(resets, 12, 7)
    L.add(bgp_session_state("gNMI on_change session-state per device and neighbour (same panel as in NAF · bgp-peer-drift). "
                            "While pe1's accepted-prefix-limit tears the session down, both ends flicker between ESTABLISHED (green) "
                            "and the other states (red); with the clamp or the fix it stays green."), 12, 7)

    # ---------------- security-policy-drift ----------------
    L.row("NAF · security-policy-drift (deferred)")
    L.add(table("csrx rule order: actual vs intent",
                "Actual = naf_secpolicy (collector/netconf_poller.py --only secpolicy); intent = naf_intent usecase=security-policy-drift. Empty until the csrx has zones/policies (OPEN-QUESTIONS Q1).",
                [q("A", 'SELECT last("rule_order") AS "Actual order", last("rule_count") AS "Actual rules" FROM "naf_secpolicy" WHERE $timeFilter GROUP BY "device", "from_zone", "to_zone"'),
                 q("B", 'SELECT last("rule_order") AS "Intended order", last("rule_count") AS "Intended rules" FROM "naf_intent" WHERE "usecase" = \'security-policy-drift\' GROUP BY "device", "from_zone", "to_zone"')],
                [{"id": "organize", "options": {"excludeByName": {"Time": True}}}, {"id": "merge", "options": {}},
                 {"id": "organize", "options": {"indexByName": {"device": 0, "from_zone": 1, "to_zone": 2, "Actual order": 3, "Intended order": 4,
                                                                "Actual rules": 5, "Intended rules": 6},
                                                "renameByName": {"device": "Device", "from_zone": "From zone", "to_zone": "To zone"}}}],
                no_value="deferred: csrx has no zones/policies yet"), 24, 5)

    # ---------------- swap-a-block ----------------
    L.row("NAF · swap-a-block")
    L.add(table("Collector A: openJTS profile (COMMIT poll)",
                "naf profile COMMIT, the default Collector for logging-drift.",
                [q("A", f'SELECT last("commit_epoch") * 1000 AS "Newest commit", last("commit_user") AS "User" FROM "COMMIT" WHERE {DEV} AND $timeFilter GROUP BY "SHORTNAME"')],
                [{"id": "merge", "options": {}}, {"id": "organize", "options": {"excludeByName": {"Time": True}, "renameByName": {"SHORTNAME": "Device"}}}],
                [{"matcher": {"id": "byName", "options": "Newest commit"}, "properties": [{"id": "unit", "value": "dateTimeAsIso"}]}]), 8, 6)
    L.add(table("Collector B: Python poller (naf_commit)",
                "collector/netconf_poller.py, the alternative Collector component. Same values as A when it runs.",
                [q("A", 'SELECT last("commit_epoch") * 1000 AS "Newest commit", last("user") AS "User" FROM "naf_commit" WHERE $timeFilter GROUP BY "device"')],
                [{"id": "merge", "options": {}}, {"id": "organize", "options": {"excludeByName": {"Time": True}, "renameByName": {"device": "Device"}}}],
                [{"matcher": {"id": "byName", "options": "Newest commit"}, "properties": [{"id": "unit", "value": "dateTimeAsIso"}]}],
                no_value="poller not running"), 8, 6)
    L.add(table("Collector C: syslog push (naf_syslog)",
                "telegraf_naf_syslog (compose profile naf-syslog): UI_COMMIT_COMPLETED pushed by the devices. Arrives ~5 s after a commit instead of ~30 s.",
                [q("A", 'SELECT count("message") AS "Commit messages", last("message") AS "Last message" FROM "naf_syslog" WHERE $timeFilter GROUP BY "hostname"')],
                [{"id": "merge", "options": {}}, {"id": "organize", "options": {"excludeByName": {"Time": True}, "renameByName": {"hostname": "Device"}}}],
                no_value="syslog Collector not active"), 8, 6)
    L.add(table("Which Intent backend and Executor acted",
                "naf_event decisions that reached the Executor (pending / remediated / no-drift), grouped by the Intent backend and Executor in use. Same id, plan and diff whatever the blocks.",
                [q("A", 'SELECT count("count") AS "Decisions" FROM "naf_event" WHERE $timeFilter AND "kind" =~ /^(pending|remediated|no-drift)$/ '
                        'GROUP BY "intent_backend", "executor", "kind"')],
                [{"id": "merge", "options": {}},
                 {"id": "organize", "options": {"excludeByName": {"Time": True}, "renameByName": {"intent_backend": "Intent", "executor": "Executor", "kind": "Decision"}}}],
                no_value="no executor decisions in range"), 24, 6)
    L.newline()
    return L.panels


# Rows open when the dashboard loads; every other row starts collapsed (attendees expand the row of
# the use case they work on). Expanding in the browser is not saved, so the profile file stays the
# single source of truth.
OPEN_ROWS = ("Device health", "Traffic counters", "NAF loop · Orchestrator decisions")


def _is_open(title: str) -> bool:
    return any(title.startswith(t) for t in OPEN_ROWS)


def flatten(panels: list[dict]) -> list[dict]:
    """Un-nest collapsed rows, so the dashboard is a flat list in display order (idempotent input)."""
    out: list[dict] = []
    for p in panels:
        children = (p.get("panels") or []) if p.get("type") == "row" else []
        if p.get("type") == "row":
            p = {**p, "collapsed": False, "panels": []}
        out.append(p)
        out.extend(children)
    return out


def sections(panels: list[dict]) -> list[tuple[dict | None, list[dict]]]:
    """[(row or None, panels under it)] from a flat list."""
    result: list[tuple[dict | None, list[dict]]] = [(None, [])]
    for p in panels:
        if p.get("type") == "row":
            result.append((p, []))
        else:
            result[-1][1].append(p)
    return [s for s in result if s[0] is not None or s[1]]


def collapse_and_layout(panels: list[dict]) -> list[dict]:
    """Re-stack sections top to bottom; nest the children of rows that are not in OPEN_ROWS."""
    out: list[dict] = []
    y = 0
    for row, children in sections(panels):
        top = min((c["gridPos"]["y"] for c in children), default=0)
        height = max((c["gridPos"]["y"] + c["gridPos"]["h"] - top for c in children), default=0)
        if row is not None:
            row = {**row, "gridPos": {**row["gridPos"], "x": 0, "y": y, "w": 24, "h": 1}}
            y += 1
        placed = [{**c, "gridPos": {**c["gridPos"], "y": y + c["gridPos"]["y"] - top}} for c in children]
        if row is not None and not _is_open(row.get("title", "")):
            out.append({**row, "collapsed": True, "panels": placed})
        else:
            if row is not None:
                out.append({**row, "collapsed": False, "panels": []})
            out.extend(placed)
            y += height
    return out


def main() -> None:
    d = json.loads(DASH.read_text())
    panels = flatten(d["panels"])
    start = next((i for i, p in enumerate(panels) if p.get("type") == "row" and
                  (p.get("title", "").startswith("Drift (NAF loop)") or p.get("title", "").startswith("NAF "))), len(panels))
    kept = panels[:start]
    y = max((p["gridPos"]["y"] + p["gridPos"]["h"] for p in kept), default=0)
    first_id = max((p["id"] for p in kept), default=0) + 1
    d["panels"] = collapse_and_layout(kept + build(y, first_id))
    anns = d.setdefault("annotations", {}).setdefault("list", [])
    if not any(a.get("name") == "NAF remediations" for a in anns):
        anns.append({"datasource": {"type": "datasource", "uid": "grafana"}, "enable": True, "hide": False, "iconColor": "purple",
                     "name": "NAF remediations", "target": {"limit": 100, "matchAny": False, "tags": ["naf"], "type": "tags"}})
    tvars = d.setdefault("templating", {}).setdefault("list", [])
    tvars[:] = [v for v in tvars if v.get("name") != "max_rx_prefixes"] + [{
        # hidden: maximum RX prefixes from intent, drawn as the "MAX RX prefixes" line (refreshed with the time range)
        "name": "max_rx_prefixes", "label": "MAX RX prefixes", "type": "query", "hide": 2, "refresh": 2,
        "datasource": DS, "includeAll": False, "multi": False, "options": [], "current": {}, "regex": "", "sort": 0,
        "query": 'SELECT max("rx_max") FROM (SELECT last("rx_max") AS "rx_max" FROM "naf_intent" '
                 'WHERE "usecase" = \'prefix-limit\' GROUP BY "device", "neighbor_address")',
        "definition": ""}]
    # Open on every device: a value saved from the Grafana UI (e.g. "csrx") would hide pe1/pe2 panels
    for v in tvars:
        if v.get("name") in ("device", "interface"):
            v["current"] = {"selected": True, "text": ["All"], "value": ["$__all"]}
    d["version"] = d.get("version", 0) + 1
    d["id"] = None
    DASH.write_text(json.dumps(d, indent=2) + "\n")
    rows = [p for p in d["panels"] if p.get("type") == "row"]
    print(f"kept {len(kept)} panels, NAF rows rebuilt, version {d['version']}; "
          f"open: {[r['title'] for r in rows if not r['collapsed']]}; collapsed: {sum(r['collapsed'] for r in rows)}")


if __name__ == "__main__":
    main()
