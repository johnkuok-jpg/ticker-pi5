# ticker-pi5

A small, self-hosted LED ticker for a Raspberry Pi 5. It drives a 128×32 RGB HUB75 display, switches visual modes through a phone-friendly web page, and keeps all control state in a simple shared file so it survives service restarts.

## Hardware bill of materials

- Raspberry Pi 5 running Raspberry Pi OS Lite (64-bit, Bookworm)
- [Adafruit RGB Matrix Bonnet for Raspberry Pi](https://www.adafruit.com/product/3211)
- Two 64×32 P2.5 HUB75 RGB LED panels, chained horizontally as one 128×32 display
- Adafruit 5 V / 4 A power supply connected to the Bonnet barrel jack
- A separate 27 W USB-C power supply for the Pi 5
- Suitable HUB75 ribbon cable(s), standoffs, enclosure, and networking

The panel supply and Pi USB-C supply are separate by design. Connect the first panel to the Bonnet's output and chain the second panel from the first panel's output; make sure both panels use the same orientation.

## Prerequisites

1. Install a fresh Raspberry Pi OS Lite 64-bit Bookworm image.
2. Configure the network and verify SSH access.
3. Update the Pi firmware/kernel if `/dev/pio0` is absent; Pi 5 PIO support is required.
4. Attach the Bonnet and panels only with power disconnected. Power the panels from the Bonnet's barrel jack and the Pi from its own USB-C supply.

## Install

After the repository has been made public to your Pi (or after authenticating GitHub access for the private repository), the one-command installer is:

```bash
curl -sL https://raw.githubusercontent.com/johnkuok-jpg/ticker-pi5/main/scripts/install.sh | bash
```

Read the script before piping it to `bash`; it installs system packages, clones the repository into `/home/pi/ticker-pi5`, creates a virtual environment, enables services, and starts them.

### Manual install

```bash
git clone https://github.com/johnkuok-jpg/ticker-pi5.git /home/pi/ticker-pi5
cd /home/pi/ticker-pi5
sudo apt update
sudo apt install -y python3-venv python3-dev build-essential pkg-config git
python3 -m venv --system-site-packages venv
venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install -r requirements.txt
venv/bin/python -m pip install --editable .
cp .env.example .env
sudo install -d -m 0775 -o pi -g pi /var/lib/ticker
sudo cp systemd/ticker.service systemd/ticker-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ticker ticker-web
```

Open `http://ticker.local:8080` from a phone on the same network. The page selects `stocks`, `news`, `weather`, or `flights` by writing `/var/lib/ticker/current_mode`. When that directory cannot be written, the program uses `~/.ticker/current_mode` instead.

## Configuration

Copy `.env.example` to `.env` and edit it. The renderer and web service read it at startup.

| Variable | Meaning |
| --- | --- |
| `TICKER_WIDTH` | Display width; use `128` for two horizontal 64-pixel panels. |
| `TICKER_HEIGHT` | Display height; use `32`. |
| `TICKER_ADDR_LINES` | HUB75 address lines; use `4` for these panels. |
| `TICKER_CHANNEL_ORDER` | Channel order the panels expect, any permutation of `rgb`. Use `rbg` if yellow and pink appear swapped. |
| `TICKER_BRIGHTNESS` | Default brightness fraction, from `0.05` to `1.0`. The web slider overrides it persistently. |
| `TICKER_FPS` | Render loop target rate; default `30`. |
| `TICKER_SYMBOLS` | Comma-separated symbols, for example `AAPL,NVDA,SPY,BTC-USD`. Quotes use Yahoo Finance and are cached for 60 seconds. |
| `CRYPTO_SYMBOLS` | Comma-separated coins for crypto mode, quoted in USD, for example `BTC,ETH,SOL`. Up to three are shown. |
| `NEWS_FEED_URL` | RSS/Atom feed URL; defaults to AP top news. |
| `NEWS_SOURCE_NAME` | Dim prefix shown above news headlines. |
| `WEATHER_LAT`, `WEATHER_LON` | Coordinates for the US National Weather Service forecast. Required by weather mode. |
| `BART_STATION` | Four-letter BART station abbreviation for the departure board, for example `EMBR`. The web app's dropdown overrides it persistently. |
| `WEATHER_USER_AGENT` | NWS-compliant identifier sent with its requests. Leave the supplied value unless you host a fork. |
| `WIFI_SETUP_SSID` | Name of the fallback setup hotspot; defaults to `TICKER-SETUP`. |
| `WIFI_SETUP_PASSWORD` | Fixed password for that hotspot. Leave unset and one is generated once, stored in `/var/lib/ticker/hotspot_password`, and shown on the panel. Values under 8 characters are ignored, since WPA2 rejects them. |

After editing `.env`, run `sudo systemctl restart ticker ticker-web`.

## Modes

- **stocks** — cached Yahoo Finance prices and percentage change; green is up and red is down.
- **news** — RSS headlines refresh every five minutes and scroll continuously.
- **weather** — NWS point + grid forecast API, cached for 10 minutes; shows temperature, condition, high/low, and wind.
- **flights** — tracks one flight number: arrival time, delay, terminal, gate and baggage carousel, with a progress bar and airline tile. Schedule data comes from Flightradar24's web endpoints; live ADS-B positions are the fallback when no schedule is published. Instead of a flight number the web app can take a destination airport, in which case the panel picks an aircraft that is airborne towards it and re-picks on every switch into the mode.
- **market** — US market session clock: `OPEN`, `PRE`, `AFTER`, `CLOSED` or `WEEKEND`, a countdown to the next change, and a bar showing progress through the trading day. The [NYSE holiday and hours calendar](https://www.nyse.com/markets/hours-calendars) is compiled in for 2026–2027, including the 1:00 pm early closes, so it knows Thanksgiving from a Thursday. Needs no network. Past 2027 it falls back to weekday arithmetic and says `NO HOLIDAY DATA` rather than guessing — extend `HOLIDAYS` and `EARLY_CLOSES` in `src/ticker/market.py` when the NYSE publishes the next year.
- **crypto** — 24-hour price and change for up to three coins from Coinbase's keyless public endpoint, set with `CRYPTO_SYMBOLS`. Two coins render in 6×12 type; a third drops all rows to 5×8. Something stays live on the panel when the equity market is shut.
- **bart** — the next three trains from one station, soonest first, from [BART's public real-time ETD API](https://api.bart.gov/docs/etd/etd.aspx) (no key of your own needed). Destinations are drawn in the line colour rather than beside a colour chip, because five pixels of chip is invisible across a room. Countdowns turn amber when BART reports the train delayed, and `NOW` means the doors are open. Pick the station from the web app's dropdown or set `BART_STATION`.
- **aqi** — current US AQI and PM2.5 for the weather coordinates from [Open-Meteo's keyless air-quality API](https://open-meteo.com/en/docs/air-quality-api), with a 24-hour trend chart whose baseline sits at 50, the Good/Moderate boundary. Colours are the EPA's own category values from the [AQI technical assistance document](https://document.airnow.gov/technical-assistance-document-for-the-reporting-of-daily-air-quailty.pdf), lifted toward white only where a category would otherwise fall below the panel's legibility floor.
- **net** — the ticker's own network: the SSID, the IPv4 address in 6×12 type, and a four-bar signal reading. The address is here because `ticker.local` depends on mDNS, and mDNS is exactly what a hotel or café network with client isolation drops. While the setup hotspot is up this screen turns itself on and shows the hotspot's name, password and address instead — see [Wi-Fi away from home](#wi-fi-away-from-home).

### Add a mode

1. Create `src/ticker/modes/example.py` and extend `Mode` from `base.py`.
2. Implement `render(self, canvas, tick)`; the renderer clears the canvas and invokes it every frame.
3. Register the class in `MODE_TYPES` in `src/ticker/modes/__init__.py`.
4. Add its name to `VALID_MODES` in `src/ticker/config.py` and a button in the template if you want web control.

### Add ticker logos

Drop a PNG named after a symbol in `src/ticker/web/static/logos/`, such as `AAPL.png` or `btc-usd.png`. The stocks mode matches names case-insensitively and resizes each image to 16×16. Keep artwork simple and high-contrast for best legibility.

## Wi-Fi away from home

Take the ticker somewhere it has never been and it has no way to ask for a
password: it is headless, and the only interface is a web app that needs the
network the ticker cannot join. So it broadcasts its own.

**What you do**

1. Plug it in somewhere new and wait about a minute. The panel switches itself to
   the Wi-Fi setup screen and stays there.
2. Read the network name (`TICKER-SETUP`), the password, and the address off the
   panel. The panel brightens to at least 45% for this, so the password is still
   readable if you arrive at night.
3. Join that network from a phone and open the address it shows —
   `http://10.42.0.1:8080/wifi`.
4. Tap the network you want, type its password, and submit. **Your phone will lose
   its connection at that moment** — that is the ticker leaving its own hotspot,
   not a failure. Rejoin the house network and give it fifteen seconds.
5. The panel returns to whatever mode you had selected, showing the new address.

Networks are remembered, so the second visit needs none of this. There is no
button to press and nothing to hold down.

**What it does not handle.** Captive portals — hotel and airport networks with a
terms-of-service page — cannot be accepted from the ticker, because there is no
browser on it. The join will succeed and the internet will still not work.
Enterprise networks needing a username as well as a password are out too, and
5 GHz-only networks are invisible to the Pi 5's radio when it is configured for a
region that restricts them.

**How it works, and why not comitup.** A root daemon (`ticker-wifi.service`) polls
NetworkManager every 20 seconds. Three consecutive readings with no connection —
about a minute, long enough to sit out a booting router — and it raises an access
point with `nmcli device wifi hotspot`, then writes a small JSON notice into
`/var/lib/ticker/network_notice`. The renderer reads that notice once a second and
forces the `net` screen on; it never writes to the saved mode, so the panel
returns to your selection by itself. The web app, which runs unprivileged, reaches
`nmcli` through a narrow `/etc/sudoers.d/ticker-nmcli` rule covering five
subcommands and nothing else.

Every four minutes the daemon drops the access point briefly to listen for a known
network, because a single radio cannot scan while it is being an access point, and
restores it if nothing familiar is in range. That is why the hotspot occasionally
blinks out for a few seconds.

[comitup](http://davesteele.github.io/comitup/) solves the same problem and was
deliberately not used: it ships its own captive web interface on port 80, which
would sit alongside this project's control panel as a second, competing way to
configure the same device.

**If the hotspot never appears**

```bash
systemctl status ticker-wifi
journalctl -u ticker-wifi -n 50
nmcli device status
sudo -n nmcli connection show >/dev/null && echo "sudoers rule OK"
```

An `nmcli` that reports nothing is the one case where the daemon deliberately
does nothing at all: a NetworkManager it cannot read may well be sitting on a
working connection, and tearing that down would strand the ticker rather than
rescue it.

## Operations

```bash
# Follow renderer logs
journalctl -u ticker -f

# Follow web service logs
journalctl -u ticker-web -f

# Update a deployed checkout
/home/pi/ticker-pi5/scripts/update.sh
```

## Troubleshooting

| Symptom | Checks |
| --- | --- |
| No display | Confirm the Bonnet barrel jack receives the 5 V / 4 A panel supply, the Pi has its separate USB-C supply, ribbon cable pin 1 orientation is correct, and `journalctl -u ticker -f` has no PIO error. Verify `/dev/pio0` exists; update Pi firmware/kernel if it does not. |
| Wrong colors | Re-seat the HUB75 cable and confirm both panels have identical orientation. Some panel batches have nonstandard color ordering; verify panel documentation before changing hardware or software. |
| Flicker or dim output | Lower `TICKER_BRIGHTNESS`, verify the panel power supply is rated and connected directly to the Bonnet barrel jack, and keep the ribbon cable short. |
| Mode does not switch | Check that `/var/lib/ticker/current_mode` exists and is writable by `pi`; run `sudo install -d -m 0775 -o pi -g pi /var/lib/ticker`, then restart both services. The renderer polls the file about once a second. |
| Panel B is dark | Ensure Panel B's input is connected to Panel A's output, not a second Bonnet port, and that the display geometry remains 128×32. Re-seat the inter-panel ribbon cable. |
| Hotspot appears but the page will not load | Confirm the phone joined `TICKER-SETUP` and not a remembered network, then open `http://10.42.0.1:8080/wifi` by address — `ticker.local` cannot resolve on the hotspot. |
| Joined a network but nothing works | Almost always a captive portal. The panel will show an address; if that address is reachable and the internet is not, the network wants a browser the ticker does not have. |
| Permission errors | The renderer intentionally runs as root for PIO/GPIO access. The web service runs as `pi`; make `/var/lib/ticker` group-writable as shown above. If `/dev/pio0` has restrictive permissions, follow the driver documentation's udev guidance. |

## Development checks

On a development machine without hardware, install the dev dependency and run:

```bash
python3 -m pip install -e '.[dev]'
pytest
python3 -m compileall -q src/
```

The smoke tests do not initialize the hardware driver.

## License

MIT © 2026 John Kuok. See [LICENSE](LICENSE).
