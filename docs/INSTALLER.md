# Interactive Traffic installer

Run on the Linux Docker host, for example the Pi over SSH. MeshMonitor and Docker Compose must already be installed, the container must be running, and `/data/scripts` must be an existing host bind mount. The host user must have Docker access and the same UID as the container's `node` user. This installs the Traffic package, not Docker, MeshMonitor or radio firmware.

From a complete checkout:

```bash
python3 install_traffic.py
```

The wizard prompts for the radio source UUID, Traffic channel index, inherited or explicit hop limit, private output folder, Compose file, service/container names, and a hidden ADOT API key. Get your own key using [the API key guide](GUIDE.md#obtain-and-install-the-adot-api-key). Find the source UUID in the selected radio's MeshMonitor URL; confirm its channel index in MeshMonitor.

It generates disabled automation envelopes and separate `.graph.json` files. A private JSON Compose override supplies the key; the original Compose file remains intact. The wizard validates Compose, asks before installing/recreating, backs up existing scripts and ledger, copies the five scripts, initializes only a missing ledger, recreates only MeshMonitor and checks imports. No test packets or feed requests are sent by the installer.

Optionally, enter a MeshMonitor API token with **automations read/write** permission to create missing global JSON variables and disabled workflows automatically. The token is hidden and is not saved. Remote API URLs require HTTPS; localhost may use HTTP. Authentication failures stop API setup without credential fallbacks. Existing same-name automations are preserved, even if their settings differ; review them yourself. Partial failures can leave files or disabled workflows installed, so review the reported state before retrying.

Without a token, create global JSON variables `traffic` and `traffic_scheduled` with default `{}` through the UI. Use the generated envelopes with an import facility if available; otherwise paste each `.graph.json` into Advanced JSON, enter its envelope name/description and leave Enabled off. The original repository guide covers these steps.

Review source, channels and hops. Disable an old scheduled Traffic timer before enabling the new scheduled workflow. Enable the on-demand workflow when ready. Keep the manual TEST workflow disabled. The selected receiving source filter prevents copies heard by other radios from generating duplicate replies.

## Prepare without changing Docker or installing scripts

```bash
python3 install_traffic.py --prepare-only
```

This prompts only for source/channel/hops/output and creates configured imports. It never asks for keys or tokens. Useful for reviewing setup before installation.

## Future operations and rollback

The key is stored in the private `compose.traffic.json` file, with mode 600 inside a mode-700 output directory outside the repository. Keep that directory private and out of Git. The installer prints and saves the exact Compose command in `OPERATING-NOTES.txt`. **Include both your original Compose file and this override for future recreations/upgrades**, or the API key setting may be lost. Preserve any additional override files you normally use; this installer invokes only the explicitly selected base file and its generated override.

It does not automatically restore files after failure. To roll back, disable Traffic workflows in the UI, restore backed-up scripts, and recreate MeshMonitor using your previous Compose configuration. Preserve the current ledger for investigation; restoring an old ledger can reannounce incidents. The installer does not delete API-created variables or workflows.

This version was checked with offline tests and the installed MeshMonitor 4.17.0-rc2 API schemas. The Docker recreation and authenticated API creation paths have not been run against production by this development task. Concurrent on-demand requests still share variables; the known northern I-19 coverage overlap remains unchanged.
