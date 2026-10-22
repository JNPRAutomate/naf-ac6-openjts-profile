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
  "kapacitor": null,
  "grafana": ["dashboard-naf.json"]
}
```

- **`telegraf`**: one key per device family (`mx`, `ptx`, `acx`, `ex`, `qfx`, `srx`, `crpd`, `cptx`, `csrx`, `vmx`, `vsrx`, `vjunos`, `vevo`). Each family takes a list of `{version, conf}` entries:
  - `"all"` is the fallback for any Junos version.
  - You can also target versions, for example `">=24.2"` or `"==23"`. A version-specific entry wins over `"all"`.
- **`kapacitor`**: Kapacitor alerting scripts. The NAF profile has none, so it is `null`.
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

## 6. Package and deploy

```bash
cd ~/jts_custom_profiles
tar czvf naf.tgz naf/                       # archive name = folder name
cp naf.tgz ~/JTS/compose/jtso/profiles/
```

Then follow the JTSO log on the host:

```bash
tail -f /var/log/jtso.log
```

Within about a minute you should see that the new profile was detected, or that the profile was updated if you replaced an existing archive. To update a profile later, rebuild the archive and copy it again. JTSO notices the content change and reconfigures the stack.

### Assign the profile

In the JTSO portal go to **Profiles → Associations** and add `naf` to the cRPD and cSRX routers. Saving triggers a stack reconfiguration. Click the green **file** icon to view the generated Telegraf TOML. You can also find it on disk under `JTS/compose/telegraf/<family>/telegraf.d/`.

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

### Where to look when nothing shows up

| Check | How |
|-------|-----|
| Profile detected/extracted? | `tail -f /var/log/jtso.log` (errors like `Unable to parse defintion.json` point to invalid JSON) |
| Telegraf config generated for the family? | `ls ~/JTS/compose/telegraf/csrx/telegraf.d/` and `cat` the `csrx_collection_*.conf` file |
| Telegraf container running? | `sudo docker ps \| grep telegraf_csrx`. A Telegraf container starts only if at least one router is assigned to that family. |
| Collection errors (auth, RPC, paths)? | Enable **Debug** (the "D" button on the Telegraf container in the JTSO main page), then `tail -f /var/log/debugtelegraf_<family>.log` (for example `debugtelegraf_csrx.log`). Disable it afterwards, because debug logs grow fast. |
| gNMI `connection refused` / `no route to host` in the debug log? | The device isn't serving gNMI on the port JTSO uses. JTSO reads the port from `JTS/compose/jtso/config.yml` (`gnmi.port`, 9339 by default), and the same port applies to all devices. Make the device listen on it (`set system services extension-service request-response grpc clear-text port 9339`), then test with `gnmic -a <ip>:9339 --insecure -u <user> -p <pwd> capabilities`. |
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
