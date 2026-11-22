"""The README's feature panels: each feature as one finished card (the user's wish, 2026-11-15: no loose pictures
and empty space). A navy card, the screenshots arranged on it, the henchman, a heading and a few lines.

    python3 docs/panels.py AVATARS_DIR     (needs Pillow and the Inter font; AVATARS_DIR holds <agent>.png, <agent>_manager.png,
                                            <agent>_crew.png, you.png and you_away.png: avatars.py --svg,
                                            avatar_ prefix dropped, 420 px wide; writes docs/panels/*.png)

The screenshots are docs/screens/*.png (docs/screens/polish.sh). Panels are drawn at twice the size they show at.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
SCREENS, OUT = HERE / "screens", HERE / "panels"
FONTS = Path("/usr/share/fonts/opentype/inter")
NAVY, SURFACE, CYAN, TEXT, MUTED = (10, 20, 48), (20, 32, 74), (0, 229, 255), (240, 244, 252), (180, 196, 226)
WIDTH, PAD, RADIUS = 1800, 80, 56


def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / f"Inter-{weight}.otf"), size)


def wrap(draw: ImageDraw.ImageDraw, text: str, face, width: int) -> list[str]:
    lines = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split():
            trial = f"{line} {word}".strip()
            if draw.textlength(trial, font=face) > width and line:
                lines.append(line)
                line = word
            else:
                line = trial
        lines.append(line)
    return lines


def card(height: int) -> Image.Image:
    """The navy card, a little lighter towards the bottom."""
    image = Image.new("RGBA", (WIDTH, height), (0, 0, 0, 0))
    fill = Image.new("RGBA", (WIDTH, height))
    shade = ImageDraw.Draw(fill)
    for y in range(height):
        t = y / height
        shade.line([(0, y), (WIDTH, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(NAVY, SURFACE)) + (255,))
    mask = Image.new("L", (WIDTH, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, WIDTH - 1, height - 1], RADIUS, fill=255)
    image.paste(fill, (0, 0), mask)
    return image


def picture(name: str, height: int) -> Image.Image:
    image = Image.open(SCREENS / f"{name}.png").convert("RGBA")
    return image.resize((round(image.width * height / image.height), height), Image.LANCZOS)


SHOT = 0.9  # every screenshot at the same scale, so the text in them is the same size in every card
CODE = (6, 14, 36)


def flow(image, draw, x: int, width: int, items: list, height: int, measure: bool = False, top: int = 0,
         spreadable: bool = True) -> int:
    """A column of items, centred top to bottom in the card: ("avatar", path), ("head", text), ("body", text),
    ("code", what the agent runs), ("shot", name). Returns its height (and draws it unless measuring)."""
    head, small = font("Bold", 68), font("Regular", 38)
    mono = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 30)
    total, steps = 0, []
    for kind, what in items:
        if kind == "avatar":
            steps.append((kind, what, 130)); total += 130 + 30
        elif kind == "face":  # a big avatar in its ring, as the boss
            steps.append((kind, what, 432)); total += 432 + 40
        elif kind == "head":
            lines = wrap(draw, what, head, width); steps.append((kind, lines, len(lines) * 84)); total += len(lines) * 84 + 24
        elif kind == "body":
            lines = wrap(draw, what, small, width); steps.append((kind, lines, len(lines) * 56)); total += len(lines) * 56 + 40
        elif kind == "code":  # as a terminal shows it, the same size in every card; a long one goes on two lines
            lines = what.split("\n"); h = 44 + 46 * len(lines)
            steps.append((kind, lines, h)); total += h + 40
        elif kind == "row":  # a card screenshot with words and the command beside it: no column left half empty
            name, beside = what
            shot = Image.open(SCREENS / f"{name}.png").convert("RGBA")
            shot = shot.resize((round(shot.width * SHOT), round(shot.height * SHOT)), Image.LANCZOS)
            inner = flow(None, draw, 0, width - shot.width - 24, beside, 0, measure=True)
            h = max(shot.height, inner)
            steps.append((kind, (shot, beside), h)); total += h + 40
        else:
            shot = Image.open(SCREENS / f"{what}.png").convert("RGBA")
            shot = shot.resize((round(shot.width * SHOT), round(shot.height * SHOT)), Image.LANCZOS)
            steps.append((kind, shot, shot.height)); total += shot.height + 40
    total -= 40 if steps and steps[-1][0] != "head" else 24
    if measure:
        return total
    spare = height - 2 * PAD - total
    gaps = sum(1 for kind, *_ in steps[:-1] if kind not in ("avatar", "head"))  # a heading stays with its words
    spread = spare // gaps if spreadable and gaps and len(steps) > 1 and 0 < spare < 480 else 0  # spread small gaps
    y = top + (height - total - spread * gaps) // 2
    for kind, what, h in steps:
        if kind == "avatar":
            image.alpha_composite(Image.open(what).convert("RGBA").resize((130, 130), Image.LANCZOS), (x, y)); y += 160
        elif kind == "face":
            ring(image, draw, what, x + (width - 420) // 2, y + 6); y += h + 40 + spread
        elif kind == "head":
            for line in what:
                draw.text((x, y), line, font=head, fill=TEXT); y += 84
            y += 24
        elif kind == "body":
            for line in what:
                draw.text((x, y), line, font=small, fill=MUTED); y += 56
            y += 40 + spread
        elif kind == "code":  # what the agent runs, as a terminal line, its box as wide as the command
            box = min(width, max(int(draw.textlength(line, font=mono)) for line in what) + 56)
            draw.rounded_rectangle([x, y, x + box, y + h], 18, fill=CODE)
            ty = y + 22
            for line in what:
                draw.text((x + 28, ty), line, font=mono, fill=CYAN); ty += 46  # each line 46, 22 above and below
            y += h + 40 + spread
        elif kind == "row":
            shot, beside = what
            image.alpha_composite(shot, (x - 12, y + (h - shot.height) // 2))
            flow(image, draw, x + shot.width + 12, width - shot.width - 24, beside, h, top=y, spreadable=False)
            y += h + 40 + spread
        else:
            image.alpha_composite(what, (x - 12, y)); y += h + 40 + spread  # its shadow's margin, so its edge lines up with the text
    return total


def panel(out: str, columns: list[tuple[int, list]]):
    """A card of columns side by side: (width, items) each; a bare screenshot column is a phone, full height."""
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    height = max(flow(None, probe, 0, w, items, 0, measure=True) for w, items in columns) + 2 * PAD
    image = card(height)
    draw = ImageDraw.Draw(image)
    gap = (WIDTH - 2 * PAD - sum(w for w, _ in columns)) // max(1, len(columns) - 1)
    x = PAD
    for w, items in columns:
        flow(image, draw, x, w, items, height)
        x += w + gap
    OUT.mkdir(exist_ok=True)
    image.save(OUT / out, optimize=True)


def ring(image, draw, path: Path, ax: int, ay: int, size: int = 420):
    """A big avatar on a lighter disc (it lifts the dark circle off the card), with a cyan ring round it."""
    face = Image.open(path).convert("RGBA").resize((size, size), Image.LANCZOS)
    draw.ellipse([ax - 6, ay - 6, ax + size + 6, ay + size + 6], fill=(28, 44, 96))
    image.alpha_composite(face, (ax, ay))
    draw.ellipse([ax - 6, ay - 6, ax + size + 6, ay + size + 6], outline=(0, 229, 255, 160), width=4)


def crew_tree(image, draw, avatars: Path, x: int, y: int):
    """A small org chart: Claude in a white hard hat running Codex and Cursor in yellow ones, and a Cursor
    working solo beside them, hatless."""
    size, line = 120, (0, 229, 255)
    face = lambda name, fx, fy: image.alpha_composite(
        Image.open(avatars / f"{name}.png").convert("RGBA").resize((size, size), Image.LANCZOS), (fx, fy))
    left, right, top, low = x, x + 200, y, y + size + 60
    mid = (left + right) // 2
    face("claude_manager", mid, top)
    cx, fork = mid + size // 2, top + size + 30
    draw.line([(cx, top + size), (cx, fork)], fill=line, width=3)
    draw.line([(left + size // 2, fork), (right + size // 2, fork)], fill=line, width=3)
    for fx, name in ((left, "codex_crew"), (right, "cursor_crew")):
        draw.line([(fx + size // 2, fork), (fx + size // 2, low)], fill=line, width=3)
        face(name, fx, low)
    solo = right + 220
    face("cursor", solo, low)
    small = font("Medium", 32)
    draw.text((solo + size // 2, low + size + 12), "solo", font=small, fill=MUTED, anchor="mt")


def boss(avatars: Path):
    """The opening: you, the boss, and your crew, which now runs crews of its own."""
    height = 820
    image = card(height)
    draw = ImageDraw.Draw(image)
    ring(image, draw, avatars / "you.png", PAD + 20, (height - 420) // 2)
    x = PAD + 560
    draw.text((x, 110), "You're the boss.", font=font("ExtraBold", 92), fill=TEXT)
    lines = wrap(draw, "Your crew is Claude Code, Cursor and Codex. And now they run crews of their own.",
                 font("Regular", 40), WIDTH - x - PAD)
    y = 250
    for line in lines:
        draw.text((x, y), line, font=font("Regular", 40), fill=MUTED)
        y += 60
    crew_tree(image, draw, avatars, x, y + 40)
    OUT.mkdir(exist_ok=True)
    image.save(OUT / "boss.png", optimize=True)


def main() -> None:
    avatars = Path(sys.argv[1])
    boss(avatars)
    phone = round(648 * SHOT) - 24  # a phone screenshot's column: its shadow margins left out
    text = 1640 - phone - 90  # the words beside one phone
    panel("minions.png", [
        (text, [("head", "They hire their own minions"),
                ("body", "White hat: running a crew. Yellow: on one. Tap a face to End or Keep each helper."),
                ("row", ("crewsheet", [("body", "Only its boss, the helper, or you can end one.")])),
                ("code", 'handsfree deploy --agent cursor \\\n    --ask "Write the parser tests"'),
                ("code", 'handsfree report "12 tests, all green"')]),
        (phone, [("shot", "chats")])])
    panel("ring.png", [
        (phone, [("shot", "ring")]),
        (text, [("head", "They ring you"),
                ("body", "When an agent needs a decision, it rings your phone: full screen, with its henchman, its own "
                         "ringtone, a line of henchman-speak, and why it's calling. Answer, and you're talking to the "
                         "one who rang."),
                ("row", ("banner", [("body", "Using your phone? It rings as a banner instead.")])),
                ("code", 'handsfree ring "Shall I deploy the model?"')])])
    panel("call.png", [
        (text, [("head", "Call them, in their voice or the phone's"),
                ("row", ("voices", [("body", "Laptop: each agent in its own voice.")])),
                ("row", ("voices-phone", [("body", "Phone: one voice for all, steady on a weak signal.")]))]),
        (phone, [("shot", "call")])])
    panel("talk.png", [
        (phone, [("shot", "chat")]),
        (text, [("head", "Talk or text, any time"),
                ("body", "Every henchman has its own chat on your phone, your whole crew a tap away. Talk, hold the "
                         "button, or type; pictures and audio play right in it. Not urgent? The agent texts instead."),
                ("row", ("text", [("body", "Reply right from the notification. It goes to that agent alone, typed "
                                           "into its terminal.")])),
                ("code", 'handsfree text "Checkpoint pushed."')])])
    panel("plan.png", [
        (1640 - 2 * phone - 100, [("head", "See the plan, mind the limits"),
                ("body", "Each agent's plan sits beside its chat, steps ticking off as it works. Usage shows how "
                         "much of every agent's limits is left, and when they reset.")]),
        (phone, [("shot", "plan")]),
        (phone, [("shot", "usage")])])
    panel("away.png", [
        (440, [("face", avatars / "you_away.png")]),
        (1640 - 440 - 80, [("head", "Off the clock"),
                ("body", "Tap your avatar. Agents text instead of talking or ringing."),
                ("shot", "away"),
                ("row", ("later", [("body", "Send it later, or offline from the outbox. One tick sent, two picked up.")]))])])
    panel("files.png", [
        (1640 - phone - 120, [("head", "Files both ways"),
               ("body", "Share a photo, a recording, a zip from any app and pick the henchman it's for: it lands on "
                        "your computer and the agent is told where. Or tap the paperclip in a chat."),
               ("row", ("filecard", [("body", "Agents send files back, straight into their chat."),
                                     ("code", "handsfree send map.png")]))]),
        (phone, [("shot", "share")])])


if __name__ == "__main__":
    main()
