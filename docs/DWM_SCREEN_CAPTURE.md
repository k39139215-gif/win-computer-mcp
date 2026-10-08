# DWM Occlusion & Fast Screen Capture Architecture

## Overview
Standard BitBlt screenshot methods often capture blank black frames or occluded borders when targeting minimized or background windows.
`win-computer-mcp` utilizes direct Desktop Window Manager (DWM) composition buffers:

1. **Window Handle Resolution**: Queries active HWNDs via Win32 EnumWindows with sub-10ms latency.
2. **Coordinate Normalization**: Translates high-DPI virtual desktop coordinates into native physical display pixels.
3. **Safety Guardrails**: Strictly prevents agent mouse inputs outside the approved boundary rect.
