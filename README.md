# Aroma-Link Local

Local, cloud-free Home Assistant control of **Aroma-Link / DeWoo / Crearoma** WiFi
scent diffusers (verified on the **Pro300**). The device speaks native MQTT to the
vendor cloud; this integration takes that over on your own broker, so the device
never needs the cloud again.

It is **auto-discovering**: add the integration once and every diffuser that
publishes on your broker appears automatically (no MAC to type in). Power and fan
are exposed as switches.

## How it works

The diffuser keeps one plaintext MQTT (TCP/1883) connection to a broker whose
hostname it resolves by DNS. It subscribes to its downlink topic and publishes
state on its uplink topic:

```
q/1/d/<device-id>/bus    commands  (HA -> device)
q/2/d/<device-id>/bus    state     (device -> HA)
q/2/d/<device-id>/sys    telemetry (device -> HA)
```

`<device-id>` is `qdpu118T` + the lowercase MAC. Messages are a small ASCII-hex
frame (`AA | dir | MAC | cmd | sub | payload | XOR | EE`). This integration
subscribes the uplink wildcard through Home Assistant's own MQTT integration,
decodes the frames, and publishes commands on the downlink topic.

## Requirements

- Home Assistant **2024.1+** with the **MQTT integration** configured and pointed
  at your own MQTT broker (e.g. Mosquitto or EMQX).
- The diffuser **redirected to that broker** (see *Redirecting the device* below).

## Install (HACS)

1. HACS → Integrations → ⋮ → **Custom repositories** → add this repository, type
   *Integration*.
2. Install **Aroma-Link Local**, restart Home Assistant.
3. Settings → Devices & Services → **Add Integration** → *Aroma-Link Local*
   (single click; devices appear on their own once they publish).

## Configuration

The integration itself needs no broker settings — it uses Home Assistant's MQTT
integration. Options (Settings → the integration → **Configure**):

| Option | Default | Meaning |
|---|---|---|
| State poll interval | `0` (off) | Seconds between periodic state queries per device (`0` disables). |
| Uplink topic | `q/2/d/+/+` | Subscription for device messages (covers `/bus` and `/sys`). |
| Downlink pattern | `q/1/d/{id}/bus` | Command topic; `{id}` is the device topic-id. |

Entity and area names are set natively in Home Assistant (rename the device).

## Redirecting the device to your broker (go local)

The diffuser resolves its broker hostname by DNS, so the clean method is a **local
DNS override**. Find the hostname and broker with a packet capture on the device's
VLAN while it (re)connects, e.g.:

```bash
tcpdump -ni <iface> -s0 "host <device-ip> and (tcp port 1883 or udp port 53)"
# power-cycle the diffuser; note the DNS name it looks up (e.g. iot-south.landecia.com)
```

Then:

1. **DNS override** on your router/resolver: point that hostname at your broker's IP.
   (If the device uses a hard-coded IP instead, use a destination-NAT rule; when the
   broker and device share an L2 subnet, add SNAT/masquerade so return packets go
   back through the router.)
2. **Broker auth.** The device connects with its topic-id as client-id, **no
   username**, and a **rotating signed password token** — so a static broker
   credential will not match. Use a broker listener that does not require
   authentication for this client (keep topic ACLs), or an auth backend that
   accepts it. The device sets **no Last-Will**.
3. **Broker ACL.** Allow the device client-id to publish `q/2/d/<id>/+` and
   subscribe `q/1/d/<id>/+`; allow Home Assistant's MQTT account to subscribe
   `q/2/d/+/+` and publish `q/1/d/+/bus`.
4. Power-cycle the diffuser; it reconnects to your broker. Removing the DNS/NAT
   override returns it to the vendor cloud (kill-switch).

## Protocol

Reverse-engineered frame format, command set, and checksum are documented in
[`docs/protocol.md`](docs/protocol.md). Checksum is a byte-wise XOR of the frame
up to the checksum byte. Commands: power (`57/10`), fan (`57/11`), schedule
(`57/15`), query (`52/15`). Oil level is a software estimate from runtime, not a
device reading.

## License

MIT. Not affiliated with Aroma-Link, DeWoo, or Crearoma.
