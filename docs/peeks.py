"""The henchmen peeking at you from the README, each in its own way (the user's wish, 2026-11-15: personalities):

    claude     curious: leans in round the right-hand edge, head tilting, and waves
    cursor     sneaky: slides in from the left, one goggle showing, eyes darting, and ducks away again
    codex      daredevil: drops down and hangs upside down from the top, swinging, visor scanning
    handsfree  excitable: pops up from below and can't stop hopping

Animated SVG (SMIL), which GitHub shows in a README image; without animation each still shows its peek.

    python3 docs/peeks.py        (writes docs/peek_<agent>.svg; the henchmen are call/android/avatars.py's)
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "call/android"))
from avatars import FLAVOURS, NAVY, PUPIL, shapes, svg  # noqa: E402

EYES = {"one": [(54, 47.5, 6.8, 3.2)], "two": [(45.5, 47.5, 4.8, 2.2), (62.5, 47.5, 4.8, 2.2)]}  # cx, cy, glass, pupil
EDGE = "#8B93A1"  # the edge of the page they hide behind
FONT = 'font-family="Comic Sans MS, Chalkboard, sans-serif" font-size="9"'


def anim(kind: str, values: str, dur: float, begin: float = 0, times: str = "", freeze: bool = False) -> str:
    """One SMIL transform animation; repeating unless frozen."""
    keys = f' keyTimes="{times}"' if times else ""
    end = 'fill="freeze"' if freeze else 'repeatCount="indefinite"'
    return (f'<animateTransform attributeName="transform" type="{kind}" values="{values}"{keys} dur="{dur}s" '
            f'begin="{begin}s" {end}/>')


def henchman(name: str, body: str, light: str, glow: str, eyes: str, look_every: float) -> str:
    """The henchman drawn in place (avatars.py), its pupils looking about and a blink now and then."""
    drawn = [(kind, a) for kind, a in shapes(body, light, glow, eyes) if a.get("fill") not in (NAVY, PUPIL, "#FFFFFF")]
    look = anim("translate", "0 0;0 0;-2.4 0.4;-2.4 0.4;2.4 0.4;2.4 0.4;0 0", look_every,
                times="0;0.2;0.3;0.5;0.6;0.85;1")
    parts = []
    for cx, cy, glass, r in EYES.get(eyes, []):
        parts.append(f'<g>{look}<circle cx="{cx}" cy="{cy}" r="{r}" fill="{PUPIL}"/>'
                     f'<circle cx="{cx - r * 0.7}" cy="{cy - r * 0.7}" r="{r * 0.4}" fill="#FFFFFF"/></g>'
                     f'<clipPath id="glass-{name}-{cx}"><circle cx="{cx}" cy="{cy}" r="{glass}"/></clipPath>'
                     f'<rect clip-path="url(#glass-{name}-{cx})" x="{cx - glass}" y="{cy - glass}" width="{2 * glass}" '
                     f'height="0" fill="{body}"><animate attributeName="height" values="0;0;{2 * glass};0" '
                     f'keyTimes="0;0.92;0.96;1" dur="4.3s" repeatCount="indefinite"/></rect>')
    if eyes == "visor":
        parts.append(f'<g>{look}<rect x="50" y="45.5" width="8" height="4" rx="2" fill="{PUPIL}"/></g>')
    return svg(drawn, top=False) + "".join(parts)


def giggle(text: str, x: float, y: float, colour: str, every: float, at: str) -> str:
    return (f'<text x="{x}" y="{y}" {FONT} fill="{colour}" opacity="0">{text}<animate attributeName="opacity" '
            f'values="0;0;1;1;0" keyTimes="0;{at};1" dur="{every}s" begin="1s" repeatCount="indefinite"/></text>')


def page(view: str, size: tuple, inner: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view}" width="{size[0]}" height="{size[1]}">'
            f'{inner}</svg>')


def claude(body, light, glow, eyes):
    """Curious: round the right-hand edge, leaning in, head tilting, a wave."""
    me = henchman("claude", body, light, glow, eyes, 5)
    return page("0 0 100 90", (180, 162),
                '<clipPath id="side-claude"><rect x="0" y="0" width="87" height="90"/></clipPath>'
                '<g clip-path="url(#side-claude)"><g>' + anim("translate", "34 0;-2 0;0 0", 0.9, 0.3, "0;0.7;1", True)
                + '<g transform="translate(26 -2)"><g>' + anim("rotate", "-12 54 70;-24 54 70;-12 54 70", 4.5)
                + me + '</g></g></g></g>'
                f'<rect x="87" y="0" width="3" height="90" rx="1.5" fill="{EDGE}"/>'
                f'<g><ellipse cx="88.5" cy="58" rx="2.6" ry="4.2" fill="{body}"/>'
                + anim("rotate", "0 88.5 62;0 88.5 62;-28 88.5 62;0 88.5 62;-28 88.5 62;0 88.5 62", 5, 1.2,
                       "0;0.6;0.68;0.76;0.84;0.92") + '</g>'
                + giggle("hee hee!", 4, 20, light, 5, "0.55;0.6;0.9;0.95"))


def cursor(body, light, glow, eyes):
    """Sneaky: in from the left, one goggle round the edge, eyes darting, then ducking away again."""
    me = henchman("cursor", body, light, glow, eyes, 1.6)
    return page("0 0 100 90", (180, 162),
                '<clipPath id="side-cursor"><rect x="11" y="0" width="89" height="90"/></clipPath>'
                '<g clip-path="url(#side-cursor)"><g>'
                + anim("translate", "-34 0;0 0;0 0;-34 0;-34 0", 8, 0.6, "0;0.08;0.62;0.7;1")
                + '<g transform="translate(-36 4)"><g>' + anim("rotate", "8 54 70;12 54 70;8 54 70", 2.2)
                + me + '</g></g></g></g>'
                f'<rect x="8" y="0" width="3" height="90" rx="1.5" fill="{EDGE}"/>'
                + giggle("pssst…", 40, 30, light, 8, "0.15;0.2;0.5;0.58"))


def codex(body, light, glow, eyes):
    """Daredevil: drops from the top and hangs upside down, swinging, visor scanning."""
    me = henchman("codex", body, light, glow, eyes, 2.2)
    return page("0 0 100 100", (180, 180),
                '<clipPath id="top-codex"><rect x="0" y="9" width="100" height="91"/></clipPath>'
                '<g clip-path="url(#top-codex)"><g>' + anim("translate", "0 -80;0 4;0 0", 1.1, 0.9, "0;0.75;1", True)
                + '<g>' + anim("rotate", "-9 54 8;9 54 8;-9 54 8", 3.2)
                + '<g transform="translate(0 -19) rotate(180 54 55)">' + me + '</g></g></g></g>'
                f'<rect x="0" y="6" width="100" height="3" rx="1.5" fill="{EDGE}"/>'
                + "".join(f'<ellipse cx="{x}" cy="7.5" rx="3.6" ry="2.4" fill="{body}"/>' for x in (46, 62))
                + giggle("wheee!", 70, 70, light, 6, "0.4;0.45;0.75;0.8"))


def handsfree(body, light, glow, eyes):
    """Excitable: pops up from below and can't stop hopping."""
    me = henchman("handsfree", body, light, glow, eyes, 3)
    return page("10 0 100 72", (200, 144),
                '<clipPath id="below-handsfree"><rect x="0" y="0" width="120" height="62"/></clipPath>'
                '<g clip-path="url(#below-handsfree)"><g>' + anim("translate", "0 40;0 -4;0 0", 0.8, 0.2, "0;0.7;1", True)
                + '<g>' + anim("translate", "0 0;0 -9;0 0;0 -6;0 0;0 0", 2.6, 1, "0;0.12;0.24;0.34;0.44;1")
                + me + '</g></g></g>'
                f'<rect x="10" y="62" width="100" height="3" rx="1.5" fill="{EDGE}"/>'
                + "".join(f'<ellipse cx="{x}" cy="62.6" rx="4" ry="2.4" fill="{body}"/>' for x in (38, 70))
                + giggle("boss-a!", 82, 30, light, 2.6, "0.05;0.1;0.4;0.45"))


def hero() -> str:
    """The README's banner: a navy card with the name and the line, the crew hiding round it. Cursor sneaks round
    the left side, Claude leans round the right, hands-free pops up over the top, Codex hangs from the bottom."""
    def me(name):
        body, light, glow, eyes = FLAVOURS[name]
        return henchman("hero-" + name, body, light, glow, eyes, {"cursor": 1.6, "codex": 2.2}.get(name, 4))
    card = '<rect x="140" y="70" width="520" height="190" rx="30" fill="#0A1430"/>'
    words = ('<text x="400" y="160" text-anchor="middle" font-family="Trebuchet MS, Segoe UI, Helvetica, sans-serif" '
             'font-size="62" font-weight="bold" fill="#FFFFFF" letter-spacing="1">Henchline</text>'
             '<text x="400" y="205" text-anchor="middle" font-family="Trebuchet MS, Segoe UI, Helvetica, sans-serif" '
             'font-size="21" fill="#00E5FF">Talk to all your AI henchmen.</text>'
             '<text x="400" y="234" text-anchor="middle" font-family="Trebuchet MS, Segoe UI, Helvetica, sans-serif" '
             'font-size="17" fill="#9FB3D9">By voice, by phone, by text.</text>')
    behind = (
        # Cursor: round the left side, ducking in and out
        '<g>' + anim("translate", "40 0;0 0;0 0;40 0;40 0", 8, 0.6, "0;0.08;0.62;0.7;1")
        + '<g transform="translate(118 112) scale(1.5) rotate(-10 54 55) translate(-54 -40)">' + me("cursor") + '</g></g>'
        # Claude: leans out round the right side, then hides behind the card again, twice in quick looks
        + '<g>' + anim("translate", "-60 0;0 0;0 0;-60 0;-60 0;0 0;0 0;-60 0;-60 0", 9, 1.4,
                       "0;0.06;0.3;0.36;0.5;0.56;0.7;0.76;1")
        + '<g transform="translate(682 112) scale(1.5) rotate(10 0 30) translate(-54 -40)">' + me("claude") + '</g></g>'
        # hands-free: over the top edge, hopping
        + '<g>' + anim("translate", "0 60;0 -4;0 0", 0.8, 0.2, "0;0.7;1", True)
        + '<g>' + anim("translate", "0 0;0 -10;0 0;0 -6;0 0;0 0", 2.6, 1, "0;0.12;0.24;0.34;0.44;1")
        + '<g transform="translate(250 14) scale(1.3) translate(-54 -17)">' + me("handsfree") + '</g></g></g>')
    hanging = ('<g>' + anim("translate", "0 -90;0 4;0 0", 1.1, 0.9, "0;0.75;1", True)
               + '<g>' + anim("rotate", "-9 560 258;9 560 258;-9 560 258", 3.2)
               + '<g transform="translate(560 258) scale(1.2) rotate(180) translate(-54 -86)">' + me("codex") + '</g>'
               + '</g></g>'
               + ''.join(f'<ellipse cx="{x}" cy="259" rx="4.5" ry="3" fill="{FLAVOURS["codex"][0]}"/>' for x in (550, 570)))
    hands = (f'<ellipse cx="140" cy="150" rx="3.2" ry="5" fill="{FLAVOURS["cursor"][0]}"/>'
             f'<ellipse cx="660" cy="150" rx="3.2" ry="5" fill="{FLAVOURS["claude"][0]}"/>'
             + ''.join(f'<ellipse cx="{x}" cy="70" rx="5" ry="3.2" fill="{FLAVOURS["handsfree"][0]}"/>' for x in (237, 263)))
    return page("0 0 800 360", (800, 360), behind + card + words + hanging + hands)


def main() -> None:
    drawings = {}
    for name, (body, light, glow, eyes) in FLAVOURS.items():
        drawings[name] = globals()[name](body, light, glow, eyes)
        (HERE / f"peek_{name}.svg").write_text(drawings[name])
    (HERE / "hero.svg").write_text(hero())


if __name__ == "__main__":
    main()
