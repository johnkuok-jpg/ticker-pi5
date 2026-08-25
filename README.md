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

Read the script before piping it to `bash`; it installs system packages, clones the repository into `/home/pi/ticker-pi5`, creates a virtual environment, installs the renderer + web + Wi-Fi fallback services, and starts them.

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

That gets the renderer and web control panel running. **The Wi-Fi fallback hotspot is optional**, and only sensible on an image where NetworkManager owns the radio (Bookworm defaults) and `nmcli` is present. Skip this block on dhcpcd-based images.

```bash
# Wi-Fi fallback (headless setup hotspot). Skip on non-NetworkManager images.
sudo cp systemd/ticker-wifi.service /etc/systemd/system/
# Validate before installing: a malformed file in /etc/sudoers.d can break sudo.
sudo visudo -c -q -f systemd/ticker-nmcli.sudoers \
  && sudo install -m 0440 -o root -g root systemd/ticker-nmcli.sudoers /etc/sudoers.d/ticker-nmcli
sudo systemctl daemon-reload
sudo systemctl enable --now ticker-wifi
```

Open `http://ticker.local:8080` from a phone on the same network. The page selects any enabled mode (see [Modes](#modes)) by writing `/var/lib/ticker/current_mode`. When that directory cannot be written, the program falls back to `~/.ticker/current_mode`.

## Configuration

Copy `.env.example` to `.env` and edit it. The renderer and web service read it at startup; after editing, run `sudo systemctl restart ticker ticker-web`. If you also changed a `WIFI_SETUP_*` variable, restart `ticker-wifi` too.

### Identity

| Variable | Meaning |
| --- | --- |
| `TICKER_UNIT_NAME` | Label for this physical unit (e.g. `MOM'S TICKER`), shown on the bottom row of the Wi-Fi/network screen once connected. Every unit runs identical code from the same git remote, so this is the only thing (besides the hostname set at flash time) that distinguishes one board from another once you have more than one. Leave blank to show `ticker.local:8080` instead. |

### Display

| Variable | Meaning |
| --- | --- |
| `TICKER_WIDTH` | Display width; use `128` for two horizontal 64-pixel panels. |
| `TICKER_HEIGHT` | Display height; use `32`. |
| `TICKER_ADDR_LINES` | HUB75 address lines; use `4` for these panels. |
| `TICKER_CHANNEL_ORDER` | Channel order the panels expect, any permutation of `rgb`. Use `rbg` if yellow and pink appear swapped. |
| `TICKER_BRIGHTNESS` | Default brightness fraction, from `0.05` to `1.0`. The web slider overrides it persistently. |
| `TICKER_BRIGHTNESS_SCHEDULE` | Optional per-time-of-day steps, e.g. `mon-fri 07:00=55, 22:00=off, weekend 09:00=40`. Malformed entries are skipped rather than crashing the display. |
| `TICKER_FPS` | Render loop target rate; default `30`. |

### Location and time

| Variable | Meaning |
| --- | --- |
| `WEATHER_ZIP` | Optional first-boot seed for the weather/air location. A 5-digit US ZIP, geocoded once and cached in the state dir. Prefer the web UI's "Weather location" field, which does the same thing at runtime and takes precedence. |
| `WEATHER_LAT`, `WEATHER_LON` | Fallback coordinates for the US National Weather Service forecast and Open-Meteo air quality, used by `weather` and `aqi` when no ZIP has been set. |
| `WEATHER_USER_AGENT` | NWS-compliant identifier sent with its requests. Leave the supplied value unless you host a fork. |
| `TICKER_TIMEZONE` | IANA zone (e.g. `America/Los_Angeles`) used by `market`, `worldclock`, and the brightness schedule. Defaults to the system's local zone. |
| `TICKER_CLOCK_24H` | `true` to render clocks in 24-hour form; default is 12-hour with `AM`/`PM`. |

### Stocks, crypto, news

| Variable | Meaning |
| --- | --- |
| `TICKER_SYMBOLS` | Comma-separated symbols, for example `AAPL,NVDA,SPY,BTC-USD`. |
| `FINNHUB_API_KEY` | Preferred stock quote source (real-time US equities). If unset, quotes fall back to Yahoo Finance (~15–20 min delayed). Cached for 60 seconds either way. |
| `STOCKS_LAYOUT` | `card` (default, one ticker at a time with logo) or `list` (compact multi-line). |
| `CRYPTO_SYMBOLS` | Comma-separated coins for `crypto` mode, quoted in USD, for example `BTC,ETH,SOL`. Up to three are shown. |
| `NEWS_FEED_URL` | RSS/Atom feed URL; defaults to CNBC Markets. |
| `NEWS_SOURCE_NAME` | Dim prefix shown above news headlines; defaults to `CNBC MARKETS`. |

### Flights, transit

| Variable | Meaning |
| --- | --- |
| `FLIGHT_NUMBER` | Default flight number the panel tracks (e.g. `UA123`). The web control panel overrides it persistently. |
| `FLIGHT_AIRPORT` | Alternative to a flight number: an airport code (e.g. `SFO`) picks whichever airborne aircraft is heading there. |
| `BART_STATION` | Four-letter BART station abbreviation for the departure board, for example `EMBR`. The web app's dropdown overrides it persistently. |
| `BIKE_STATION_ID` | Bay Wheels station ID for the `bikes` mode. The web app has a station picker. |

### Personalisation

| Variable | Meaning |
| --- | --- |
| `NAMETAG_NAME` | Text drawn by the `nametag` mode. |
| `NAMETAG_COLOR` | Hex color for the name tag, e.g. `#FFAA00` or `FA0`. Defaults to white. |
| `NAMETAG_FONT` | `spleen` (default, auto-shrink ladder), `terminus` (badge look), or `scientifica` (tall condensed). |

### Spotify

| Variable | Meaning |
| --- | --- |
| `SPOTIFY_CLIENT_ID` | Spotify developer app client ID. The `spotify` mode is a placeholder until this is set. |
| `SPOTIFY_CLIENT_SECRET` | Spotify developer app client secret. |
| `SPOTIFY_REDIRECT_URI` | OAuth redirect URI; defaults to `http://127.0.0.1:8080/spotify/callback`. |

### Wi-Fi fallback hotspot

| Variable | Meaning |
| --- | --- |
| `WIFI_SETUP_SSID` | Name of the fallback setup hotspot; defaults to `TICKER-SETUP`. |
| `WIFI_SETUP_PASSWORD` | Fixed password for that hotspot. Leave unset and one is generated once, stored in `/var/lib/ticker/hotspot_password`, and shown on the panel. Values under 8 characters are ignored, since WPA2 rejects them. |

## Modes

The web control panel toggles modes on and off individually; the current selection is written to disk so it survives restarts.

| Mode | What it shows |
| --- | --- |
| `stocks` | Symbol, price, and percentage change from Finnhub (with Yahoo Finance fallback), 60-second cache, green = up / red = down. |
| `news` | RSS headlines refresh every five minutes and scroll continuously (default: CNBC Markets). |
| `weather` | NWS point + grid forecast, cached 10 minutes; temperature, condition, high/low, wind. |
| `flights` | Tracks one flight or one destination airport: arrival, delay, terminal, gate, baggage, with a progress bar and airline tile. Live ADS-B is the fallback when Flightradar24 has no schedule. |
| `market` | US session clock — `OPEN`, `PRE`, `AFTER`, `CLOSED`, `WEEKEND` — plus countdown and progress bar. NYSE holidays and 1 pm early closes are compiled in for 2026–2027; past that the panel says `NO HOLIDAY DATA` rather than guessing. Extend `HOLIDAYS`/`EARLY_CLOSES` in `src/ticker/market.py` when NYSE publishes the next year. |
| `crypto` | 24-hour price and change for up to three coins from Coinbase's keyless endpoint. Two coins render in 6×12 type; a third drops all rows to 5×8. |
| `bart` | Next three trains from one station, soonest first, from BART's public real-time ETD API (no key needed). Destinations are drawn in the line colour; countdowns turn amber when BART reports the train delayed; `NOW` means doors are open. |
| `aqi` | US AQI and PM2.5 for the weather coordinates from Open-Meteo's keyless air-quality API, with a 24-hour trend chart whose baseline is 50 (Good/Moderate boundary). Uses the EPA's own category colours, lifted toward white only where a category would otherwise fall below the panel's legibility floor. |
| `bikes` | One Bay Wheels station: ebikes, classic bikes, and open docks, colour-coded so a glance answers "can I take one?" and "will I be able to return it?" |
| `nametag` | A single name in bold. Three font families available (`spleen`, `terminus`, `scientifica`) and any hex colour. Long names scroll rather than truncate. |
| `spotify` | Now-playing track: album art, title, artist, and progress bar. Requires a Spotify developer app; falls back to a terse placeholder until connected. |
| `pokemon` | Who's That Pokémon — a random Gen 1 silhouette dissolves into its colored sprite, with the name scrolling on the right. Passive; no scoring. |
| `focus` | Countdown timer with an animated hourglass. Presets from the web app; digits and bar turn red in the final seconds. |
| `worldclock` | One large home dial plus two secondary dials, city labels underneath. |
| `net` | The ticker's own network: SSID, IPv4 address in 6×12 type, and a four-bar signal reading. Also the screen the Wi-Fi fallback forces on when setup is needed. |
| `youtube` | Actual video playback in the left 57 columns, with scrolling title/channel/views on the right. Sourced from YouTube's public global music chart via `yt-dlp` (no API key). |
| `commute` | Door-to-door minutes from home to work via Google Maps Directions API. Picks between transit, driving, walking, and biking from the web app. Deliberately tap-to-update: the panel holds the last fetched number until you tap Route now again, so API spend stays gated on intent rather than a background timer. Requires `GOOGLE_MAPS_API_KEY`. |

### Add a mode

1. Create `src/ticker/modes/example.py` and extend `Mode` from `base.py`.
2. Implement `render(self, canvas, tick)`; the renderer clears the canvas and invokes it every frame.
3. Register the class in `MODE_TYPES` in `src/ticker/modes/__init__.py`.
4. Add its name to `VALID_MODES` in `src/ticker/config.py`, a label if you want the panel and web app to say something other than the raw key, and a config card in `settings.html` only when the mode needs user controls. The mode button grid and visible-modes toggles are rendered from the registry automatically.

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
   `http://10.42.0.1:8080/settings`.
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
`nmcli` through a narrow `/etc/sudoers.d/ticker-nmcli` rule that permits exactly
these six actions and nothing else: `nmcli device wifi connect`, `nmcli device
wifi hotspot`, `nmcli connection up id`, `nmcli connection down id`, `nmcli
connection delete id`, and `nmcli connection modify`.

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

# Follow Wi-Fi fallback logs
journalctl -u ticker-wifi -f

# Update a deployed checkout
/home/pi/ticker-pi5/scripts/update.sh
```

## Fleet auto-update

Fleet auto-update keeps multiple deployed tickers on the same commit. Every
install ships a `ticker-updater` systemd timer that polls this GitHub repo
every 5 minutes and, on any new commit to `main`, runs `scripts/update.sh`
to pull and restart the services.

One repo, one branch, one push -- every online Pi in the fleet converges on
the same code within a few minutes. Offline Pis catch up when they come back.

Opt in when installing:

```bash
TICKER_AUTO_UPDATE=1 scripts/install.sh
```

Or enable it on an already-installed Pi:

```bash
sudo systemctl enable --now ticker-updater.timer
```

Watch a rollout:

```bash
journalctl -u ticker-updater.service -f
```

Freeze a Pi on its current commit (e.g. the maintainer's own bench during
active development):

```bash
sudo systemctl disable --now ticker-updater.timer
```

Tune the poll interval by editing `systemd/ticker-updater.timer` in the
repo. The change reaches deployed Pis the next time the currently-installed
timer fires: that scheduled run copies the revised unit into place and
reloads systemd, and every subsequent poll uses the new interval. See
`systemd/README.md` for more.

## Troubleshooting

| Symptom | Checks |
| --- | --- |
| No display | Confirm the Bonnet barrel jack receives the 5 V / 4 A panel supply, the Pi has its separate USB-C supply, ribbon cable pin 1 orientation is correct, and `journalctl -u ticker -f` has no PIO error. Verify `/dev/pio0` exists; update Pi firmware/kernel if it does not. |
| Wrong colors | Re-seat the HUB75 cable and confirm both panels have identical orientation. Some panel batches have nonstandard color ordering; verify panel documentation before changing hardware or software. |
| Flicker or dim output | Lower `TICKER_BRIGHTNESS`, verify the panel power supply is rated and connected directly to the Bonnet barrel jack, and keep the ribbon cable short. |
| Mode does not switch | Check that `/var/lib/ticker/current_mode` exists and is writable by `pi`; run `sudo install -d -m 0775 -o pi -g pi /var/lib/ticker`, then restart both services. The renderer polls the file about once a second. |
| Panel B is dark | Ensure Panel B's input is connected to Panel A's output, not a second Bonnet port, and that the display geometry remains 128×32. Re-seat the inter-panel ribbon cable. |
| Hotspot appears but the page will not load | Confirm the phone joined `TICKER-SETUP` and not a remembered network, then open `http://10.42.0.1:8080/settings` by address — `ticker.local` cannot resolve on the hotspot. |
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
