"""Desktop window that only displays the console served by the background service.

The monitoring (pump, ATLs, Janis, Sheets, Slack, transfer logic) runs in the
``HeliumRecovery`` Windows service (``heliumrecovery serve``, see
scripts/install_service.ps1). This window is just a viewer of
``http://127.0.0.1:<HR_UI_PORT>/``: closing it stops nothing, and opening it
twice is harmless. While the service is not reachable it shows a waiting page
that switches to the console as soon as the service answers.
"""

from __future__ import annotations

import socket

WAITING_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Helium Recovery</title>
<style>body{font-family:Segoe UI,Arial,sans-serif;background:#f6f8fa;color:#192d40;display:flex;
align-items:center;justify-content:center;height:100vh;margin:0}
.box{text-align:center;max-width:560px}h1{font-weight:500;font-size:26px}p{color:#617387;font-size:15px}
code{background:#eaf2fa;padding:2px 6px;border-radius:4px}</style></head><body><div class="box">
<h1>Waiting for the Helium Recovery service…</h1>
<p>Nothing answers at <code>%(url)s</code> yet. The console opens here by itself when the service is running.</p>
<p>Check it with <code>sc query HeliumRecovery</code>; start it with
<code>nssm start HeliumRecovery</code> (administrator), or for a one-off run without the service:
<code>uv run heliumrecovery desktop</code>.</p>
<p id="s">checking…</p></div>
<script>
const url = "%(url)s";
let n = 0;
async function poll() {
  try { await fetch(url, {mode: "no-cors", cache: "no-store"}); location.replace(url); }
  catch (e) { document.getElementById("s").textContent = "still waiting (" + (++n) + ")"; setTimeout(poll, 3000); }
}
poll();
</script></body></html>"""


def service_up(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def view(port: int, size: tuple[int, int] = (1600, 960), host: str = "127.0.0.1") -> None:
    import webview  # pywebview

    url = f"http://{host}:{port}/"
    w, h = size
    if service_up(host, port):
        webview.create_window("Helium Recovery", url, width=w, height=h)
    else:
        webview.create_window("Helium Recovery", html=WAITING_PAGE % {"url": url}, width=w, height=h)
    webview.start()
