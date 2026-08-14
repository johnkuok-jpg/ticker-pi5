# MIT License — Copyright (c) 2026 John Kuok

# systemd units

`install.sh` copies these units into `/etc/systemd/system/`, reloads systemd, and enables both services.

- `ticker.service` runs the PIO/GPIO renderer as `root`.
- `ticker-web.service` runs Gunicorn as `pi` on port 8080.
- `ticker-updater.service` + `ticker-updater.timer` poll GitHub every 5 minutes and pull new commits (opt-in; see "Fleet auto-update" in the top-level README).

Useful commands:

```bash
sudo systemctl status ticker ticker-web
sudo systemctl restart ticker ticker-web
journalctl -u ticker -f
journalctl -u ticker-web -f
```

After changing a unit, run `sudo systemctl daemon-reload` before restarting it.

## Fleet auto-update

Enable per-Pi with:

```bash
sudo systemctl enable --now ticker-updater.timer
```

Disable (freeze on the current commit) with:

```bash
sudo systemctl disable --now ticker-updater.timer
```

Watch a live rollout:

```bash
journalctl -u ticker-updater.service -f
```

Force an immediate poll:

```bash
sudo systemctl start ticker-updater.service
```
