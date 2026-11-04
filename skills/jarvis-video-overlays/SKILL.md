---
name: jarvis-video-overlays
description: "Use when adding animated broadcast badges to video."
version: 1.0.0
author: __JARVIS_USER__; Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [jarvis, video, overlay, ffmpeg, graphics, candidate]
---

# Jarvis Video Overlays

> **Prefer the `video-use` skill** for any video edit: its `helpers/text_overlay.py badge "45" --subtext "NÚMERO" --brand edit/brand.json` renders this same badge (glass card, accent pill, label over a big value) as a transparent clip, and `render.py` composites it with fades and positioning from the EDL. Use the template below only outside a video-use project.

A verified pipeline to design high-impact broadcast overlay badges (candidate numbers, titles, lower-thirds) with PIL and render them onto videos with smooth FFmpeg alpha transitions.

## Features
- **Typography**: High-impact bold sans-serif fonts (Arial Black / Segoe UI Black).
- **Glassmorphism**: Dark semi-transparent card background with fine outline highlights and vibrant accent pill bars.
- **Alpha Transitions**: Native FFmpeg `fade=in` and `fade=out` applied directly to looped PNG overlay streams (`-loop 1`).

## Execution Script Template

```python
import os, subprocess
from PIL import Image, ImageDraw, ImageFont

# 1. Generate PNG Glassmorphic Badges
def create_badge(num_str, label, color_rgb, output_path):
    font_num = ImageFont.truetype('C:/Windows/Fonts/ariblk.ttf', 85)
    font_label = ImageFont.truetype('C:/Windows/Fonts/segoeuib.ttf', 30)
    
    bbox = font_num.getbbox(num_str)
    w_num = bbox[2] - bbox[0]
    w, h = max(440, w_num + 140), 160
    
    card = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)
    
    # Glassmorphism background
    draw.rounded_rectangle([0, 0, w-1, h-1], radius=24, fill=(15, 23, 42, 235), outline=(255, 255, 255, 220), width=3)
    # Accent bar
    draw.rounded_rectangle([12, 16, 28, h-16], radius=8, fill=color_rgb + (255,))
    
    # Text
    draw.text((46, 20), label, font=font_label, fill=(226, 232, 240, 255))
    draw.text((49, 61), num_str, font=font_num, fill=(0, 0, 0, 180))  # Shadow
    draw.text((46, 58), num_str, font=font_num, fill=(255, 255, 255, 255))
    
    card.save(output_path, 'PNG')

# 2. Render Video with FFmpeg
# Filter format:
# [1:v]format=rgba,fade=in:st=START:d=0.5:alpha=1,fade=out:st=END-0.5:d=0.5:alpha=1[p1]
# [0:v][p1]overlay=x=60:y=100:enable='between(t,START,END)'[v1]
```

## Verification Checklist
- [x] Font loaded without fallback errors.
- [x] `-loop 1` parameter added before each PNG input stream.
- [x] Video rendered with `-shortest` and verified via `vision_analyze`.
