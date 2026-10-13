"""A tmux pane as a PNG that reads on a phone: its real colours, re-wrapped to SCREENSHOT_COLS.

Claude Code panes here are 290+ columns wide; a literal capture would shrink to unreadable on a
phone. So lines are wrapped at SCREENSHOT_COLS (default 110), full-width rules (────) are cut
to that width, trailing blank space is dropped, and only the last SCREENSHOT_LINES rows (the
part with the spinner, the latest output and the input box) are drawn.

Needs Pillow; fontTools picks a fallback font per glyph (the mono font lacks ⏺ ⎿ ⏵, and colour
emoji come from Noto Color Emoji). `render(pane)` returns PNG bytes, or None if it can't.
"""
import io
import os
import re
import subprocess
import unicodedata
from functools import lru_cache

COLS = int(os.environ.get("SCREENSHOT_COLS", "110"))
MAX_LINES = int(os.environ.get("SCREENSHOT_LINES", "60"))
SIZE = int(os.environ.get("SCREENSHOT_FONT_SIZE", "17"))
BG, FG = (24, 24, 32), (214, 214, 220)
FONTS = [os.environ.get("SCREENSHOT_FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
         "/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf",
         "/usr/share/fonts/truetype/freefont/FreeMono.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/noto/NotoSansMono-Regular.ttf",
         "/usr/share/fonts/opentype/unifont/unifont_upper.otf"]
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
EMOJI = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"

CSI = re.compile(r"\x1b\[([0-9;:?]*)([A-Za-z])")
OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
RULES = set("─━═╌╍┄┅")


def available():
    try:
        import PIL  # noqa: F401
        return os.path.exists(FONTS[0])
    except ImportError:
        return False


def _xterm(n):
    base = [(0, 0, 0), (205, 49, 49), (13, 188, 121), (229, 229, 16), (36, 114, 200), (188, 63, 188),
            (17, 168, 205), (229, 229, 229), (102, 102, 102), (241, 76, 76), (35, 209, 139), (245, 245, 67),
            (59, 142, 234), (214, 112, 214), (41, 184, 219), (255, 255, 255)]
    if n < 16:
        return base[n]
    if n < 232:
        n -= 16
        lv = [0, 95, 135, 175, 215, 255]
        return lv[n // 36], lv[(n // 6) % 6], lv[n % 6]
    g = 8 + (n - 232) * 10
    return g, g, g


def _sgr(params, st):
    """Apply one SGR parameter list to style dict st (fg, bg, bold, dim, italic, under, inv)."""
    p = [int(x) if x.isdigit() else 0 for x in re.split("[;:]", params)] if params else [0]
    i = 0
    while i < len(p):
        c = p[i]
        if c == 0:
            st.update(fg=None, bg=None, bold=False, dim=False, italic=False, under=False, inv=False)
        elif c == 1:
            st["bold"] = True
        elif c == 2:
            st["dim"] = True
        elif c == 3:
            st["italic"] = True
        elif c == 4:
            st["under"] = True
        elif c == 7:
            st["inv"] = True
        elif c == 22:
            st["bold"] = st["dim"] = False
        elif c == 23:
            st["italic"] = False
        elif c == 24:
            st["under"] = False
        elif c == 27:
            st["inv"] = False
        elif 30 <= c <= 37 or 90 <= c <= 97:
            st["fg"] = _xterm(c - 30 if c < 90 else c - 82)
        elif 40 <= c <= 47 or 100 <= c <= 107:
            st["bg"] = _xterm(c - 40 if c < 100 else c - 92)
        elif c == 39:
            st["fg"] = None
        elif c == 49:
            st["bg"] = None
        elif c in (38, 48) and i + 1 < len(p):
            key = "fg" if c == 38 else "bg"
            if p[i + 1] == 5 and i + 2 < len(p):
                st[key] = _xterm(p[i + 2] & 255)
                i += 2
            elif p[i + 1] == 2 and i + 4 < len(p):
                st[key] = tuple(v & 255 for v in p[i + 2:i + 5])
                i += 4
        i += 1


def _width(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def parse(text):
    """ANSI text -> rows of cells [(char, style tuple, width)]."""
    rows, st = [], dict(fg=None, bg=None, bold=False, dim=False, italic=False, under=False, inv=False)
    for line in OSC.sub("", text).split("\n"):
        cells, pos = [], 0
        for m in CSI.finditer(line):
            for ch in line[pos:m.start()]:
                if ch >= " " or ch == "\t":
                    cells.append((" " if ch == "\t" else ch, tuple(st.values()), _width(ch)))
            if m.group(2) == "m":
                _sgr(m.group(1), st)
            pos = m.end()
        for ch in line[pos:]:
            if ch >= " " or ch == "\t":
                cells.append((" " if ch == "\t" else ch, tuple(st.values()), _width(ch)))
        rows.append(cells)
    return rows


def layout(rows, cols=COLS, max_lines=MAX_LINES):
    """Trim, cut rules, wrap to `cols` cells, keep the last `max_lines` rows."""
    out = []
    for cells in rows:
        while cells and cells[-1][0] == " " and cells[-1][1][1] is None:   # trailing blank space
            cells = cells[:-1]
        # a full-width rule: cut the run to the wrap width
        trimmed, run = [], 0
        for c in cells:
            run = run + 1 if c[0] in RULES else 0
            if run <= cols:
                trimmed.append(c)
        cells = trimmed
        if not cells:
            out.append([])
            continue
        if sum(c[2] for c in cells) > cols:
            cells = _squeeze_gaps(cells)
        while sum(c[2] for c in cells) > cols:
            # break at the last space that leaves a reasonably full line, else hard-break
            w, cut = 0, None
            for i, c in enumerate(cells):
                if w + c[2] > cols:
                    break
                w += c[2]
                if c[0] == " " and w > cols * 0.6:
                    cut = i
            n = max(1, i if cut is None else cut + 1)
            out.append(cells[:n])
            rest = cells[n:]
            indent = next((k for k, c in enumerate(cells) if c[0] != " "), 0)    # keep the hanging indent
            cells = [(" ", rest[0][1] if rest else None, 1)] * min(indent, 8) + rest
        out.append(cells)
    while out and not out[-1]:
        out.pop()
    while out and not out[0]:
        out.pop(0)
    # squeeze runs of blank rows to one
    squeezed = []
    for r in out:
        if r or (squeezed and squeezed[-1]):
            squeezed.append(r)
    return squeezed[-max_lines:]


def _squeeze_gaps(cells, longest=3):
    """Right-aligned TUI bits ("…      1h 53m · ↓ 661k tokens") sit behind a long run of
    spaces; shrink such runs so the line can fit instead of wrapping."""
    out, run = [], []
    for c in cells:
        if c[0] == " " and c[1][1] is None:
            run.append(c)
            continue
        out.extend(run if len(run) < 6 or not out else run[:longest])
        run = []
        out.append(c)
    return out


@lru_cache(maxsize=None)
def _font(path, size):
    from PIL import ImageFont
    return ImageFont.truetype(path, size)


@lru_cache(maxsize=None)
def _cmap(path):
    from fontTools.ttLib import TTFont
    try:
        return frozenset(TTFont(path, lazy=True, fontNumber=0).getBestCmap())
    except Exception:  # noqa: BLE001
        return frozenset()


@lru_cache(maxsize=4096)
def _font_for(ch):
    for p in FONTS:
        if os.path.exists(p) and ord(ch) in _cmap(p):
            return p
    return None


@lru_cache(maxsize=512)
def _emoji(ch, cell_w, cell_h):
    """A colour emoji scaled into a 2-cell box (CBDT fonts only render at size 109)."""
    from PIL import Image, ImageDraw
    f = _font(EMOJI, 109)
    img = Image.new("RGBA", (160, 136), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((0, 0), ch, font=f, embedded_color=True)
    box = img.getbbox()
    if not box:
        return None
    img = img.crop(box)
    target_h = int(cell_h * 0.9)
    return img.resize((max(1, int(img.width * target_h / img.height)), target_h), Image.LANCZOS)


def image(rows, cols=COLS):
    from PIL import Image, ImageDraw
    reg = _font(FONTS[0], SIZE)
    cell_w = int(round(reg.getlength("M")))
    cell_h = int(SIZE * 1.32)
    width = max((sum(c[2] for c in r) for r in rows), default=40)
    pad = 18
    img = Image.new("RGB", (pad * 2 + max(width, 40) * cell_w, pad * 2 + max(len(rows), 1) * cell_h), BG)
    d = ImageDraw.Draw(img)
    has_emoji = os.path.exists(EMOJI)
    for y, row in enumerate(rows):
        x = 0
        top = pad + y * cell_h
        for ch, (fg, bg, bold, dim, italic, under, inv), w in row:
            fg, bg = fg or FG, bg
            if inv:
                fg, bg = (bg or BG), fg
            if dim:
                fg = tuple((a + b) // 2 for a, b in zip(fg, BG))
            left = pad + x * cell_w
            if bg:
                d.rectangle([left, top, left + w * cell_w - 1, top + cell_h - 1], fill=bg)
            if ch != " ":
                path = _font_for(ch)
                if path is None and has_emoji and ord(ch) in _cmap(EMOJI):
                    em = _emoji(ch, cell_w, cell_h)
                    if em:
                        img.paste(em, (left, top + (cell_h - em.height) // 2), em)
                elif ch not in ("️", "‍"):
                    f = _font(BOLD if bold and path == FONTS[0] and os.path.exists(BOLD) else (path or FONTS[0]), SIZE)
                    d.text((left, top + (cell_h - SIZE) // 2 - 1), ch, font=f, fill=fg)
            if under:
                d.line([left, top + cell_h - 2, left + w * cell_w, top + cell_h - 2], fill=fg)
            x += w
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def capture(pane):
    """The pane's visible screen with colours (alternate screen if a full-screen app is up)."""
    best = ""
    for extra in ([], ["-a"]):
        try:
            r = subprocess.run(["tmux", "capture-pane", "-p", "-e", *extra, "-t", pane],
                               capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0 and len(r.stdout.strip()) > len(best.strip()):
            best = r.stdout
    return best


def render(pane, cols=COLS, max_lines=MAX_LINES):
    """PNG bytes of `pane`, or None (no Pillow, no font, empty pane)."""
    if not available():
        return None
    text = capture(pane)
    if not text.strip():
        return None
    return image(layout(parse(text), cols, max_lines), cols)
