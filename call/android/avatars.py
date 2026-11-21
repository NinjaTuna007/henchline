"""Henchline's avatars: the app icon's techno henchman, one flavour per agent, and the boss: you.

    python3 avatars.py [--svg DIR]

Each is drawn once as a list of shapes and written as an Android vector drawable into
app/src/main/res/drawable/ (avatar_<agent>.xml); --svg also writes SVGs into DIR, to look at.
"""
import sys
from pathlib import Path

FLAVOURS = {
    # name: body, highlight, glow, eye style
    "claude": ("#D9774B", "#F2A27E", "#FFD27A", "one"),     # warm coral, one big goggle
    "cursor": ("#3B4250", "#6B7586", "#E8EEF8", "two"),     # graphite, two goggles
    "codex": ("#0F9D76", "#46C9A0", "#9CFFDD", "visor"),    # green, a wide visor
    "handsfree": ("#1E6FE0", "#5FA4FF", "#00E5FF", "one"),  # the app's own blue
}
NAVY, METAL, DARK, PUPIL = "#0A1430", "#B9C6D6", "#0E1F44", "#062048"
GUNMETAL, CUP, HEADSET_RIM = "#4B5563", "#2B2F3A", "#00B8D4"  # the boss's headset
SHIRT = "#1FA5A0"  # the boss's holiday shirt, when away


def shapes(body, light, glow, eyes):
    out = [
        ("circle", dict(cx=54, cy=54, r=54, fill=NAVY)),
        ("path", dict(d="M54,22 V30", stroke=light, sw=2)),
        ("circle", dict(cx=54, cy=20, r=3.2, fill=glow)),
        ("path", dict(d="M34,50 A20,20 0,0 1,74 50 V74 A12,12 0,0 1,62 86 H46 A12,12 0,0 1,34 74 Z", fill=body)),
        ("path", dict(d="M38,50 A16,16 0,0 1,46 36.2", stroke=light, sw=2.4)),
        ("path", dict(d="M40,78 H46 L49,75 M68,78 H62 L59,75 M54,82 V86", stroke=glow, sw=1.2)),
        ("rect", dict(x=33, y=44, w=42, h=7, fill=DARK)),
    ]
    if eyes == "one":
        out += [("circle", dict(cx=54, cy=47.5, r=11, fill=METAL)), ("circle", dict(cx=54, cy=47.5, r=8.4, fill=DARK)),
                ("circle", dict(cx=54, cy=47.5, r=6.8, fill=glow)), ("circle", dict(cx=54, cy=47.5, r=3.2, fill=PUPIL)),
                ("circle", dict(cx=51.6, cy=45.2, r=1.3, fill="#FFFFFF"))]
    elif eyes == "two":
        for cx in (45.5, 62.5):
            out += [("circle", dict(cx=cx, cy=47.5, r=8, fill=METAL)), ("circle", dict(cx=cx, cy=47.5, r=6, fill=DARK)),
                    ("circle", dict(cx=cx, cy=47.5, r=4.8, fill=glow)), ("circle", dict(cx=cx, cy=47.5, r=2.2, fill=PUPIL)),
                    ("circle", dict(cx=cx - 1.6, cy=45.9, r=0.9, fill="#FFFFFF"))]
    else:  # visor
        out += [("rect", dict(x=37, y=41, w=34, h=13, fill=METAL, rx=6.5)), ("rect", dict(x=39, y=43, w=30, h=9, fill=DARK, rx=4.5)),
                ("rect", dict(x=40.5, y=44.5, w=27, h=6, fill=glow, rx=3)), ("rect", dict(x=50, y=45.5, w=8, h=4, fill=PUPIL, rx=2))]
    out.append(("path", dict(d="M47,66 Q54,71 61,66", stroke=glow, sw=2)))
    return out


# Hard hats (the user's pick, A1, 2026-11-21): white for a manager that started others, yellow for its crew,
# none for a chat working solo. The hat carries the antenna, poking out of the crown; the brim stops above the eyes.
HATS = {
    "manager": dict(fill="#F4F7FC", shade="#D5DDEA", hi="#FFFFFF", line="#C9D1E0"),
    "crew": dict(fill="#FFC629", shade="#E09E0E", hi="#FFE27A", line="#B07A08"),
}
HAT_DOME = "M36.5,33.5 C36.5,21 43.5,13 54,13 C64.5,13 71.5,21 71.5,33.5 Z"
HAT_SHADOW = "M61.5,15 C68,18.5 71.5,25 71.5,33.5 H65.5 C65.5,25.5 64.5,19.5 61.5,15 Z"  # right side in shade
HAT_RIDGE = "M51,33.5 L51.6,14.6 Q54,12.6 56.4,14.6 L57,33.5 Z"
HAT_RIBS = "M43.5,33.5 C43.5,24.5 46,18.5 49.8,15.4 M64.5,33.5 C64.5,24.5 62,18.5 58.2,15.4"


def hat(role, light, glow):
    """A full-brimmed hard hat with a front peak, a ridge and two ribs, in the role's colours."""
    c = HATS[role]
    under = ellipse_path(54, 33, 25, 3.2)
    return [
        ("path", dict(d=under, fill=c["line"])),
        ("path", dict(d=ellipse_path(54, 32, 24.6, 2.6), fill=c["fill"])),
        ("path", dict(d="M54,13 V7", stroke=light, sw=2)),  # the antenna, out through the crown
        ("circle", dict(cx=54, cy=5, r=3.2, fill=glow)),
        ("path", dict(d=HAT_DOME, fill=c["fill"])),
        ("path", dict(d=HAT_SHADOW, fill=c["shade"])),
        ("path", dict(d="M40.2,30.5 C40.2,23.5 43,18.5 47.2,16", stroke=c["hi"], sw=2.2)),  # light from the left
        ("path", dict(d=HAT_RIBS, stroke=c["shade"], sw=1.5)),
        ("path", dict(d=HAT_RIDGE, fill=c["hi"])),
        ("path", dict(d="M56.2,15.2 L56.6,32.5", stroke=c["shade"], sw=1.2)),
        ("path", dict(d=HAT_DOME, stroke=c["line"], sw=1)),
        ("path", dict(d="M38,33.4 Q54,32 70,33.4 Q67,36.3 54,36.4 Q41,36.3 38,33.4 Z", fill=c["shade"])),  # peak
        ("path", dict(d="M40,33.3 Q54,32.1 68,33.3", stroke=c["hi"], sw=1)),
        ("path", dict(d=under, stroke=c["line"], sw=0.8)),
    ]


def hatted(body, light, glow, eyes, role):
    """The henchman with its role's hat on: its own antenna gives way to the one the hat carries."""
    items = [i for i in shapes(body, light, glow, eyes)
             if not (i[0] == "circle" and i[1]["cy"] == 20) and not (i[0] == "path" and i[1].get("d") == "M54,22 V30")]
    return items + hat(role, light, glow)


GREY, BLACK, COAT, COAT_DARK, COAT_LINE = "#9AA1AD", "#2B2E36", "#2A2D35", "#1C1E24", "#3C414D"


def bezier(p0, p1, p2, p3, n=24):
    return [tuple((1 - t) ** 3 * a + 3 * (1 - t) ** 2 * t * b + 3 * (1 - t) * t ** 2 * c + t ** 3 * d
                  for a, b, c, d in zip(p0, p1, p2, p3)) for t in (i / n for i in range(n + 1))]


def stripes(top, bottom, count):
    """The scarf's grey and near-black stripes across a band whose edges are `top` and `bottom`."""
    out, n = [], len(top) - 1
    for k in range(count):
        i0, i1 = round(k * n / count), round((k + 1) * n / count)
        edge = top[i0:i1 + 1] + bottom[i0:i1 + 1][::-1]
        out.append(("path", dict(d="M" + " L".join(f"{x:.2f},{y:.2f}" for x, y in edge) + " Z",
                                 fill=GREY if k % 2 == 0 else BLACK)))
    return out


def boss(away: bool = False):
    """You, the boss the crew works for (picked by the user, 2026-11-15, from several drafts): a bald head
    with a scowl, a long nose and a smirk, and a headset with a goggle-cyan mic, since you're the one they
    call; a big barrel of a black coat and a thick grey-and-black striped scarf wrapped high, its ends down."""
    face = [
        ("path", dict(d="M54,20 C71,20 77,35 77,49 C77,64 67,75 54,75 C41,75 31,64 31,49 C31,35 37,20 54,20 Z", fill="#DCC7AC")),
        ("path", dict(d="M39,34 A18,18 0,0 1,50 24.5", stroke="#F3E6D3", sw=2.4)),
        ("path", dict(d="M39,43 L49.5,46 M69,43 L58.5,46", stroke="#2B2F3A", sw=3.4)),
        ("circle", dict(cx=45, cy=50.5, r=3.4, fill="#FFFFFF")), ("circle", dict(cx=45.8, cy=51, r=1.7, fill=PUPIL)),
        ("circle", dict(cx=63, cy=50.5, r=3.4, fill="#FFFFFF")), ("circle", dict(cx=62.2, cy=51, r=1.7, fill=PUPIL)),
        ("path", dict(d="M51.5,49 Q49,60 43,67 Q52,68 56.5,62.5 Q57.5,55 55,49 Z", fill="#C8AE8E")),
        ("path", dict(d="M47,70.5 Q55,72.5 62,67.5", stroke="#2B2F3A", sw=2)),
    ]
    # The headset, made to read at chat size (the user's pick, 2026-11-17, "the guy in the chair"): a gunmetal band
    # outlined in the background's navy over the head, both ear cups and a mic capsule rimmed in cyan so they stand
    # out from the dark, a cyan light on the right cup and at the mic
    headset = [
        ("path", dict(d="M28.5,50 A25.5,28 0,0 1,79.5 50", stroke=NAVY, sw=6.2)),
        ("path", dict(d="M28.5,50 A25.5,28 0,0 1,79.5 50", stroke=GUNMETAL, sw=4.2)),
    ]
    for x in (23.5, 75.5):
        headset += [("rect", dict(x=x, y=40, w=9, h=17, fill=HEADSET_RIM, rx=4.5)),
                    ("rect", dict(x=x + 1.6, y=41.6, w=5.8, h=13.8, fill=CUP, rx=3))]
    headset += [
        ("circle", dict(cx=80, cy=48.5, r=1.4, fill="#00E5FF")),
        ("path", dict(d="M79,57 Q78,70 64,72.5", stroke=HEADSET_RIM, sw=3.4)),
        ("path", dict(d="M79,57 Q78,70 64,72.5", stroke=GUNMETAL, sw=1.8)),
        ("rect", dict(x=55.5, y=69.3, w=10, h=6.4, fill=HEADSET_RIM, rx=3.2)),
        ("rect", dict(x=56.6, y=70.4, w=7.8, h=4.2, fill=CUP, rx=2.1)),
        ("circle", dict(cx=57.9, cy=72.5, r=1.2, fill="#00E5FF")),
    ]
    if not away:
        face += headset
    if away:  # on vacation (the user's pick, 2026-11-17): no headset, sunglasses, a relaxed smile
        mouth = next(i for i, (k, a) in enumerate(face) if k == "path" and a.get("d", "").startswith("M47,70.5"))
        face[mouth] = ("path", dict(d="M45,68.5 Q54,75 63,68.5", stroke="#2B2F3A", sw=2.2))
        face += [("rect", dict(x=37, y=45, w=15, h=10, fill="#111318", rx=4)),
                 ("rect", dict(x=56, y=45, w=15, h=10, fill="#111318", rx=4)),
                 ("path", dict(d="M52,48 L56,48", stroke="#111318", sw=2)),
                 ("path", dict(d="M40,47.5 L45,47.5", stroke="#5B6B8C", sw=1.4)),
                 ("path", dict(d="M59,47.5 L64,47.5", stroke="#5B6B8C", sw=1.4))]
    out = [
        ("clip", dict(d=circle_path(54, 54, 54))),  # the coat and scarf go past the round edge: cut there
        ("circle", dict(cx=54, cy=54, r=54, fill=NAVY)),
        ("path", dict(d="M2,108 C1,90 12,77 30,73 C44,70.5 64,70.5 78,73 C96,77 107,90 106,108 Z", fill=COAT)),
        ("path", dict(d="M54,84 V108", stroke=COAT_LINE, sw=1.2)),
        ("path", dict(d="M30,73 C40,80 46,92 47,108", stroke=COAT_DARK, sw=1.4)),
        ("path", dict(d="M78,73 C68,80 62,92 61,108", stroke=COAT_DARK, sw=1.4)),
    ]
    out += stripes(bezier((34, 72), (35, 84), (33, 96), (31, 110)), bezier((45, 74), (46, 86), (45, 98), (44, 110)), 7)
    out += stripes(bezier((62, 75), (63, 86), (64, 98), (65, 110)), bezier((72, 73), (73, 84), (75, 96), (77, 110)), 7)
    out.append(("group", dict(dy=-10, items=face)))  # the face sits up in the scarf
    out += stripes(bezier((25, 62), (38, 70.5), (70, 70.5), (83, 61)), bezier((26, 78), (38, 87), (70, 87), (82, 77)), 11)
    out.append(("path", dict(d="M27,70.5 C39,78.5 69,78.5 82.5,69.5", stroke="#000000", sw=1.2, alpha=0.2)))
    out += [("ellipse", dict(cx=25.6, cy=70, rx=3.6, ry=8, fill=GREY)), ("ellipse", dict(cx=82.4, cy=69, rx=3.6, ry=8, fill=GREY))]
    if away:  # the coat and scarf swapped for a teal holiday shirt with red flowers and an open collar
        out = [(k, dict(a, fill=SHIRT) if k == "path" and a.get("d", "").startswith("M2,108") else a) for k, a in out
               if not (k == "ellipse" or (k == "path" and (a.get("fill") in (GREY, BLACK) or a.get("alpha") == 0.2
                                                           or a.get("stroke") in (COAT_DARK, COAT_LINE))))]
        out += [("path", dict(d="M43,71 L54,86 L65,71 Z", fill="#DCC7AC")),
                ("path", dict(d="M43,71 L54,86 M65,71 L54,86", stroke="#F3F0E8", sw=2.4))]
        for x, y in ((22, 80), (33, 88), (76, 88), (87, 79), (64, 92), (44, 92)):
            out += [("circle", dict(cx=x, cy=y, r=3.6, fill="#FF6B6B")), ("circle", dict(cx=x, cy=y, r=1.4, fill="#FFD166"))]
    # Zoomed in on the face (the user's wish, 2026-11-15): the shoulders and scarf still in, cut by the circle
    return out[:2] + [("group", dict(scale=1.3, px=54, py=40, items=out[2:]))]


def circle_path(cx, cy, r):
    return f"M{cx - r},{cy}a{r},{r} 0,1 0,{2 * r} 0a{r},{r} 0,1 0,{-2 * r} 0"


def rect_path(x, y, w, h, rx=0):
    if not rx:
        return f"M{x},{y}h{w}v{h}h{-w}z"
    return (f"M{x + rx},{y}h{w - 2 * rx}a{rx},{rx} 0,0 1,{rx},{rx}v{h - 2 * rx}a{rx},{rx} 0,0 1,{-rx},{rx}"
            f"h{-(w - 2 * rx)}a{rx},{rx} 0,0 1,{-rx},{-rx}v{-(h - 2 * rx)}a{rx},{rx} 0,0 1,{rx},{-rx}z")


def ellipse_path(cx, cy, rx, ry):
    return f"M{cx - rx},{cy}a{rx},{ry} 0,1 0,{2 * rx} 0a{rx},{ry} 0,1 0,{-2 * rx} 0"


def svg(items, top=True):
    body = []
    for kind, a in items:
        if kind == "clip":
            body.append(f'<clipPath id="round"><path d="{a["d"]}"/></clipPath>')
        elif kind == "group":
            if "scale" in a:
                z, px, py = a["scale"], a["px"], a["py"]
                body.append(f'<g transform="translate({px},{py}) scale({z}) translate({-px},{-py})">' + svg(a["items"], False) + "</g>")
            else:
                body.append(f'<g transform="translate(0,{a["dy"]})">' + svg(a["items"], False) + "</g>")
        elif kind == "ellipse":
            body.append(f'<ellipse cx="{a["cx"]}" cy="{a["cy"]}" rx="{a["rx"]}" ry="{a["ry"]}" fill="{a["fill"]}"/>')
        elif kind == "circle":
            body.append(f'<circle cx="{a["cx"]}" cy="{a["cy"]}" r="{a["r"]}" fill="{a["fill"]}"/>')
        elif kind == "rect":
            body.append(f'<rect x="{a["x"]}" y="{a["y"]}" width="{a["w"]}" height="{a["h"]}" rx="{a.get("rx", 0)}" fill="{a["fill"]}"/>')
        elif "stroke" in a:
            body.append(f'<path d="{a["d"]}" stroke="{a["stroke"]}" stroke-width="{a["sw"]}" fill="none" '
                        f'stroke-opacity="{a.get("alpha", 1)}" stroke-linecap="round"/>')
        else:
            body.append(f'<path d="{a["d"]}" fill="{a["fill"]}"/>')
    if not top:
        return "".join(body)
    clipped = items and items[0][0] == "clip"
    inner = body[1:] if clipped else body
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 108 108" width="216" height="216">'
            + (body[0] + '<g clip-path="url(#round)">' + "".join(inner) + "</g>" if clipped else "".join(inner)) + "</svg>")


def vector_paths(items):
    paths = []
    for kind, a in items:
        if kind == "clip":
            paths.append(f'<clip-path android:pathData="{a["d"]}" />')
        elif kind == "group":
            how = (f'android:pivotX="{a["px"]}" android:pivotY="{a["py"]}" android:scaleX="{a["scale"]}" '
                   f'android:scaleY="{a["scale"]}"' if "scale" in a else f'android:translateY="{a["dy"]}"')
            paths.append(f'<group {how}>\n        ' + "\n        ".join(vector_paths(a["items"])) + "\n    </group>")
        elif kind == "ellipse":
            paths.append(f'<path android:fillColor="{a["fill"]}" android:pathData="{ellipse_path(a["cx"], a["cy"], a["rx"], a["ry"])}" />')
        elif kind == "circle":
            paths.append(f'<path android:fillColor="{a["fill"]}" android:pathData="{circle_path(a["cx"], a["cy"], a["r"])}" />')
        elif kind == "rect":
            paths.append(f'<path android:fillColor="{a["fill"]}" android:pathData="{rect_path(a["x"], a["y"], a["w"], a["h"], a.get("rx", 0))}" />')
        elif "stroke" in a:
            paths.append(f'<path android:strokeColor="{a["stroke"]}" android:strokeWidth="{a["sw"]}" '
                         + (f'android:strokeAlpha="{a["alpha"]}" ' if "alpha" in a else "")
                         + f'android:strokeLineCap="round" android:pathData="{a["d"]}" />')
        else:
            paths.append(f'<path android:fillColor="{a["fill"]}" android:pathData="{a["d"]}" />')
    return paths


def vector(items, name):
    paths = vector_paths(items)
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            f'<!-- {name}: generated by call/android/avatars.py, edit that instead -->\n'
            '<vector xmlns:android="http://schemas.android.com/apk/res/android"\n'
            '    android:width="108dp" android:height="108dp" android:viewportWidth="108" android:viewportHeight="108">\n    '
            + "\n    ".join(paths) + "\n</vector>\n")


# The call menu's henchmen, alive in their round windows (the user's wish, 2026-11-16: the whole picture moved with
# its navy disc, as if the window moved): no backdrop, the pupils and an eyelid as their own groups, and each its
# own way of looking about, blinking and moving, as the README's peeks (docs/peeks.py) do.
GLASSES = {"one": [(54, 47.5, 6.8)], "two": [(45.5, 47.5, 4.8), (62.5, 47.5, 4.8)]}  # cx, cy, glass radius
MOODS = {  # how each moves: (look every s, look reach, sway, hop) — curious, sneaky, scanning, excitable
    "claude": (3.2, 2.4, 9.0, 2.0), "cursor": (1.4, 2.6, 4.0, 1.0), "codex": (2.2, 6.0, 5.0, 3.0),
    "handsfree": (2.6, 2.0, 6.0, 7.0),
}


def live_vector(name, body, light, glow, eyes):
    items = shapes(body, light, glow, eyes)[1:]  # no navy disc: the window is the backdrop
    still = [(k, a) for k, a in items if a.get("fill") not in (PUPIL, "#FFFFFF")]
    pupils = [(k, a) for k, a in items if a.get("fill") in (PUPIL, "#FFFFFF")]
    if eyes == "visor":
        lid, top = [("rect", dict(x=40.5, y=44.5, w=27, h=6, fill=body, rx=3))], 44.5
    else:
        lid = [("circle", dict(cx=cx, cy=cy, r=r, fill=body)) for cx, cy, r in GLASSES[eyes]]
        top = min(cy - r for cx, cy, r in GLASSES[eyes])
    inner = ("\n        ".join(vector_paths(still))
             + '\n        <group android:name="pupils">\n            ' + "\n            ".join(vector_paths(pupils))
             + f'\n        </group>\n        <group android:name="lid" android:pivotY="{top}" android:scaleY="0">\n            '
             + "\n            ".join(vector_paths(lid)) + "\n        </group>")
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            f'<!-- {name.capitalize()}\'s henchman, alive (no backdrop; pupils and lid as groups): generated by '
            'call/android/avatars.py, edit that instead -->\n'
            '<vector xmlns:android="http://schemas.android.com/apk/res/android"\n'
            '    android:width="108dp" android:height="108dp" android:viewportWidth="108" android:viewportHeight="108">\n'
            '    <group android:name="body" android:pivotX="54" android:pivotY="86">\n        ' + inner
            + "\n    </group>\n</vector>\n")


def keyframes(prop, frames, dur, value_type="floatType"):
    ks = "".join(f'<keyframe android:fraction="{f}" android:value="{v}"/>' for f, v in frames)
    return (f'<objectAnimator android:duration="{int(dur * 1000)}" android:repeatCount="infinite">'
            f'<propertyValuesHolder android:propertyName="{prop}" android:valueType="{value_type}">{ks}'
            '</propertyValuesHolder></objectAnimator>')


def alive(name):
    every, reach, sway, hop = MOODS[name]
    look = keyframes("translateX", [(0, 0), (0.2, 0), (0.3, -reach), (0.5, -reach), (0.6, reach), (0.85, reach), (1, 0)],
                     every)
    blink = keyframes("scaleY", [(0, 0), (0.92, 0), (0.96, 1), (1, 0)], 4.3)
    move = keyframes("rotation", [(0, 0), (0.25, -sway), (0.5, 0), (0.75, sway * 0.6), (1, 0)], every * 2.1)
    bob = keyframes("translateY", [(0, 0), (0.08, -hop), (0.16, 0), (0.24, -hop * 0.5), (0.32, 0), (1, 0)], every * 1.7)

    def target(group, *animators):
        return (f'<target android:name="{group}"><aapt:attr name="android:animation"><set>{"".join(animators)}</set>'
                '</aapt:attr></target>')
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            f'<!-- {name.capitalize()}\'s henchman looking about, blinking and moving: generated by '
            'call/android/avatars.py, edit that instead -->\n'
            '<animated-vector xmlns:android="http://schemas.android.com/apk/res/android"\n'
            '    xmlns:aapt="http://schemas.android.com/aapt"\n'
            f'    android:drawable="@drawable/avatar_{name}_live">\n'
            + target("pupils", look) + "\n" + target("lid", blink) + "\n" + target("body", move, bob) + "\n"
            + "</animated-vector>\n")


if __name__ == "__main__":
    res = Path(__file__).resolve().parent / "app/src/main/res/drawable"
    previews = Path(sys.argv[2]) if sys.argv[1:2] == ["--svg"] else None
    for name, (body, light, glow, eyes) in FLAVOURS.items():
        items = shapes(body, light, glow, eyes)
        (res / f"avatar_{name}.xml").write_text(vector(items, f"{name.capitalize()}'s henchman"))
        (res / f"avatar_{name}_live.xml").write_text(live_vector(name, body, light, glow, eyes))
        (res / f"avatar_{name}_alive.xml").write_text(alive(name))
        for role in HATS:
            (res / f"avatar_{name}_{role}.xml").write_text(
                vector(hatted(body, light, glow, eyes, role), f"{name.capitalize()}'s henchman, {role} hard hat"))
        if previews:
            (previews / f"avatar_{name}.svg").write_text(svg(items))
            for role in HATS:
                (previews / f"avatar_{name}_{role}.svg").write_text(svg(hatted(body, light, glow, eyes, role)))
    (res / "avatar_you.xml").write_text(vector(boss(), "The boss (you)"))
    (res / "avatar_you_away.xml").write_text(vector(boss(away=True), "The boss, away: headset off"))
    if previews:
        (previews / "avatar_you.svg").write_text(svg(boss()))
        (previews / "avatar_you_away.svg").write_text(svg(boss(away=True)))
