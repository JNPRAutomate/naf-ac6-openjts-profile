# How to create a new profile in OpenJTS (example: `naf`)

This guide explains how to build a custom OpenJTS profile, using the **NAF workshop** profile as an example. It collects:

- **BGP and interface counters** from **cRPD** routers via **gNMI**
- **Interface MTU and admin/oper status** from **cSRX** firewalls via **NETCONF**

All the data goes to InfluxDB and appears in one Grafana dashboard.

> A profile builder UI (widgets to create profiles without editing JSON) is planned in OpenJTS. Until it ships, you build profiles by hand as described below.

---

## 1. What a profile is

A profile is a **`.tgz` archive** that contains **one folder with the same name as the archive** (`naf.tgz` → `naf/`). The folder holds:

| File | Purpose |
|------|---------|
| `definition.json` | Profile entry point: description, and which Telegraf config to use per device family/version, which Grafana dashboard(s) to install |
| `crpd_naf.json` | Telegraf template for the **crpd** family (gNMI paths) |
| `csrx_naf.json` | Telegraf template for the **csrx** family (NETCONF RPC + XPaths) |
| `dashboard-naf.json` | Grafana dashboard installed when the profile is used |
| `commit_change.tick` | Kapacitor TICKscript that alerts when a device's latest commit timestamp changes |

```bash
ls -lart naf/
-rw-rw-r-- 1 hpen hpen 10935 crpd_naf.json
-rw-rw-r-- 1 hpen hpen   372 definition.json
-rw-rw-r-- 1 hpen hpen  1369 csrx_naf.json
-rw-rw-r-- 1 hpen hpen 11198 dashboard-naf.json
```

> Package only the files the profile needs. Keep backups (for example `crpd_naf.original.json`), notes and **secrets such as `grafana.token.txt`** out of the archive.

### How JTSO processes a profile

```text
naf.tgz ──► compose/jtso/profiles/        (scanned by JTSO every ~1 minute, change detected by MD5)
         └► extracted to compose/jtso/active_profiles/naf/
               ├─ definition.json   → picks crpd_naf.json / csrx_naf.json per router family+version
               ├─ *_naf.json        → rendered into Telegraf TOML (compose/telegraf/<family>/telegraf.d/)
               └─ dashboard-naf.json→ copied into Grafana
```

Data flow inside each Telegraf container:

```text
input (gNMI / NETCONF) → rename → filtering → enrichment → regex → rate → monitoring → InfluxDB output
                         (processors run in ascending "order")
```

---

## 2. `definition.json`

```json
{
  "version": 1,
  "min-interval": 30,
  "description": "This profile provides template for collecting NAF workshop related data",
  "telegraf": {
    "crpd": [{ "version": "all", "conf": "crpd_naf.json" }],
    "csrx": [{ "version": "all", "conf": "csrx_naf.json" }]
  },
  "kapacitor": ["commit_change.tick"],
  "grafana": ["dashboard-naf.json"]
}
```

- **`telegraf`**: one key per device family (`mx`, `ptx`, `acx`, `ex`, `qfx`, `srx`, `crpd`, `cptx`, `csrx`, `vmx`, `vsrx`, `vjunos`, `vevo`). Each family takes a list of `{version, conf}` entries:
  - `"all"` is the fallback for any Junos version.
  - You can also target versions, for example `">=24.2"` or `"==23"`. A version-specific entry wins over `"all"`.
- **`kapacitor`**: list of TICKscript files in the profile folder (or `null`/`[]` for none). JTSO installs each one as a Kapacitor **stream** task named `jts_tick_<n>` on `jtsdb.autogen`, and removes it when no router uses the profile anymore. The NAF profile ships `commit_change.tick`. See [Kapacitor alerting](#kapacitor-alerting-commit-change).

### Kapacitor alerting (commit change)

Both `crpd_naf.json` and `csrx_naf.json` poll `<get-commit-information>` over NETCONF every 30 s into the `COMMIT` measurement:

- `commit-history[sequence-number]` becomes the tag `commit_seq`. Only `commit_seq = 0` (the latest commit) is kept, so a device writes one point per poll instead of up to 50.
- `date-time` uses the `epoch` field type. The plugin's `time_layout` (`2006-01-02 15:04:05 MST`) parses Junos `2026-10-22 14:19:11 UTC` into Unix seconds, stored as `commit_epoch`. `user` and `client` are stored as `commit_user` and `commit_client`.

`commit_change.tick` groups the points by `device`, runs `difference('commit_epoch')` and raises an `INFO` alert when the difference is not 0. Because `difference()` only outputs the computed field, the script joins the result back with the original point so it can include the user and client in the alert. Each new commit produces one alert. That alert is:

- appended to `/var/lib/kapacitor/alerts.log` inside the container (`~/JTS/compose/kapacitor/data/alerts.log` on the host);
- written back to InfluxDB as measurement `COMMIT_ALERT`, which you can use in Grafana panels or annotations.

The dashboard's **Latest commit** table (below *Device inventory*) shows one row per device with the commit time (`commit_epoch * 1000`, unit `dateTimeAsIso`), its age (`dateTimeFromNow`), the user and the client. Rows are sorted with the most recent commit first.

To get Slack, email, webhook or similar notifications, add a handler to the `|alert()` node, for example `.post('http://...')` or `.slack()` once Slack is configured in `kapacitor.conf`. Because polling runs every 30 s, there is up to 30 s of delay, and several commits made within one poll interval produce a single alert. The first point after the task starts is used as the baseline and never alerts.

> **JTSO v1.2.3 bug: duplicate tasks.** JTSO queues the TICKscript once **per router** that uses the profile, instead of once per script. With pe1, pe2 and the cSRX, you get 3 identical tasks (`jts_tick_0..2`), which means 3 alerts per commit. JTSO only keeps track of the last task, so the others are never removed. After each deployment, and after each JTSO restart (JTSO wipes and recreates all tasks at startup), remove the extra tasks and keep the highest-numbered one:
>
> ```bash
> docker exec kapacitor kapacitor -url http://localhost:9292 list tasks
> docker exec kapacitor kapacitor -url http://localhost:9292 delete tasks jts_tick_0 jts_tick_1
> ```
>
> The upstream fix is a one-line change in `association/stackconfig.go`: only append to `kapaStart` when `!found && to_add`.
- **`grafana`**: dashboard file(s) to install.

---

## 3. `csrx_naf.json`: collecting via NETCONF

```json
{
  "netconf_inputs": [{
    "subscriptions": [{
      "name": "INTERFACES",
      "rpc": "<get-interface-information></get-interface-information>",
      "fields": [
        { "field_path": "/interface-information/physical-interface[name]/mtu",          "field_type": "int" },
        { "field_path": "/interface-information/physical-interface[name]/admin-status", "field_type": "string" },
        { "field_path": "/interface-information/physical-interface[name]/oper-status",  "field_type": "string" }
      ],
      "interval": 30
    }]
  }],
  "rename_list": [{
    "order": 100,
    "namepass": ["INTERFACES"],
    "entries": [
      { "type": 0, "from": "name",         "to": "if_name" },
      { "type": 1, "from": "mtu",          "to": "if_mtu" },
      { "type": 1, "from": "admin-status", "to": "if_admin_status" },
      { "type": 1, "from": "oper-status",  "to": "if_oper_status" }
    ]
  }],
  "enrichment_list": [{
    "order": 200, "namepass": ["INTERFACES"],
    "family": "csrx", "two_levels": false, "level1_tag": "device"
  }],
  "influx_outputs": [{
    "retention": "autogen",
    "fieldpass": ["if_mtu", "if_admin_status", "if_oper_status"]
  }]
}
```

### 3.1 `netconf_inputs`: what to collect

- **`name`**: the **measurement** (InfluxDB table) where the data goes, here `INTERFACES`. You can define several subscriptions. Usually you create **one subscription per show command/RPC**.
- **`rpc`**: the Junos RPC behind the show command. To find it, run the command on the device with `| display xml rpc`:

  ```text
  > show interfaces | display xml rpc
  <rpc><get-interface-information></get-interface-information></rpc>
  ```

- **`fields`**: the values to extract, written as an *XPath-lite* that follows the RPC reply. Run `show interfaces | display xml` to see the reply structure.
  - **Square brackets mark the key of a list.** `show interfaces` returns a list of `physical-interface` elements, and each one has a `<name>`. `physical-interface[name]` tells the plugin: "this is a list, a new element starts at each `name`, and `name` is the key". The key can be any child element that identifies an entry, such as `name` or an IP address. You find it in the XML output. It is usually obvious.
  - The key becomes a **tag** (indexed, used for filtering/grouping). The other leaves become **fields** (values).
  - **Nested lists** use two keys. For example, to collect logical interface attributes:
    `/interface-information/physical-interface[name]/logical-interface[name]/...`
- **`field_type`**: the conversion to apply. Supported types: `int`, `float`, `string`, `epoch`, `epoch_ms`, `epoch_us`, `epoch_ns`. Use the `epoch*` types to convert date strings to timestamps. Most often you convert a string to `int` or `float`.
- **`interval`**: polling period in seconds.

### 3.2 `rename_list`: normalizing names

Use **`type: 0`** to rename a **tag** (the key, here `name` → `if_name`) and **`type: 1`** to rename a **field**.

- With **NETCONF**, the plugin names tags and fields after their **leaf element** (`name`, `mtu`, `admin-status`). Use those leaf names in `from`.
- In the target name (`to`), use short `snake_case` names **without `-`**.
- Renaming is optional, but it makes the data easier to read. It also lets you **use the same names for cRPD (gNMI) and cSRX (NETCONF)**. Then the database holds one set of metrics no matter which protocol collected them, and **a single dashboard** can show both families. In the NAF profile, both families store the interface name as `if_name`.

### 3.3 `enrichment_list`: adding metadata

The enrichment processor adds JTSO inventory tags (for example `SHORTNAME`, `FAMILY`, `MODEL`, `VERSION`) to each point. It looks them up by the `device` tag in `/var/metadata/metadata_<family>.json`. **`family` must match the family of the file** (`csrx` here). Almost every profile needs this step, because the dashboards filter on these tags.

### 3.4 `influx_outputs`: what goes to the database

Only the fields in **`fieldpass`** are written to InfluxDB. If you add a new field and forget to list it here, it is silently dropped.

---

## 4. `crpd_naf.json`: collecting via gNMI

This file follows the same structure, with a gNMI input and more processors.

### 4.1 `gnmi_inputs`

```json
{ "name": "NETWORK_INSTANCE_BGP",
  "path": "/network-instances/network-instance/protocols/protocol/bgp/neighbors/neighbor/state/session-state",
  "mode": "on_change", "interval": 0 },
{ "name": "INTERFACES",
  "path": "/interfaces/interface/state/counters",
  "mode": "sample", "interval": 30 }
```

- **`name`**: the destination measurement. The NAF profile writes to two of them: `NETWORK_INSTANCE_BGP` (BGP global, peer-group, neighbor and AFI/SAFI state) and `INTERFACES` (interface counters). `INTERFACES` is **the same measurement as the cSRX NETCONF data**.
- **`path`**: an OpenConfig/Junos gNMI path.
- **`mode`**: `sample` (periodic, every `interval` seconds) or `on_change` (sent only when the value changes, use `interval: 0`). The BGP `session-state` uses `on_change`, so state transitions are captured right away.

### 4.2 Processors used in the cRPD file

| Section | `order` | What it does in NAF |
|---------|--------:|---------------------|
| `rename_list` | 100 | gNMI keeps **full paths** as names, so `from` is the full path. Keys (`type: 0`) become `instance_name`, `neighbor_address`, `peer_group_name`, `if_name`, and so on. Values (`type: 1`) become `safi_received`, `session_state`, `in_octets`, and so on. Both `/interfaces/interface/name` and `/interfaces/interface/@name` map to `if_name` to cover both key encodings. |
| `filtering_list` | 150 | Keeps only interfaces whose `if_name` tag (`type: 0` = tag, `1` = field) matches `^(eth\|lo).*`. |
| `enrichment_list` | 200/210 | Adds the cRPD metadata (`family: "crpd"`). For `INTERFACES` it uses `two_levels: true` with `level2_tags: ["if_name"]`, so per-interface metadata (such as `LINKNAME`) can be added too. |
| `regex_list` | 300 | Renames the long prefix-limit field to `max-prefixes` (`type: 1` = rename a field by regex). |
| `rate_list` | 500 | Turns the counters (`in_octets`, `in_pkts`, …) into rates by adding new fields with a `_rate` suffix (`in_octets_rate`, …). |
| `monitoring_list` | 600 | Creates real-time alarms in the `ALARMING` measurement: `ETH_DROP_IN` / `ETH_DROP_OUT` fire when `in_errors_rate` / `out_errors_rate` is `gt` 1. |
| `influx_outputs` | – | Lists all the fields to store, **including the `_rate` fields** created by the rate processor. |

> `order` only sets the relative order of the processors. JTSO can renumber them when it merges several profiles for the same device, but the relative order stays the same.

Other processors are available if you need to change data further: `clone_list`, `pivot_list`, `xreducer_list`, `converter_list`, `enum_list`, `strings_list`. Look at the native profiles in `JTS/design_profile/` for examples.

> **Watch the number of InfluxDB series.** Every distinct tag value creates a new series. Never keep a tag that changes over time, such as a process PID: the cRPD keeps spawning short-lived processes (`mgd` for every NETCONF session, `ifinfo`, `ps`, …), and each one would add new series. The NAF profile overwrites `process_pid` with a constant through `regex_list` (`"type": 2`), which keeps one series per process name. To check, run `SHOW SERIES EXACT CARDINALITY FROM "<measurement>"`. The count should stay flat over time.

---

## 5. The Grafana dashboard

Build the dashboard in Grafana, then export it as JSON to `dashboard-naf.json`. Make sure that:

- Panels use the OpenJTS InfluxDB datasource (`"uid": "jtsuid"`).
- Queries use the **measurement names** (`INTERFACES`, `NETWORK_INSTANCE_BGP`) and the **renamed** tags/fields (`if_name`, `if_mtu`, `device`, `SHORTNAME`, …). Influx names are **case-sensitive**.
- The dashboard has its own `uid` and `title` (`naf-profile` / `NAF PROFILE`).
- The top-level `"id"` is **`null`**. A dashboard exported from Grafana carries its internal database ID, for example `"id": 97`. If that dashboard is later deleted, provisioning fails silently, and Grafana shows "Dashboard not found / invalid UID".
- The dashboard **filename is unique** to the profile and is never reused for a different dashboard. Grafana links each file path to one dashboard. If you reuse a name, for example by shipping a copied BGP dashboard as `dashboard-naf.json`, the new dashboard is never loaded. To recover, rename the file and update the `grafana` list in `definition.json`.
- Panels that show raw numbers (MTU, ifIndex, …) use unit **`none`**. With `bytes`, Grafana displays 9500 as `9 KiB`.

> **Don't save changes to a profile dashboard from the Grafana UI.** Saving from the UI, or importing a dashboard with the same `uid`, creates a separate database copy. Grafana keeps showing that copy and ignores the profile file, so your later profile updates never appear. Edit the JSON in the profile instead (or export from Grafana, then set `"id": null`) and redeploy the `.tgz`. If a UI copy already exists, delete it (`curl -X DELETE http://<host>:<grafana-port>/api/dashboards/uid/<uid>`). Then redeploy, because Grafana only re-reads a file when its timestamp changes.

---

### NAF workshop rows (closed-loop KPIs)

The bottom of `dashboard-naf.json` belongs to the NAF workshop closed loop (`~/naf-ac6-workshop`):
one row for the orchestrator and one row per use case. **Do not edit those panels by hand.**
Regenerate them with `python3 tools/naf_drift_rows.py`. The script is idempotent and never changes
the panels above the first NAF row. Then rebuild and deploy the `.tgz`.

The script also sets which rows start **collapsed**. Only the rows listed in `OPEN_ROWS` (*Device
health*, *Traffic counters*, *NAF loop · Orchestrator decisions*) open by default; the others
(*Interfaces*, *Drift: admin up / oper down*, every *NAF · <use case>* row) are collapsed, and their
panels are nested inside the row object, as Grafana stores collapsed rows. Edit those rows' panels
from the script or in the JSON, never by saving from the UI.

| Row | KPIs | Data |
|---|---|---|
| NAF loop · Orchestrator decisions | autonomy level, decisions by kind, alert level per use case, decision log, remediation annotations | `naf_event` (orchestrator, `ORCH_INFLUX_EVENTS=true`), annotations tag `naf` |
| NAF · bgp-peer-drift | peer-as actual vs intent (+ DRIFT flag), configured peer-as / import per group, session state | `NETWORK_INSTANCE_BGP`, `naf_intent`, **`NAF_CONFIG`** |
| NAF · logging-drift | BGP traceoptions present, logging intent + maintenance windows, log file size, log growth (B/s), latest commit | **`NAF_TRACE`**, **`NAF_LOGFILES`**, `naf_intent`, `COMMIT` |
| NAF · prefix-limit | RX prefixes vs. MAX RX prefixes (40/50 thresholds, dashed MAX RX prefixes line from intent via the hidden variable `max_rx_prefixes`), pre-policy vs accepted, import clamp present, prefix intent (minimum / maximum RX prefixes), session resets sent (NOTIFICATIONs per minute) next to the BGP session state | `NETWORK_INSTANCE_BGP`, **`NAF_CONFIG`**, `naf_intent` |
| NAF · security-policy-drift (deferred) | rule order actual vs intent | `naf_secpolicy`, `naf_intent` |
| NAF · swap-a-block | newest commit per Collector (profile / Python poller / syslog push), which Intent backend + Executor acted | `COMMIT`, `naf_commit`, `naf_syslog`, `naf_event` |

Collection items added to the profile for these rows (NETCONF, every 30 s):

| Measurement | Families | RPC | Tags → fields |
|---|---|---|---|
| `NAF_CONFIG` | crpd | `<get-configuration>` filtered to `protocols bgp` | `bgp_group` → `cfg_peer_as` (int), `cfg_import` (string, absent when no import policy) |
| `NAF_TRACE` | crpd | `<get-configuration>` filtered to `protocols bgp traceoptions` | → `bgp_trace_file` (string). **No point at all** when traceoptions are absent |
| `NAF_LOGFILES` | crpd, csrx | `<file-list><detail/><path>/var/log/</path></file-list>` | `log_file` (filtered to `messages`, `bgp.log`, `interactive-commands`) → `log_file_size`, `log_file_size_rate` (rate processor) |

gNMI items added to `crpd_naf.json` for these rows (measurement `NETWORK_INSTANCE_BGP`, `sample`, every 60 s):

| Path (under `.../bgp/neighbors/neighbor/state/`) | Field | Used by |
|---|---|---|
| `messages/sent/NOTIFICATION` | `session_notif_sent` (counter) | `prefix_limit.tick` CRITICAL when the router keeps tearing the session down (with `session_flaps`); *Session resets sent* panel |
| `established-transitions` | `session_flaps` (counter) | flap count, grows on both ends of the session; `prefix_limit.tick` requires it so that a session that never comes up does not count |

Config leaves that disappear (an import policy removed, traceoptions deleted) produce no new value.
Their panels therefore query only the last 90 s (`time > now() - 90s`) instead of the dashboard range.
The syslog level (`messages any any`) cannot be collected this way: Junos encodes it as an empty
element (`<any/>`), which the XPath-lite cannot read. The `messages` growth rate is the KPI instead.

> **Pitfall: no backslashes in filter or regex patterns.** JTSO copies `pattern` into a TOML
> double-quoted string, so `bgp\.log` becomes the invalid TOML escape `\.`. **Every** Telegraf of
> that family then refuses to start (`invalid TOML syntax`), and all collection stops until the
> profile is fixed. Use a character class instead: `bgp[.]log`. Check after each deploy:
> `docker logs --since 2m telegraf_crpd 2>&1 | grep 'E!'`.

## 6. Package and deploy

```bash
cd ~/naf-ac6-openjts-profile
# archive name = folder name; list only the profile files (no backups, notes or tokens)
tar czvf naf.tgz naf/definition.json naf/crpd_naf.json naf/csrx_naf.json naf/dashboard-naf.json naf/commit_change.tick
cp naf.tgz ~/JTS/compose/jtso/profiles/
```

> Use `tar czvf` (the `z` matters). An archive made with `tar cf` is not gzipped, and JTSO rejects it with `Unable to extract new profile naf: gzip: invalid header`.

Then follow the JTSO log on the host (see [Read the JTSO log](#read-the-jtso-log) for the messages to expect):

```bash
tail -f /var/log/jtso.log
```

Within about a minute you should see that the new profile was detected, or that the profile was updated if you replaced an existing archive. To update a profile later, rebuild the archive and copy it again. JTSO notices the content change and reconfigures the stack.

### Assign the profile

In the JTSO portal go to **Profiles → Associations** and add `naf` to the cRPD and cSRX routers. Saving triggers a stack reconfiguration. Click the green **file** icon to view the generated Telegraf TOML. You can also find it on disk under `JTS/compose/telegraf/<family>/telegraf.d/`.

A router's profile list can't be edited in place. If the router already has profiles (for example `bgp`), adding `naf` fails with `Router <name> is already assigned to a profile`. **Delete** the router's association, then **add** it again with every profile selected (for example `bgp` + `naf`). The router collects nothing between the two steps, so do them back to back.

The profile's dashboard then shows up in Grafana (OpenJTS folder).

---

## 7. Verify and troubleshoot

### Check the data in InfluxDB

```bash
cd ~/JTS/compose
sudo docker compose exec influxdb bash
influx
```

```sql
USE jtsdb
SHOW MEASUREMENTS
SELECT * FROM INTERFACES ORDER BY time DESC LIMIT 10
SHOW TAG KEYS FROM INTERFACES
SHOW FIELD KEYS FROM INTERFACES
```

Allow a sampling interval or two (about 30–60 s) after the association before data appears.

### Read the JTSO log

JTSO writes its log on the **host** at `/var/log/jtso.log`. The JTSO container mounts the host's `/var/log`, so you don't need `docker exec`. JTSO scans `compose/jtso/profiles/` about once a minute.

```bash
tail -f /var/log/jtso.log                         # follow live
grep -E "naf|ERROR" /var/log/jtso.log | tail -30  # recent profile events and errors
```

**Normal sequence after copying a new or updated `.tgz`:**

```text
[INFO] New profile naf detected and added to active profiles    ← first time
[INFO] Profile naf has been updated                             ← later versions (archive content changed)
[INFO] Time to reconfigure JTS components for family crpd
[INFO]  Family: crpd
[INFO]      Profiles [files]:
[INFO]        -naf [crpd_naf.json]                               ← definition.json picked this file for the family
[INFO] Active dashboard dashboard-naf.json for profile naf      ← dashboard copied to Grafana
[INFO] telegraf_crpd container has been restarted
[INFO] All JTS components reconfigured for family crpd
```

After you change an association, you should also see `Profile(s) of router <name> has been successfully updated` or `Profile of router <name> has been successfully deleted`.

**Messages that point to a problem:**

| Message | Meaning / fix |
|---------|---------------|
| `Unable to extract new profile <name>: gzip: invalid header` | The archive isn't gzipped. Rebuild it with `tar czvf`. |
| `Unable to open defintion.json for profile <name>` | The archive doesn't contain `<name>/definition.json`. Check that the folder name matches the archive name. |
| `Unable to parse defintion.json for profile <name>` | `definition.json` is invalid JSON. Check it with `python3 -m json.tool definition.json`. |
| `Router <name> is already assigned to a profile` | Delete the router's association and add it again with all profiles (see [Assign the profile](#assign-the-profile)). |
| `Unable to open the source dashboard <file>` | The `grafana` list in `definition.json` names a file that isn't in the archive. |
| `telegraf_<family> container has been stopped - no router to collect` | Normal for families with no assigned router. If it's *your* family, no router of that family is assigned yet. |
| No `-<profile> [<file>.json]` line under your `Family:` | `definition.json` has no entry for that family. Telegraf then collects nothing from the profile for those routers. |

> The log shows only **JTSO** events: archive extraction, config generation, container restarts. Errors from the actual collection (gNMI/NETCONF connections, wrong paths or RPCs) appear in the Telegraf debug log `/var/log/debugtelegraf_<family>.log`, described below.
>
> On cRPD/cSRX you may also see JTSO's own inventory collection log `[WARNING] ... Unable to parse ISIS Overview` or `No LACP Interface information`. These come from metadata RPCs that container platforms don't support, and they don't affect the profile.

### Where to look when nothing shows up

| Check | How |
|-------|-----|
| Profile detected/extracted? | `/var/log/jtso.log`. See [Read the JTSO log](#read-the-jtso-log) for the expected messages and common errors. |
| Telegraf config generated for the family? | `ls ~/JTS/compose/telegraf/csrx/telegraf.d/` and `cat` the `csrx_collection_*.conf` file |
| Telegraf container running? | `sudo docker ps \| grep telegraf_csrx`. A Telegraf container starts only if at least one router is assigned to that family. |
| Collection errors (auth, RPC, paths)? | Enable **Debug** (the "D" button on the Telegraf container in the JTSO main page), then `tail -f /var/log/debugtelegraf_<family>.log` (for example `debugtelegraf_csrx.log`). Disable it afterwards, because debug logs grow fast. |
| gNMI `connection refused` / `no route to host` in the debug log? | The device isn't serving gNMI on the port JTSO uses. JTSO reads the port from `JTS/compose/jtso/config.yml` (`gnmi.port`, 9339 by default), and the same port applies to all devices. Make the device listen on it (`set system services extension-service request-response grpc clear-text port 9339`), then test with `gnmic -a <ip>:9339 --insecure -u <user> -p <pwd> capabilities`. |
| `W! [agent] ["outputs.influxdb"] did not complete within its flush interval` in the debug log? | Harmless, and no data is lost. InfluxDB waits up to 10s before fsyncing writes (`wal-fsync-delay = "10s"` in `compose/influxdb/influxdb.conf`), which is longer than Telegraf's default `flush_interval` of 5s. To silence it, raise `flush_interval` above 10s (for example 15s) in JTSO **Settings → Telegraf tuning**. Only worry if you also see `buffer overflow` / dropped metrics. |
| Field missing in the DB? | It's probably not listed in `influx_outputs.fieldpass`, or the `rename_list` `from` doesn't match the raw name. Check the raw names in the debug log. |
| Router in the wrong family? | Check the router's family in JTSO. It must match a family key in `definition.json` (`csrx`, not `crpd`). |

> **Known issue seen during the NAF work:** in an earlier JTSO build, a family whose profile used **only NETCONF** (no gNMI input), like cSRX here, did not get its Telegraf container started. If `telegraf_csrx` doesn't come up while the config exists, upgrade JTSO to a release that includes the fix.

---

## 8. Checklist

- [ ] Folder name = profile name = archive name (`naf/` → `naf.tgz`)
- [ ] `definition.json` lists every family you target, and each `conf` file exists
- [ ] One subscription per RPC (NETCONF) or path (gNMI), with a meaningful measurement `name`
- [ ] List keys marked with `[key]` in the NETCONF `field_path`
- [ ] Tags renamed with `type: 0`, fields with `type: 1`; same target names across families
- [ ] `enrichment_list` uses the right `family`
- [ ] Every field to store is listed in `influx_outputs.fieldpass` (including `_rate` fields)
- [ ] Dashboard uses datasource `jtsuid` and the renamed measurement/tag/field names
- [ ] No backups or secrets in the archive
