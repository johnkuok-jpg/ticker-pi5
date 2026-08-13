# MIT License — Copyright (c) 2026 John Kuok

# systemd units

`install.sh` copies these units into `/etc/systemd/system/`, reloads systemd, and enables both services.

- `ticker.service` runs the PIO/GPIO renderer as `root`.
- `ticker-web.service` runs Gunicorn as `pi` on port 8080.

Useful commands:

```bash
sudo systemctl status ticker ticker-web
sudo systemctl restart ticker ticker-web
journalctl -u ticker -f
journalctl -u ticker-web -f
```

After changing a unit, run `sudo systemctl daemon-reload` before restarting it.
