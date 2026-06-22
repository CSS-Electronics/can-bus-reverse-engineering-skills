# CAN bus reverse engineering skills

This repo contains [Claude Code](https://www.claude.com/claude-code) skills that can help you reverse engineer raw CAN bus data into decoding rules stored as DBC files. 

Specifically, the skills help you leverage AI/LLM tools like Claude Code and [python-can](https://www.csselectronics.com/pages/python-can-usb-serial-api-stream) scripts to identify which CAN ID and data bits encode a
real-world value (speed, RPM, state of charge, ...), work out its start bit,
length, endianness, scale and offset, and verify the result. 

The skills assume that you are using a [CANsub](https://www.csselectronics.com/products/can-fd-usb-interface-ethernet-cansub-2) CAN bus interface from [CSS Electronics](https://www.csselectronics.com) to either record CSV log files via e.g. the [webCAN](https://www.csselectronics.com/pages/webcan-can-bus-streaming-software-browser) tool, or stream data in real-time via USB/Ethernet.

The repo bundles three skills (auto-discovered when you open the folder in Claude
Code):

- **cansub-reverse-engineering** - the workflow
- **combine-dbc** - merge per-signal DBCs into one
- **cansub-knowledge** - CANsub specs / API reference

<br>

**Note:** This is not a 'polished tool', but an illustration of how you can use the CANsub + Python + AI for CAN sniffing

**Note:** We strongly recommend reading our related article [CAN bus reverse engineering with AI](https://www.csselectronics.com/pages/can-bus-reverse-engineering-ai-llm-claude).

<video src="https://cdn.shopify.com/videos/c/o/v/e384c5a75b7943e681dcbad2d10e230a.mp4" autoplay loop muted playsinline width="100%"></video>


## Recommended hardware

- A [CANsub.2](https://www.csselectronics.com/products/can-fd-usb-interface-ethernet-cansub-2) CAN FD interface with USB/Ethernet
- An [OBD2-DB9 adapter cable](https://www.csselectronics.com/products/obd2-db9-adapter-cable) (and optionally a [contactless adapter](https://www.csselectronics.com/products/contactless-can-bus-reader-adapter))

<img src="https://www.csselectronics.com/cdn/shop/files/CANsub-can-bus-interface-stream-real-time-webcan.png" alt="CANsub CAN bus interface streaming real-time in webCAN" width="25%"> <img src="https://www.csselectronics.com/cdn/shop/products/OBD2-DB9-Adapter-Cable-CAN-Bus.jpg?v=1625644186" alt="OBD2-DB9 adapter cable" width="20%">

## 1. Get the code and install dependencies

1. **Clone the repo** (or download the ZIP from GitHub and extract it)
2. **Install Python** - [Python 3.10+](https://www.python.org/downloads/); on Windows, tick **"Add python.exe to PATH"**. Verify: `python --version`
3. **Install the dependencies** into a local virtual environment (`.venv`) so your system Python stays untouched:
   - **Windows:** double-click **`install.bat`** (or run `install.bat` in a terminal)
   - **macOS / Linux:** `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`

## 2. Set up Claude Code

The below is our recommended setup for new Claude Code users:

1. **Get a [Claude subscription](https://claude.ai)** - Claude Code is included with [Claude Pro / Max](https://www.claude.com/claude-code) (or API billing)
2. **Install [Visual Studio Code](https://code.visualstudio.com)**
3. **Install the Claude Code extension** - Extensions panel (`Ctrl+Shift+X`), search **"Claude Code"**, install, sign in
4. **Open this folder** - *File → Open Folder…* → the cloned repo; skills in `.claude/skills/` load automatically

## 3. Connect the hardware

Plug the CANsub into your computer (USB) and into the vehicle's OBD2 port using the
OBD2-DB9 adapter cable. Start the engine (or set the ignition on) so there's live
CAN traffic to capture. We recommend verifying via [webCAN](https://www.csselectronics.com/pages/webcan-can-bus-streaming-software-browser) that you can stream raw proprietary CAN bus data before proceeding. If not, you may need to consider a [contactless CAN reader](https://www.csselectronics.com/products/contactless-can-bus-reader-adapter). 

## Try it

Open the Claude Code panel in VS Code and ask, for example:

> I've connected my CANsub to my car via the OBD2-DB9 cable. Help me check if there is live proprietary CAN data available - and then help me reverse engineer my door locks

> Reverse engineer Speed and RPM from the proprietary CAN data found in Mercedes-E350-2010-obd2-can.csv (contains OBD2 reference data).

> I have a CANedge log with proprietary vehicle CAN data plus the CANedge's internal GPS/IMU on CAN9 (or a CANmod.gps GPS-to-CAN module). Use the GPS speed as the reference to reverse engineer the proprietary vehicle speed

> Help me reverse engineer Speed from my Opel Astra. I have put the raw CAN data in opel/ along with a video of the speed from my car's dashboard.

> I have a gauge-to-CAN module with 8 gauges connected to my CANsub - help me reverse engineer the 1st gauge position signal.


**Note:** You can use our [CANsub CAN+OBD2 sample data](https://www.csselectronics.com/pages/ai-can-bus-sniffer-data-pack) to test the skill


## Output structure and combining DBCs

Each confirmed signal is saved under `decoding-output/`, grouped by application
(the system under test) and signal:

```
decoding-output/
  <application>/                         e.g. mercedes-e350/
    <signal>/<signal>.dbc                e.g. engine-rpm/engine-rpm.dbc   (one DBC per signal)
    <signal>/<signal>.png                the verify plot (decoded vs reference)
    <signal>/analysis-plots/             survey / correlate / bit-search / fit plots
    <application>.dbc                     the combined DBC across all signals
```

Once you've decoded several signals, ask Claude to merge them into one
application-wide DBC (it uses the **combine-dbc** skill):

> Combine the decoded DBCs for mercedes-e350 into a single DBC.

This produces `decoding-output/<application>/<application>.dbc`. You can then load
that combined DBC in [webCAN](https://www.csselectronics.com/pages/webcan-can-bus-streaming-software-browser)
and stream live from your CANsub to see your reverse-engineered signals decoded in
real time - a final, live confirmation of the results.
