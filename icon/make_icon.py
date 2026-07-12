#!/usr/bin/env python3
"""Render Shmoozy's app icon: an Adwaita-blue speech bubble with a typing
indicator on a deep-slate rounded square. Original art, GTK/Adwaita palette —
the Linux counterpart to Chatty's maroon/orange."""
from PIL import Image, ImageDraw

SCALE = 4                       # supersample for smooth edges
S = 1024 * SCALE
SLATE = (36, 31, 49, 255)       # #241F31 background (Adwaita dark)
BLUE = (53, 132, 228, 255)      # #3584E4 bubble (Adwaita blue)

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# Rounded-square background, slight transparent margin.
m = int(54 * SCALE)
d.rounded_rectangle([m, m, S - m, S - m], radius=int(220 * SCALE), fill=SLATE)

# Speech bubble body.
bx0, by0, bx1, by1 = (int(v * SCALE) for v in (250, 300, 774, 650))
d.rounded_rectangle([bx0, by0, bx1, by1], radius=int(96 * SCALE), fill=BLUE)

# Bubble tail (points down-left).
tail = [(int(330 * SCALE), int(610 * SCALE)),
        (int(470 * SCALE), int(610 * SCALE)),
        (int(322 * SCALE), int(772 * SCALE))]
d.polygon(tail, fill=BLUE)

# Three "typing" dots punched in slate.
cy = int(478 * SCALE)
r = int(42 * SCALE)
for cx in (int(382 * SCALE), int(512 * SCALE), int(642 * SCALE)):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=SLATE)

master = img.resize((1024, 1024), Image.LANCZOS)
master.save("icon/icon_1024.png")
master.resize((256, 256), Image.LANCZOS).save("icon/shmoozy-256.png")
print("wrote icon/icon_1024.png and icon/shmoozy-256.png")
