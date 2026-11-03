# Aroma-Link Local

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Release](https://img.shields.io/github/v/release/jrx-code/hassio-integration-aroma-link)](https://github.com/jrx-code/hassio-integration-aroma-link/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Home Assistant 2024.1+](https://img.shields.io/badge/Home%20Assistant-2024.1%2B-blue)

![Aroma-Link Local](https://raw.githubusercontent.com/jrx-code/hassio-integration-aroma-link/main/docs/images/hero.png)

Local, cloud-free Home Assistant integration for **Aroma-Link / DeWoo / Crearoma** WiFi scent
diffusers. Verified on the **Pro300** (firmware `V1.0.20250416`, hardware `A6/Pro300_2_SEG`).

The diffuser speaks plain MQTT to the vendor cloud. Point its broker hostname at a broker you own
and this integration takes over the conversation: it decodes the device frames, exposes power and
fan as switches and sends exactly the commands the cloud would send. The vendor app and cloud are
no longer involved.

Local control was verified end to end: a fan command from Home Assistant is acknowledged by the
device within a second, the device reports the new fan state on the bus, and the power draw at the
wall changes accordingly.

## Contents

1. [Features](#features)
2. [How it works](#how-it-works)
3. [Requirements](#requirements)
4. [Installation](#installation)
5. [Configuration](#configuration)
6. [Redirecting the diffuser to your broker](#redirecting-the-diffuser-to-your-broker)
7. [Going back to the vendor cloud](#going-back-to-the-vendor-cloud)
8. [Protocol in short](#protocol-in-short)
9. [Limitations and roadmap](#limitations-and-roadmap)
10. [Troubleshooting](#troubleshooting)
11. [Development](#development)
12. [Credits and license](#credits-and-license)

## Features

![Entities and services](https://raw.githubusercontent.com/jrx-code/hassio-integration-aroma-link/main/docs/images/entities.png)

![Device page of a Pro300 (Polish UI, MAC address masked)](https://raw.githubusercontent.com/jrx-code/hassio-integration-aroma-link/main/docs/images/device-page.png)

Everything the cloud integration offered, now local (one design difference: the diffuser keeps a
single set of 5 schedule slots, so Home Assistant owns the weekly schedule and writes the current
day's slots at midnight).

| | |
|---|---|
| **Auto-discovery** | One integration entry. Every diffuser that publishes on the broker becomes a device. |
| **Control** | `Power` and `Fan` switches, state read back from the device. Changes made on the device show up within the poll interval. |
| **Status** | `Cycle state` (off / diffusing / paused), `Spray time left`, `Pause time left`, `Device time`, `Clock offset`, `Last reading`, `Software version` (with model and module telemetry). |
| **Weekly schedule** | Up to 4 windows per weekday, level 1-3, work/pause seconds; written to the device and verified, drift repaired. Switch `Automatic power`, services `set_schedule`, `import_schedule`, `sync_schedules`. |
| **Night Owl** | Low-intensity run outside the windows (slot 5), per-day permission, fixed or outside-windows mode, motion-gated. Switch `Night spraying after motion`, service `set_night_owl`. |
| **Timed run** | `start_timed_run` / `cancel_timed_run`, survives restarts, overlay in slot 5, hands power back at the end. |
| **Gating** | Optional HVAC, occupancy and motion gates with delays, per device, in the integration options. Two modes: schedule flip (default, no power beeps) and power switching. |
| **Usage and oil** | `Total pump time`, `Power-on count`, `Pump cycle count` (estimated from power and work phase), `Oil level`, `Oil remaining`, `Refilled to full` button, services `oil_refill`, `oil_calibrate`. |
| **Diagnostics** | `Spraying allowed`, service `send_frame`, `aroma_link_local_frame` events. |
| **Schedule card** | The cjam28 schedule card ships with the integration and loads by itself: `type: custom:aroma-link-schedule-card`. |
| **Persistence** | Devices, schedule, flags, timed run, counters and oil state survive restarts. |
| **Footprint** | Python standard library only. Depends on the core `mqtt` integration. |

The schedule card on a live Pro300 (MAC address masked):

![Schedule card](https://raw.githubusercontent.com/jrx-code/hassio-integration-aroma-link/main/docs/images/schedule-card.png)

### Notes on the data

- Counters and oil are **estimates**. The device reports no counters; they are derived from the power flag and work phase in the status report, split by the active slot's work/pause durations. Calibrate the oil with `oil_calibrate` (start, run, end, set `measured_remaining`, finalize) or give a rate. Until then the level is estimated at a nominal 1.5 ml per hour of pump work (attribute `rate_source: default`), so a refill shows 100% at once.
- The schedule and gating are inert until a schedule was saved from Home Assistant (`set_schedule`, the card, or `import_schedule` to adopt what the device holds). A fresh install never overwrites the device or fights manual use.
- Signal strength (RSSI) is not available locally; it was not found in the telemetry. See `docs/protocol.md`.
- The device clock is local time and has no known way to be set. It can be minutes off (one unit measured +12 min, constant for hours), and windows run on that clock. `Clock offset` shows the difference and the integration compensates: windows are written shifted by the measured whole minutes, split across the device's midnight, and imported slots are shifted back. Your windows always mean real time.

## How it works

![Architecture: diffuser, broker, Home Assistant](https://raw.githubusercontent.com/jrx-code/hassio-integration-aroma-link/main/docs/images/architecture.png)

| Topic | Direction | Content |
|---|---|---|
| `q/1/d/<id>/bus` | HA to device | commands: power, fan, schedule write, state query |
| `q/2/d/<id>/bus` | device to HA | command acknowledgements, fan state, schedule dumps, boot announce |
| `q/2/d/<id>/sys` | device to HA | plain-text telemetry (firmware and module versions), not parsed yet |

`<id>` is `qdpu118T` followed by the device MAC in lowercase without colons. It is also the MQTT
client id the device connects with.

Every payload is a small binary envelope carrying an ASCII-hex frame; both layers have their own
checksum. The integration subscribes to the uplink wildcard through Home Assistant's own MQTT
connection, validates and decodes the frames, and publishes commands on the downlink topic. The
full wire format is in [`docs/protocol.md`](docs/protocol.md) and summarised
[below](#protocol-in-short).

## Requirements

- Home Assistant **2024.1** or newer with the **MQTT integration** configured against a broker you control.
- A broker that can accept the diffuser's connection: a listener **without authentication** for this
  client (its password is a rotating signed token, see below) plus topic ACLs. Verified with EMQX;
  Mosquitto notes included.
- A router or resolver where you can **override one DNS name** for the diffuser's network, and,
  depending on your topology, a NAT rule and a firewall allow rule.
- A way to **power-cycle** the diffuser. A smart plug makes this painless; the device otherwise
  keeps its cloud socket open indefinitely.

## Installation

### HACS (recommended)

1. HACS, Integrations, three-dot menu, **Custom repositories**. Add
   `https://github.com/jrx-code/hassio-integration-aroma-link`, category *Integration*.
2. Download **Aroma-Link Local** and restart Home Assistant.
3. Settings, Devices & services, **Add integration**, search *Aroma-Link Local*, confirm. There are
   no fields: devices appear as soon as they publish on the broker.

### Manual

Copy `custom_components/aroma_link_local/` into `<config>/custom_components/` and restart. Then add
the integration as above.

## Configuration

The integration has no broker settings of its own: it rides on Home Assistant's MQTT integration.
Options (Settings, the integration, **Configure**):

| Option | Default | Meaning |
|---|---|---|
| State poll interval | `60` | Seconds between status and schedule queries per device, 0 to 3600. `0` disables the timer (status then updates only on commands and at start). |
| Uplink topic | `q/2/d/+/+` | MQTT subscription for device messages. Covers both `/bus` and `/sys`. |
| Downlink pattern | `q/1/d/{id}/bus` | Command topic. `{id}` is replaced with the device topic id. |

Gating (HVAC, occupancy, motion) is configured per device under *Configure, Gating*.

Naming: the device is created as `Aroma-Link <MAC>` with entities `Power` and `Fan`. Rename the
device in Home Assistant to fit your own scheme; entity ids follow Home Assistant's normal rules.

Nothing goes into `configuration.yaml`. Discovered devices are kept in
`.storage/aroma_link_local.devices`.

## Redirecting the diffuser to your broker

This is the only non-trivial step. It happens entirely on your network: the device firmware is
untouched and the change is fully reversible.

### 1. Know what the device does

Observed on the Pro300 (packet capture of a cold start):

| Item | Value |
|---|---|
| Broker | resolved by DNS: `iot-south.landecia.com` (an AWS address that changes), TCP **1883**, **no TLS** |
| Protocol | MQTT 3.1.1, keepalive 120 s, clean session, QoS 1 |
| Client id | `qdpu118T<mac>` (same as the topic id) |
| Username | none |
| Password | a signed token `1;<counter>;<salt>;<signature>` that changes between connections |
| Last Will | none |
| Subscription | `q/1/d/<id>/+` |

Two consequences: the redirect has to happen at the **DNS** level (the IP is not hard-coded), and
the broker cannot authenticate the device with a static credential.

To confirm this on your own network, capture on the device's VLAN while power-cycling it:

```bash
tcpdump -ni <iface> -s0 "host <device-ip> and (udp port 53 or tcp port 1883)"
```

### 2. Prepare the broker

Give the device a listener that does not require authentication, keep authorisation (ACL) strict,
and let Home Assistant's MQTT account use the two topic trees.

**EMQX** (verified). A second TCP listener on another port with authentication disabled:

```hocon
listeners.tcp.aroma {
  bind = "0.0.0.0:1893"
  enable_authn = false
}
```

File-based authorisation, placed before the final deny. The regular expression matches every
Aroma-Link client id, so any number of diffusers is covered by two lines:

```erlang
{allow, {clientid, {re, "^qdpu118T[0-9a-f]{12}$"}}, publish,   ["q/2/d/${clientid}/+"]}.
{allow, {clientid, {re, "^qdpu118T[0-9a-f]{12}$"}}, subscribe, ["q/1/d/${clientid}/+"]}.
{allow, {username, "<home-assistant-mqtt-user>"}, subscribe, ["q/2/d/+/+"]}.
{allow, {username, "<home-assistant-mqtt-user>"}, publish,   ["q/1/d/+/bus"]}.
```

The same can be done in the EMQX dashboard or through its REST API (`POST /api/v5/listeners` with
`"name": "tcp:aroma"`).

**Mosquitto** (not verified here, taken from the Mosquitto documentation). With
`per_listener_settings true`, add a listener with `allow_anonymous true` and an ACL file that uses
`pattern` rules so the client id is substituted:

```
per_listener_settings true

listener 1893
allow_anonymous true
acl_file /etc/mosquitto/aroma.acl
```

```
# aroma.acl
pattern write q/2/d/%c/+
pattern read  q/1/d/%c/+
```

**Port and topology.** The device always connects to **1883**, and your authenticated listener
usually owns that port already. Two ways to meet the device:

- **A. The router rewrites the port** (what the author runs). The DNS name points at a broker
  address in another subnet; the router allows the flow, rewrites 1883 to the listener port and
  masquerades the return path. Everything lives on the router, see step 3.
- **B. A dedicated broker address inside the device VLAN, no NAT.** Give the broker host a second
  IP in the diffuser's VLAN (extra interface or address alias), bind the no-auth listener to
  `<that IP>:1883` and point the DNS name at it. Device and listener share one L2 segment, so the
  router needs no firewall or NAT rule at all, only the DNS record. The catch: Linux refuses to
  bind `<ip>:1883` while another socket listens on `0.0.0.0:1883` (verified: `EADDRINUSE`; it only
  works when both sockets set `SO_REUSEPORT`, which neither EMQX 5.8 nor Mosquitto expose). So the
  authenticated listener has to be bound to explicit addresses as well, one listener per address it
  serves (LAN IP, IoT IP, loopback). Rebinding disconnects its plaintext clients once; they
  reconnect on their own. If your broker runs in Docker **bridge** mode, publishing
  `<that IP>:1883` to the container's listener port (for example `"192.0.2.29:1883:1893"`) achieves
  the same without touching listener binds (not tested by the author, whose EMQX uses host
  networking).

Pick A when you would rather not touch a working broker, B when you want the router out of the
picture. Step 3 describes A in full; for B only its DNS item applies.

![Topology A and B](https://raw.githubusercontent.com/jrx-code/hassio-integration-aroma-link/main/docs/images/topology.png)

### 3. DNS override and routing

1. **DNS.** On the resolver the diffuser uses (usually the router), add a static record
   `iot-south.landecia.com -> <broker IP>`. On a dnsmasq-based router this is one line:
   `host-record=iot-south.landecia.com,<broker IP>`. UniFi: Settings, Routing, DNS, static entry.
   Pi-hole or AdGuard Home: a local DNS record. Flush the resolver cache afterwards (dnsmasq re-reads
   on `HUP`).
2. **Firewall.** If the IoT VLAN is isolated from the broker's VLAN, allow TCP from the diffuser (or
   the IoT network) to `<broker IP>:<listener port>` and nothing more.
3. **Port rewrite** (only if the listener is not on 1883). Destination NAT on the router for the
   diffuser's traffic: `tcp <device IP> -> <broker IP>:1883` becomes `<broker IP>:<listener port>`.
4. **Return path.** A rewritten flow must come back through the router, so add a source NAT
   (masquerade) for that same flow. Without it the broker answers the device directly from a port it
   never talked to and the connection dies with resets.
5. **Same-subnet trap.** If the broker also has an address in the diffuser's own subnet, do **not**
   point the DNS name at that address when you rely on the router rewriting the port: traffic
   between two hosts in one subnet never reaches the router. Point the name at a broker address in
   another subnet instead (then items 2 to 4 apply).
6. **Persistence.** Make sure the rules survive a router reboot. On UniFi gateways hand-made
   `iptables` rules are lost on reboot; use the controller's NAT and firewall rules, or a boot
   script. If the rules vanish, the device falls back to the cloud after its next reconnect, which is
   a safe failure but a silent one.

Example with documentation addresses (diffuser `198.51.100.20`, broker `192.0.2.10`, no-auth
listener on 1893), raw `iptables` form for reference:

```bash
iptables -I FORWARD -s 198.51.100.20 -d 192.0.2.10 -p tcp --dport 1893 -j ACCEPT
iptables -t nat -A PREROUTING  -s 198.51.100.20 -d 192.0.2.10 -p tcp --dport 1883 -j DNAT --to-destination 192.0.2.10:1893
iptables -t nat -A POSTROUTING -s 198.51.100.20 -d 192.0.2.10 -p tcp --dport 1893 -j MASQUERADE
```

### 4. Power-cycle and verify

The device holds its cloud socket stubbornly: dropping the connection-tracking entry or blocking
the cloud for a minute did not make it reconnect in testing. Cut the power for about ten seconds.

Then check, in this order:

1. **Broker:** a client `qdpu118T<mac>` is connected on the new listener. With an account allowed
   to read the tree, `mosquitto_sub -v -t 'q/#'` shows frames on `q/2/d/<id>/bus` and `/sys` right
   after boot (`53/F1` identity, `53/06` status).
2. **Home Assistant log:** `discovered Aroma-Link device qdpu118T... (mac ...)`.
3. **Entities:** a device `Aroma-Link <MAC>` with `Power` and `Fan`.
4. **Control:** toggle Fan. On the bus you should see, within a second: a downlink `57/11` frame,
   an uplink acknowledgement `57/11` with value `00`, and an uplink state `53/11` with value `01`
   (on) or `00` (off). The Fan switch follows that state frame.

## Going back to the vendor cloud

You will want this when **pairing a new diffuser in the vendor app** (pairing goes through the
cloud), for a vendor firmware update, or to rule out your own infrastructure while troubleshooting.

1. Remove the DNS override (and the NAT rules if you added any). Flush the resolver cache.
2. Power-cycle the diffuser(s). They resolve the real cloud address again and the app works.
3. Do what you need in the app.
4. Put the override and rules back, power-cycle again. New diffusers show up in Home Assistant on
   their own; nothing to configure.

While the redirect is active the vendor app shows the device offline. This integration does not
talk to the cloud at all, so there is no "both at once" mode.

## Protocol in short

![Frame format](https://raw.githubusercontent.com/jrx-code/hassio-integration-aroma-link/main/docs/images/frame-format.png)

MQTT payload, a binary envelope around an ASCII-hex frame:

```
AA AA | len16 | cksum8 | msgid16 | tag16 | ASCII-hex inner frame
```

- `len16`: number of bytes after the length field. `tag16`: `0023` downlink, `0024` uplink.
- `cksum8` = `(sum of msgid bytes + sum of tag bytes + sum of the ASCII bytes) & 0xFF`. A frame with
  a wrong value is acknowledged by the device's MQTT stack (PUBACK) and then silently dropped, which
  makes it confusing to debug.
- `msgid16` must be fresh: a value the device has already seen is ignored as a replay. The
  integration starts at `0x1001` after each start and counts up.

Inner frame:

```
AA | dir | flag | MAC(6) | 00 | cmd | sub | payload | XOR | EE
```

`dir` is `AC` downlink or `CA` uplink, `flag` is `00` or `01`, `XOR` covers every byte from `AA` up
to the byte before it.

| Operation | Dir | cmd/sub | Payload |
|---|---|---|---|
| Power on / off | down | `57/10` | `01 00 01` / `01 00 00` |
| Fan on / off | down | `57/11` | `01 00 01` / `01 00 00` |
| Schedule write (5 slots) | down | `57/15` | `2E 00 01` + 5 x 9 bytes: `startH startM endH endM flags work16 pause16` |
| State / schedule query | down | `52/15` | `02 00 01 1F` |
| Command acknowledged | up | `57/10`, `57/11` | `01 00 00` (always `00`: an ACK, not the state) |
| Power state | up | `53/10` | `01 00 <01 or 00>` |
| Fan state | up | `53/11` | `01 00 <01 or 00>` |
| Schedule / status dump | up | `52/15`, `53/15` | `2E 00 01` + 5 slots |
| Boot announce | up | `53/F1`, `53/06` | identity string (IMEI, firmware, model), short status |

Slot `flags`: high nibble = intensity level 1 to 3, low nibble = enabled. Times are hex hour and
minute (`17 3B` = 23:59); work and pause are big-endian seconds.

## Limitations and roadmap

- **No signal strength** and no firmware update path; both would need more protocol work.
- **Counters and oil are estimates** (see above); the error is bounded by the poll interval (default 60 s).
- **State after changes on the device** shows with the next status report (poll interval); the fan state is pushed immediately.
- **`/sys` telemetry** is shown as attributes of the firmware sensor, not as separate sensors.
- The card was ported from the cloud integration; layouts are unchanged.
- Tested with one Pro300. Other Aroma-Link models use the same app and most likely the same protocol; a report with a capture is welcome.

## Troubleshooting

| Symptom | Check |
|---|---|
| No device appears | Is the device on your broker? Look at the broker's client list. Is Home Assistant's MQTT account allowed to subscribe `q/2/d/+/+`? Power-cycle the device: it announces itself only after a connect. |
| Entities `unavailable` after a restart | Fixed in 0.3.4 (devices are remembered and queried at start). Update. |
| Device connects, commands are PUBACKed, nothing happens | Envelope checksum or replayed msgid. Make sure you run 0.3.3 or newer. |
| Device keeps reconnecting to the cloud | Resolver cache (flush it), a second resolver the device may use, or an override applied to one interface only. Confirm with a capture during power-up. |
| Connection refused or resets | VLAN isolation (allow the flow), missing masquerade for a DNATed flow, or the DNS name points at a broker address in the device's own subnet (see the same-subnet trap). |
| A switch flips back | The device's real state frame (`53/10`, `53/11`) wins over the optimistic update: the command was dropped. See the checksum row. |
| After a router reboot the device is back in the cloud | The NAT or firewall rules were not persistent. |

## Development

```bash
python3 custom_components/aroma_link_local/aroma_codec.py   # codec self-test
```

The codec (`aroma_codec.py`) has no Home Assistant imports, so it can be reused in a bridge or a
test harness. Hassfest and HACS validation run on every push (`.github/workflows/validate.yml`).

## Credits and license

- [cjam28/homeassistant_aroma-link](https://github.com/cjam28/homeassistant_aroma-link): the
  cloud-based integration this project replaces for local control; its entity model was a useful
  reference.
- The protocol was reverse-engineered from packet captures of the device talking to the vendor
  cloud.

MIT License. Not affiliated with Aroma-Link, DeWoo or Crearoma. Use at your own risk: redirecting
the device takes it off the vendor cloud until you undo the DNS change.

