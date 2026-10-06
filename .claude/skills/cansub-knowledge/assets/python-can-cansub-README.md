# python-can-cansub

A [python-can](https://python-can.readthedocs.io/) integration for the [CANsub](https://csselectronics.com/) CAN bus interface family by CSS Electronics. Source on [GitHub](https://github.com/CSS-Electronics/python-can-cansub).

This package registers the CANsub as a standard python-can interface, making it compatible with all python-can tools and workflows. It also adds a CSV logger compatible with the *webCAN* browser tool provided with the device.

> **Tip:** This README is optimized for LLMs. When using an AI coding assistant with this package, provide this file as context for accurate results.

## Compatibility

The python-can-cansub package and the CANsub device communicate over a versioned API. They are compatible when the package supports the API version used by the device firmware.

- Each **python-can-cansub** release supports one API version. The supported API version for each release is listed in the [python-can-cansub changelog](https://github.com/CSS-Electronics/python-can-cansub/blob/master/CHANGELOG.md).
- Each **CANsub firmware** release uses one API version. The API version for each firmware release is listed in the CANsub changelog (provided with the device documentation).

To check compatibility, look up the API version of the python-can-cansub release and of the device firmware release in their respective changelogs. If they match, they are compatible.

Device *auto-detection* (see *Configuration*) skips devices with an unsupported API version and emits a `UserWarning`. Opening a bus on such a device raises `can.exceptions.CanInitializationError`. In either case, update the package or the device firmware so their API versions align.

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

In python-can, a hardware *configuration* is defined by an `interface` and a `channel` (a single interface can have multiple channels).

The CANsub `interface` is always `"cansub"`. The `channel` is constructed from the device's unique hostname and the channel index.

| Connection | Hostname                | python-can `channel` string       |
|------------|-------------------------|-----------------------------------|
| USB        | `[DEVICE-ID]-usb.local` | `[DEVICE-ID]-usb.local@[channel]` |
| Ethernet   | `[DEVICE-ID]-eth.local` | `[DEVICE-ID]-eth.local@[channel]` |

The `[DEVICE-ID]` is printed on the device label. Channel indexing is **1-based** - the first channel is `1`.

A configuration is passed to `can.Bus` to open a bus.

#### Fixed

Example of a fixed configuration:

```python
configs = [{"interface": "cansub", "channel": "aabbccdd-usb.local@1"},
           {"interface": "cansub", "channel": "aabbccdd-usb.local@2"}]
```

#### Auto-detect

Example of using `detect_available_configs` to automatically discover all connected CANsub devices and channels via mDNS:

```python
configs = can.detect_available_configs(interfaces=["cansub"])
# e.g. [{"interface": "cansub", "channel": "aabbccdd-usb.local@1"},
#       {"interface": "cansub", "channel": "aabbccdd-usb.local@2"},
#       {"interface": "cansub", "channel": "11223344-eth.local@1"},
#       {"interface": "cansub", "channel": "11223344-eth.local@2"}]
```

In the above example, two CANsub devices are detected, each with two channels. One device is connected via USB and the other via Ethernet.

> **Note:** mDNS discovery relies on inbound UDP port 5353. If a host firewall blocks it, no devices are detected (a fixed configuration still works).

### Opening a Bus

The python-can-cansub constructor implements the required arguments for can.Bus and adds custom arguments specific to python-can-cansub. See the python-can-cansub constructor docstring for additional information.

A bus is opened by passing a configuration to `can.Bus`:

```python
with can.Bus(interface="cansub", channel="aabbccdd-usb.local@1", bitrate=250_000, data_bitrate=1_000_000) as bus:
    pass
```

`**config` unpacks a config dict directly into `can.Bus` keyword arguments - convenient with auto-detected configs, and for opening multiple buses:

```python
with (can.Bus(**configs[0], bitrate=250_000, data_bitrate=1_000_000) as bus1,
      can.Bus(**configs[1], bitrate=250_000, data_bitrate=1_000_000) as bus2):
    pass
```

### Device Time

On open, the device clock is compared to the host clock and set to the host time (UTC) if the two differ by more than `max_device_time_skew` seconds (default `1.0`). Pass `0.0` to always set the device clock, or `None` to never set it.

> **Note:** Setting the device clock overrides and disables the device time synchronization (PTP) for the rest of the device power cycle. When using PTP, pass `max_device_time_skew=None` to leave the synchronized device clock untouched:

```python
with can.Bus(**configs[0], bitrate=250_000, data_bitrate=1_000_000, max_device_time_skew=None) as bus:
    pass
```

### Bit Timing

`bitrate` and `data_bitrate` configure the bus with a fixed sample point of 80%. For full control of the bit timing (sample point, SJW), pass a `can.BitTiming` (classic CAN) or `can.BitTimingFd` (CAN FD) as `timing` instead. The CANsub CAN clock is 80 MHz:

```python
timing = can.BitTimingFd.from_sample_point(f_clock=80_000_000,
                                           nom_bitrate=250_000, nom_sample_point=87.5,
                                           data_bitrate=1_000_000, data_sample_point=87.5)

with can.Bus(**configs[0], timing=timing) as bus:
    pass
```

### TLS / Certificates

The data connection to the device is secured by TLS, with the device certificate verified against the built-in CANsub root certificate. When connecting via an IP address (which carries no name to verify the certificate hostname against), hostname verification is automatically disabled - the certificate chain is still verified.

When TLS mutual authentication is enabled, `client_cert` can be used to provide a tuple of paths to the client certificate (`.crt` file) and its unencrypted private key (`.key` file):

```python
with can.Bus(**configs[0], client_cert=("/path/to/client.crt", "/path/to/client.key"), bitrate=250_000, data_bitrate=1_000_000) as bus:
    pass
```

### Error Frames

Error frame reporting is disabled by default; enable it by passing `error_frames=True` to `can.Bus`. Bus errors are then received as a `can.Message` with `is_error_frame` set. The error type is encoded in `arbitration_id`, which can be converted to a `CanSubErrorFrameType` enum:

```python
from python_can_cansub import CanSubErrorFrameType

with can.Bus(**configs[0], bitrate=250_000, data_bitrate=1_000_000, error_frames=True) as bus:
    msg = bus.recv(timeout=1.0)
    if msg and msg.is_error_frame:
        error_type = CanSubErrorFrameType(msg.arbitration_id)
        print(f"Bus error: {error_type.name}")  # e.g. "Bus error: ACK"
```

### Bus State

`bus.state` queries the current channel state: `can.BusState.ACTIVE` (error-active or error-warning), `can.BusState.PASSIVE` (error-passive), or `can.BusState.ERROR` (bus-off). Reading the state of a stopped channel (e.g. after a connection loss) or of a closed bus raises `can.exceptions.CanOperationError`:

```python
with can.Bus(**configs[0], bitrate=250_000, data_bitrate=1_000_000) as bus:
    print(bus.state)  # e.g. "BusState.ACTIVE"
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

### Receive and Transmit

A bus receives the messages transmitted by the other nodes on the CAN bus. Messages transmitted by the bus itself are not received, unless the bus is opened with `receive_own_messages=True`.

```python
with can.Bus(**configs[0], bitrate=250_000, data_bitrate=1_000_000) as bus:

    # Transmit
    msg_tx = can.Message(is_extended_id=False, arbitration_id=0x123, data=[0x01, 0x02, 0x03, 0x04])
    bus.send(msg_tx)

    # Receive with timeout
    msg_rx = bus.recv(timeout=1.0)
    print(msg_rx)
```

#### TX Acknowledgement

With `receive_own_messages=True`, the bus receives its own transmitted messages (marked with `is_rx=False`) once they have been acknowledged on the CAN bus. This tx-ack can be used to wait for a message to be transmitted before proceeding, e.g. transmitting the next message:

```python
msgs = [
    can.Message(is_extended_id=False, arbitration_id=0x123, data=[0x01, 0x02, 0x03, 0x04]),
    can.Message(is_extended_id=False, arbitration_id=0x123, data=[0x05, 0x06, 0x07, 0x08]),
]

with can.Bus(**configs[0], bitrate=250_000, data_bitrate=1_000_000, receive_own_messages=True) as bus:
    for msg_tx in msgs:
        # Transmit
        bus.send(msg_tx)

        # Wait for the tx-ack before transmitting the next message
        msg_ack = bus.recv()
        print(msg_ack)
```

#### CAN FD

CAN FD frames are transmitted by setting `is_fd` (and typically `bitrate_switch`, which switches to `data_bitrate` for the payload). FD payloads can be up to 64 bytes. Transmitting an FD frame requires the bus to be opened with a `data_bitrate` (or an FD bit timing); on a classic CAN bus, FD transmission raises `can.exceptions.CanOperationError`:

```python
with can.Bus(**configs[0], bitrate=250_000, data_bitrate=1_000_000) as bus:
    msg_fd = can.Message(is_extended_id=False, arbitration_id=0x123,
                         is_fd=True, bitrate_switch=True, data=bytes(range(64)))
    bus.send(msg_fd)
```

### Error Handling

Operations on a failed bus raise `can.exceptions.CanOperationError`: `send()` and `recv()` raise it once the connection to the device is lost (detected within seconds, also on an idle bus). `send()` additionally raises `can.exceptions.CanTimeoutError` when the transmit queue stays full for the full timeout (back-pressure from a slow or blocked CAN bus). Failures to open a bus raise `can.exceptions.CanInitializationError`.

A failed bus does not recover, and the package does not reconnect automatically. To recover from a connection loss, close the failed bus and open a new one:

```python
try:
    msg = bus.recv(timeout=1.0)
except can.CanOperationError:
    # Connection lost: the bus cannot recover - close it and open a new one
    bus.shutdown()
    bus = can.Bus(**configs[0], bitrate=250_000, data_bitrate=1_000_000)
```

### Closing a Bus

Closing a bus (leaving the `with` block, or calling `bus.shutdown()`) waits up to `shutdown_timeout` seconds for the queued messages to be transmitted before asking the device to discard them (see the *Opening a Bus*).

Messages received up to (and during) the close remain retrievable with `recv()` after the bus is closed - drain until `None`:

```python
with can.Bus(**configs[0], bitrate=250_000, data_bitrate=1_000_000) as bus:
    bus.send(can.Message(is_extended_id=False, arbitration_id=0x123, data=[0x01, 0x02, 0x03, 0x04]))

# The bus is closed (all queued messages transmitted)
```

> **Tip:** When two buses are connected to the same physical CAN bus (e.g. a transmitter and a receiver), close the transmitting bus first - closing it waits for the queued messages to be transmitted, which requires the other (acknowledging) bus to still be open.

> **Tip:** A `can.Notifier` stops dispatching the moment it is stopped - received messages not yet dispatched are discarded (python-can behavior). Stop the notifier only once the traffic has settled, or read the messages with `recv()` instead.

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

Periodic transmission is offloaded to the CANsub hardware where possible, providing much better transmission time accuracy than a host-scheduled transmission. A host-side background task is used only as a fallback when hardware transmission is not available.

> **Note:** python-can requires all messages in a periodic task to share the same arbitration ID (the payload can differ per frame).

```python
from time import sleep

msgs = [
    can.Message(is_extended_id=False, arbitration_id=0x123, data=[0x01, 0x02, 0x03, 0x04]),
    can.Message(is_extended_id=False, arbitration_id=0x123, data=[0x05, 0x06, 0x07, 0x08]),
    can.Message(is_extended_id=False, arbitration_id=0x123, data=[0x09, 0x0A, 0x0B, 0x0C]),
]

with can.Bus(**configs[0], bitrate=250_000, data_bitrate=1_000_000) as bus:
    # period: time between individual frames (sequence repeats every len(msgs) * period)
    # duration: total transmission time in seconds (None = transmit indefinitely)
    task = bus.send_periodic(msgs, period=0.1, duration=5.0)

    # Perform other tasks here while frames are transmitted in the background
    sleep(6)
```

### Replaying Files

`can.MessageSync` can be used to replay messages from a log file.

```python
with can.Bus(**configs[0], bitrate=250_000, data_bitrate=1_000_000) as bus:
    with can.LogReader("log.csv") as reader:
        for msg in can.MessageSync(messages=reader):
            bus.send(msg)
```

### CSV Logger

On import, this package overrides the default python-can `.csv` reader and writer with a format compatible with the *webCAN* browser tool provided with the device. This applies automatically wherever `.csv` files are read or written, including `can.Logger`, `can.LogReader`, and the command-line tools.

The writer (`CanSubCSVWriter`) and reader (`CanSubCSVReader`) can also be used directly:

```python
from python_can_cansub import CanSubCSVWriter, CanSubCSVReader

# Write received messages to a webCAN-compatible CSV file
with can.Bus(**configs[0], bitrate=250_000, data_bitrate=1_000_000) as bus:
    with CanSubCSVWriter("log.csv") as writer:
        msg = bus.recv(timeout=1.0)
        if msg:
            writer.on_message_received(msg)

# Read messages back from the CSV file
with CanSubCSVReader("log.csv") as reader:
    for msg in reader:
        print(msg)
```

## python-can Tools

python-can includes several command-line tools. All tools accept `--interface` and `--channel` to select the bus, following the same configuration as the API.

The common argument pattern for the CANsub:

```
--interface cansub --channel aabbccdd-usb.local@1 --bitrate 250000 --data-bitrate 1000000
```

Bus arguments without a dedicated command-line flag are passed with `--bus-kwargs key=value ...`. For example, listen-only monitoring:

```
--interface cansub --channel 192.168.1.10@1 --bitrate 250000 --data-bitrate 1000000 --bus-kwargs listen_only=True
```

> **Note:** `--bus-kwargs` consumes all values that follow it; place positional arguments (e.g. the `can_player` log file) before it.

Note that the *filter* argument supported by some command-line tools matches both standard (11-bit) and extended (29-bit) CAN IDs.

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

On Windows, `can_viewer` requires `windows-curses` (`pip install windows-curses`).

### can_bridge

Forward all frames received on one bus to another, in both directions. A CANsub channel can be bridged to any python-can compatible interface.

#### Physical CAN buses

Bridge two physical CAN buses. Frames received on either bus are transmitted on the other, with the bitrate of each bus configured independently.

Any python-can compatible device connected via USB (including a CANsub device), and a CANsub connected via Ethernet:

```
    CAN bus A                                     CAN bus B
===+=========+===                             ===+=========+===
   |         |                                   |         |
[node]   [device]                             [CANsub]   [node]
             |                                   |
             +---- usb -----+     +-- ethernet --+
                            |     |
                          [can_bridge]
```

```bash
can_bridge --bus1-interface INTERFACE --bus1-channel CHANNEL --bus1-bitrate 250000 \
           --bus2-interface cansub --bus2-channel 55667788-eth.local@1 --bus2-bitrate 500000
```

Two CANsub devices connected via Ethernet:

```
    CAN bus A                                     CAN bus B
===+=========+===                             ===+=========+===
   |         |                                   |         |
[node]   [CANsub]                             [CANsub]   [node]
             |                                   |
             +-- ethernet --+     +-- ethernet --+
                            |     |
                          [can_bridge]
```

```bash
can_bridge --bus1-interface cansub --bus1-channel 11223344-eth.local@1 --bus1-bitrate 250000 \
           --bus2-interface cansub --bus2-channel 55667788-eth.local@1 --bus2-bitrate 500000
```

> **Warning:** To avoid loops, do not connect both ends of the bridge to the same bus, and do not enable `receive_own_messages` when both ends are CANsub devices.

#### SocketCAN virtual interface

Bridge CANsub channel 1 to a SocketCAN virtual interface `vcan1` (Linux), making it available to SocketCAN tools such as `candump`. The virtual side uses the `socketcan_vcan` interface (registered by this package) to keep the device timestamps of the messages. With `receive_own_messages=True`, transmission acknowledgements also appear on `vcan1`.

```bash
sudo ip link add dev vcan1 type vcan && sudo ip link set vcan1 up

can_bridge --bus1-interface cansub --bus1-channel aabbccdd-usb.local@1 \
           --bus1-bitrate 250000 --bus1-data-bitrate 1000000 \
           --bus1-bus-kwargs receive_own_messages=True \
           --bus2-interface socketcan_vcan --bus2-channel vcan1
```

In a separate terminal, show the frames on `vcan1`:

```bash
candump vcan1 -tA
```

> **Note:** The direction flag becomes invalid when bridging to a SocketCAN interface.

> **Note:** With `receive_own_messages=True`, transmission acknowledgements appear on the vcan interface with device timestamps. Be aware that other tools on the vcan interface receive both the transmission request (host time) and the transmission acknowledgement (device time).

> **Note:** `socketcan_vcan` passes the timestamps to the kernel with `SO_TXTIME`/`SCM_TXTIME`, which is not a documented use. Kernels older than Linux 5.19 ignore the timestamps.

### can_logconvert

Convert a log file between formats; the format is inferred from the file extension:

```bash
can_logconvert log.csv log.asc
```

## Related Packages

The following packages complement `python-can-cansub` and are included here as inspiration for working with CAN data in Python.

### cantools

[cantools](https://github.com/cantools/cantools) is a Python package for encoding and decoding CAN messages. Encoding/decoding rules can be created directly in code, or loaded from DBC and other database file formats. It works directly with `can.Message` objects from python-can.

#### Installation

```bash
pip install cantools
```

#### Create database in code

A database can be constructed directly in Python without a database file:

```python
import cantools
from cantools.database.conversion import LinearConversion

msg_def = cantools.database.can.Message(
    frame_id=0x123,
    name="Message1",
    length=8,
    signals=[
        cantools.database.can.Signal(name="Signal1", start=0, length=16,
                                     conversion=LinearConversion(scale=0.1, offset=0.0, is_float=False),
                                     minimum=0.0, maximum=100.0),
        cantools.database.can.Signal(name="Signal2", start=16, length=16,
                                     conversion=LinearConversion(scale=0.1, offset=0.0, is_float=False),
                                     minimum=0.0, maximum=100.0),
    ]
)

db = cantools.database.Database(messages=[msg_def])
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
