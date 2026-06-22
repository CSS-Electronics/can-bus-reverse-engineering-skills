# python-can-cansub

A [python-can](https://python-can.readthedocs.io/) integration for the [CANsub](https://csselectronics.com/) CAN bus interface family by CSS Electronics. Source on [GitHub](https://github.com/CSS-Electronics/python-can-cansub).

This package registers the CANsub as a standard python-can interface, making it compatible with all python-can tools and workflows. It also adds a CSV logger compatible with the *webCAN* browser tool provided with the device.

> **Tip:** This README is optimized for LLMs. When using an AI coding assistant with this package, provide this file as context for accurate results.

## python-can API

### Installation

```bash
pip install python-can-cansub
```

### Import

When `python-can-cansub` is installed, the `cansub` interface is automatically registered with python-can. Import with:

```python
import can
```

### Configuration

Python-can defines a hardware *configuration* by an `interface` and a `channel` (a single interface can have multiple channels).

The CANsub `interface` is fixed `"cansub"`. The `channel` is constructed from the device hostname (unique) and channel index.

| Connection | Hostname                | python-can `channel` string       |
|------------|-------------------------|-----------------------------------|
| USB        | `[DEVICE-ID]-usb.local` | `[DEVICE-ID]-usb.local@[channel]` |
| Ethernet   | `[DEVICE-ID]-eth.local` | `[DEVICE-ID]-eth.local@[channel]` |

The device-ID is printed on the device label. Channel indexing is **1-based** - the first channel is `1`.

A configuration is passed to `can.Bus` to open a bus.

#### Fixed

Example of a fixed configuration:

```python
configs = [{"interface": "cansub", "channel": "aabbccdd-usb.local@1"},
           {"interface": "cansub", "channel": "aabbccdd-usb.local@2"}]
```

#### Auto-detect

Example of using `detect_available_configs` to automatically discover (uses mDNS) all connected CANsub devices and channels:

```python
configs = can.detect_available_configs(interfaces=["cansub"])
# e.g. [{"interface": "cansub", "channel": "aabbccdd-usb.local@1"},
#       {"interface": "cansub", "channel": "aabbccdd-usb.local@2"},
#       {"interface": "cansub", "channel": "11223344-eth.local@1"},
#       {"interface": "cansub", "channel": "11223344-eth.local@2"}]
```

In the above example two CANsub devices are detected, each with two channels. One device is connected via USB and the other via Ethernet.

### Opening a Bus

#### Single bus - hardcoded

```python
with can.Bus(interface="cansub", channel="aabbccdd-usb.local@1", bitrate=250_000, data_bitrate=1_000_000) as bus:
    pass
```

#### Single bus - from configs

```python
with can.Bus(interface=configs[0]["interface"], channel=configs[0]["channel"], bitrate=250_000, data_bitrate=1_000_000) as bus:
    pass
```

#### Multiple buses - from configs

```python
with (can.Bus(interface=configs[0]["interface"], channel=configs[0]["channel"], bitrate=250_000, data_bitrate=1_000_000) as bus1,
      can.Bus(interface=configs[1]["interface"], channel=configs[1]["channel"], bitrate=250_000, data_bitrate=1_000_000) as bus2):
    pass
```

> **Tip:** `**config` unpacks a config dict directly into `can.Bus` keyword arguments:
>
> ```python
> with can.Bus(**configs[0], bitrate=250_000, data_bitrate=1_000_000) as bus:
>     pass
> ```

### Receive and Transmit

```python
with can.Bus(**configs[0], bitrate=250_000, data_bitrate=1_000_000) as bus:
    
    # Transmit
    msg_tx = can.Message(is_extended_id=False, arbitration_id=0x123, data=[0x01, 0x02, 0x03, 0x04])
    bus.send(msg_tx)
    
    # Receive with timeout
    msg_rx = bus.recv(timeout=1.0)
    print(msg_rx)
```

### Filters

Apply hardware filters by passing `can_filters` to `can.Bus`. Each filter specifies a `can_id`, a `can_mask`, and whether to match standard (`extended=False`) or extended (`extended=True`) frames. A frame passes if `(frame_id & can_mask) == (can_id & can_mask)`.

```python
filters = [
    {"can_id": 0x123, "can_mask": 0x7FF, "extended": False},  # standard frames, exact ID match
    {"can_id": 0x000, "can_mask": 0x000, "extended": True},   # all extended frames
]

with can.Bus(**configs[0], bitrate=250_000, data_bitrate=1_000_000, can_filters=filters) as bus:
    msg = bus.recv(timeout=1.0)
    print(msg)
```

> **Tip:** Applying hardware filters reduces the network load between the CANsub and the connected client.

### Notifier and Listeners

`bus.recv()` blocks until a frame arrives. A `can.Notifier` runs a background thread that dispatches received frames to one or more *listeners*, allowing the main program to continue other work.

python-can provides built-in listeners including `can.Printer` (print to stdout) and `can.Logger` (log to file). The example below prints to stdout and logs to a CSV file while the main program continues. Custom listeners can be implemented by subclassing `can.Listener`.

```python
from time import sleep

with can.Bus(**configs[0], bitrate=250_000, data_bitrate=1_000_000) as bus:
    with can.Notifier([bus], listeners=[can.Printer(), can.Logger("log.csv")]):

        # Perform other tasks here while frames are received in the background
        sleep(10)
```

### Broadcast Manager

Periodic transmission jobs can be started with `bus.send_periodic()`.

Most periodic transmission job types can be offloaded to the CANsub hardware, providing much better transmission time accuracy (compared to a host-scheduled transmission). A host-side background task is used only as a fallback when hardware transmission is not available.

```python
from time import sleep

msgs = [
    can.Message(is_extended_id=False, arbitration_id=0x123, data=[0x01, 0x02, 0x03, 0x04]),
    can.Message(is_extended_id=False, arbitration_id=0x124, data=[0x05, 0x06, 0x07, 0x08]),
    can.Message(is_extended_id=False, arbitration_id=0x125, data=[0x09, 0x0A, 0x0B, 0x0C]),
]

with can.Bus(**configs[0], bitrate=250_000, data_bitrate=1_000_000) as bus:
    # period: time between individual frames (sequence repeats every len(msgs) * period)
    # duration: total transmission time in seconds (None = transmit indefinitely)
    task = bus.send_periodic(msgs, period=0.1, duration=5.0)

    # Perform other tasks here while frames are transmitted in the background
    sleep(6)
```

### Replaying files

`can.MessageSync` can be used to replay messages from a log file.

```python
with can.Bus(**configs[0], bitrate=250_000, data_bitrate=1_000_000) as bus:
    with can.LogReader("log.csv") as reader:
        for msg in can.MessageSync(messages=reader):
            bus.send(msg)
```

## python-can tools

python-can includes several command line tools. All tools accept `--interface` and `--channel` to select the bus, following the same configuration as the API.

The common argument pattern for the CANsub:

```
--interface cansub --channel aabbccdd-usb.local@1 --bitrate 250000 --data-bitrate 1000000
```

Note that the *filter* argument supported by some command-line tools is limited to standard (11-bit) CAN IDs. Filtering on extended (29-bit) IDs requires the python-can API.

### can_logger

Log received frames to a file (format inferred from file extension):

```bash
can_logger --interface cansub --channel aabbccdd-usb.local@1 --bitrate 250000 --data-bitrate 1000000 --file_name log.csv
```

### can_player

Play back a previously recorded log file:

```bash
can_player --interface cansub --channel aabbccdd-usb.local@1 --bitrate 250000 --data-bitrate 1000000 log.csv
```

### can_viewer

Live terminal viewer showing received frames, updated counts, timestamps, and byte-level changes:

```bash
can_viewer --interface cansub --channel aabbccdd-usb.local@1 --bitrate 250000 --data-bitrate 1000000
```

On Windows, the can_viewer requires `windows-curses` (`pip install windows-curses`).

### can_bridge

Forward all frames received on one bus to another (e.g. bridge two CANsub channels):

```bash
can_bridge --bus1-interface cansub --bus1-channel aabbccdd-usb.local@1 --bus1-bitrate 250000 --bus1-data-bitrate 1000000 \
           --bus2-interface cansub --bus2-channel aabbccdd-usb.local@2 --bus2-bitrate 250000 --bus2-data-bitrate 1000000
```

### can_logconvert

Convert a log file between formats; the format is inferred from the file extension:

```bash
can_logconvert log.csv log.asc
```

## Related Packages

The following packages complement `python-can-cansub` and are included here as inspiration for working with CAN data in Python.

### cantools

[cantools](https://github.com/cantools/cantools) is a Python package for encoding and decoding CAN messages. Encoding/decoding rules can be created or loaded from DBC (and other) database files. It works directly with `can.Message` objects from python-can.

#### Installation

```bash
pip install cantools
```

#### Create database in code

A database can be constructed directly in Python without a database file:

```python
import cantools

db = cantools.database.Database()

msg_def = cantools.database.can.Message(
    frame_id=0x123,
    name="Message1",
    length=8,
    signals=[
        cantools.database.can.Signal(name="Signal1", start=0,  length=16, scale=0.1, offset=0.0, minimum=0.0, maximum=100.0),
        cantools.database.can.Signal(name="Signal2", start=16, length=16, scale=0.1, offset=0.0, minimum=0.0, maximum=100.0),
    ]
)

db.add_message(msg_def)
```

#### Load database from DBC file

```python
import cantools

db = cantools.database.load_file("database.dbc")
msg_def = db.get_message_by_name("Message1")
```

#### Encode

Encode signal values into the byte payload of a `can.Message`:

```python
data = msg_def.encode({"Signal1": 1.0, "Signal2": 42.5})
msg_tx = can.Message(arbitration_id=msg_def.frame_id,
                     is_extended_id=msg_def.is_extended_frame,
                     data=data)

with can.Bus(**configs[0], bitrate=250_000, data_bitrate=1_000_000) as bus:
    bus.send(msg_tx)
```

#### Decode

Decode the byte payload of a received `can.Message` back into signal values:

```python
with can.Bus(**configs[0], bitrate=250_000, data_bitrate=1_000_000) as bus:
    msg_rx = bus.recv(timeout=1.0)
    if msg_rx:
        signals = db.decode_message(msg_rx.arbitration_id, msg_rx.data)
        print(signals)  # e.g. {'Signal1': 1.0, 'Signal2': 42.5}
```

### asammdf

[asammdf](https://github.com/danielhrisca/asammdf) is a Python package for reading and writing MDF (Measurement Data Format) files.

When `asammdf` is installed, python-can automatically gains support for reading MDF log files via `can.LogReader`, allowing MDF recordings to be played back directly using `can.MessageSync`:

#### Installation

```bash
pip install asammdf
```

#### Playback of MDF log file

```python
with can.Bus(**configs[0], bitrate=250_000, data_bitrate=1_000_000) as bus:
    with can.LogReader("recording.mf4") as reader:
        for msg in can.MessageSync(messages=reader):
            bus.send(msg)
```
