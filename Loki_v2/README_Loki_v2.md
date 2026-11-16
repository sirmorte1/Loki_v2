# Loki 2.0

Loki 2.0 is a local Windows desktop AI widget built around Ollama.

## Included

- Local Ollama chat
- Fast/reasoning model configuration
- PC specifications
- CPU details
- CPU/RAM monitoring
- NVIDIA GPU/VRAM/temperature/driver monitoring
- All mounted-drive storage information
- Battery status
- Network adapter information and internet check
- Running-process inspection
- Approved application launching
- Windows master volume and mute control
- Windows audio output listing and default-device switching
- Read-only file/folder listing and file search
- File metadata
- Permissioned file opening
- Confirmation-gated rename/move/delete
- Confirmation-gated shutdown/restart
- In-app scheduled reminders and notifications
- Persistent short-term conversation memory
- Persistent long-term memory storage
- Permission tiers: auto, request, confirm
- Futuristic compact widget UI

## Install

Open PowerShell in this folder:

```powershell
py -m pip install -r requirements.txt
```

Make sure Ollama is running.

## Configuration

Loki loads `models.json`, `router.json`, `prompts.json`, `tools.json`, and
`apps.json` from the `config/` folder. Model names and the Ollama endpoint are
in `config/models.json`; route weights are in `config/router.json`; tool
enablement and permission tiers are in `config/tools.json`; approved app paths
and aliases are in `config/apps.json`. Changes take effect after restarting
Loki.

The UI shows the selected route and model for each response. The fast model is
used for everyday requests and for classification only when the weighted rules
find a request ambiguous.

## Audio

Windows audio control uses Pycaw. If it fails to load, Loki continues running but reports that audio support is unavailable.

## Notes

- Read-only tools are automatically allowed.
- Actions such as opening apps require an explicit user request.
- Destructive/system actions require explicit user request plus confirmation.
- File search is intentionally capped to avoid scanning indefinitely.
- Audio output switching uses the current Pycaw device-switching support. The project pins Pycaw to a release that includes this capability.
- Conversation history, long-term memory, and reminder files are local runtime data and should not be committed.
