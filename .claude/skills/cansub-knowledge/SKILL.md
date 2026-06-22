---
name: cansub-knowledge
description: >
  Authoritative reference for the CSS Electronics CANsub product family (CANsub.2
  and CANsub.4 CAN / CAN FD streaming interfaces). Use whenever a task needs
  accurate CANsub facts: hardware specs, the REST / WebSocket API, bit-timing,
  hardware filters, transmit sequences, connectors / pin-outs, firmware,
  certificate install; higher-layer protocols (OBD2, UDS, J1939, NMEA 2000,
  CANopen, CCP/XCP); the software tools (webCAN, SavvyCAN, PlotJuggler); or the
  python-can-cansub Python integration. Also consult it when another skill (e.g.
  cansub-reverse-engineering) needs CANsub context. Bundled doc snapshots live in
  assets/.
allowed-tools:
  - Read
  - Grep
  - Glob
---

# CANsub knowledge

Bundled, authoritative documentation for the CSS Electronics CANsub family
(CANsub.2 / CANsub.4 CAN / CAN FD streaming interfaces). Prefer these snapshots
over web search or training-data recall.

## How to use this skill

The reference material is in `assets/` (relative to this skill directory). These
files are large, so **don't read them whole** — `Grep` for the relevant term
first (e.g. an endpoint name, `bit-timing`, `mDNS`, a protocol), then `Read` only
the matching section.

| File | Scope | Use it for |
|---|---|---|
| `assets/cansub_intro_llm.txt` | Getting started / higher layer | webCAN configuration, protocol-specific guidance (OBD2, UDS, J1939, NMEA 2000, CANopen, CCP/XCP), streaming troubleshooting, and the software/API tool ecosystem (webCAN, python-can-cansub, SavvyCAN, PlotJuggler, custom REST/WebSocket apps) |
| `assets/cansub4_llm.txt` | CANsub.4 technical manual | The authoritative device / API reference: hardware specs, REST API, WebSocket API, firmware, bit-timing, connectors, display, device label |
| `assets/cansub_openapi.json` | REST API (OpenAPI 3.0 spec) | Exact endpoints, request/response schemas, and parameters for device config + monitoring — use when building or debugging a REST client |
| `assets/python-can-cansub-README.md` | python-can-cansub package | The python-can integration: mDNS auto-discovery, hardware filters, broadcast manager, webCAN-compatible CSV logging, and standard python-can tools |

## CANsub.2 vs CANsub.4

Only the CANsub.4 manual is bundled. The CANsub.2 is identical except:

- Front connector: 2x DB9 instead of 1x DB25.
- CAN channels: 2x CAN instead of 4x CAN (so the channel index range is 0-1, not
  0-3); adjust any channel-count-dependent API field accordingly.

For a CANsub.2 question, read `cansub4_llm.txt` and apply those two deltas. Don't
invent other differences.

## Related skills in this repo

- **`cansub-reverse-engineering`** — uses the CANsub (via `python-can-cansub`) to
  capture and reverse engineer CAN signals. It refers here for device / API /
  protocol details.
