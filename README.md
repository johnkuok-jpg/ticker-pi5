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
| `TICKER_BRIGHTNESS` | Default brightness fraction, from `0.05` to `1.0`. The web slider overrides it persistently. |
| `TICKER_FPS` | Render loop target rate; default `30`. |
| `TICKER_SYMBOLS` | Comma-separated symbols, for example `AAPL,NVDA,SPY,BTC-USD`. Quotes use Yahoo Finance and are cached for 60 seconds. |
| `CRYPTO_SYMBOLS` | Comma-separated coins for crypto mode, quoted in USD, for example `BTC,ETH,SOL`. Up to three are shown. |
| `NEWS_FEED_URL` | RSS/Atom feed URL; defaults to AP top news. |
| `NEWS_SOURCE_NAME` | Dim prefix shown above news headlines. |
| `WEATHER_LAT`, `WEATHER_LON` | Coordinates for the US National Weather Service forecast. Required by weather mode. |
| `BART_STATION` | Four-letter BART station abbreviation for the departure board, for example `EMBR`. The web app's dropdown overrides it persistently. |
| `WEATHER_USER_AGENT` | NWS-compliant identifier sent with its requests. Leave the supplied value unless you host a fork. |

After editing `.env`, run `sudo systemctl restart ticker ticker-web`.

## Modes

- **stocks** — cached Yahoo Finance prices and percentage change; green is up and red is down.
- **news** — RSS headlines refresh every five minutes and scroll continuously.
- **weather** — NWS point + grid forecast API, cached for 10 minutes; shows temperature, condition, high/low, and wind.
- **flights** — tracks one flight number: arrival time, delay, terminal, gate and baggage carousel, with a progress bar and airline tile. Schedule data comes from Flightradar24's web endpoints; live ADS-B positions are the fallback when no schedule is published.
- **market** — US market session clock: `OPEN`, `PRE`, `AFTER`, `CLOSED` or `WEEKEND`, a countdown to the next change, and a bar showing progress through the trading day. The [NYSE holiday and hours calendar](https://www.nyse.com/markets/hours-calendars) is compiled in for 2026–2027, including the 1:00 pm early closes, so it knows Thanksgiving from a Thursday. Needs no network. Past 2027 it falls back to weekday arithmetic and says `NO HOLIDAY DATA` rather than guessing — extend `HOLIDAYS` and `EARLY_CLOSES` in `src/ticker/market.py` when the NYSE publishes the next year.
- **crypto** — 24-hour price and change for up to three coins from Coinbase's keyless public endpoint, set with `CRYPTO_SYMBOLS`. Two coins render in 6×12 type; a third drops all rows to 5×8. Something stays live on the panel when the equity market is shut.
- **bart** — the next three trains from one station, soonest first, from [BART's public real-time ETD API](https://api.bart.gov/docs/etd/etd.aspx) (no key of your own needed). Destinations are drawn in the line colour rather than beside a colour chip, because five pixels of chip is invisible across a room. Countdowns turn amber when BART reports the train delayed, and `NOW` means the doors are open. Pick the station from the web app's dropdown or set `BART_STATION`.
- **aqi** — current US AQI and PM2.5 for the weather coordinates from [Open-Meteo's keyless air-quality API](https://open-meteo.com/en/docs/air-quality-api), with a 24-hour trend chart whose baseline sits at 50, the Good/Moderate boundary. Colours are the EPA's own category values from the [AQI technical assistance document](https://document.airnow.gov/technical-assistance-document-for-the-reporting-of-daily-air-quailty.pdf), lifted toward white only where a category would otherwise fall below the panel's legibility floor.

### Add a mode

1. Create `src/ticker/modes/example.py` and extend `Mode` from `base.py`.
2. Implement `render(self, canvas, tick)`; the renderer clears the canvas and invokes it every frame.
3. Register the class in `MODE_TYPES` in `src/ticker/modes/__init__.py`.
4. Add its name to `VALID_MODES` in `src/ticker/config.py` and a button in the template if you want web control.

### Add ticker logos

Drop a PNG named after a symbol in `src/ticker/web/static/logos/`, such as `AAPL.png` or `btc-usd.png`. The stocks mode matches names case-insensitively and resizes each image to 16×16. Keep artwork simple and high-contrast for best legibility.

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
