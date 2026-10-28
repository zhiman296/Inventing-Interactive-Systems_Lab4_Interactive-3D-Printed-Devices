import sys, time, json, math, threading, os, random, statistics
import pygame

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None

# ---------------- settings ----------------
W, H = 1000, 700
BAUD = 115200
HERE = os.path.dirname(os.path.abspath(__file__))
CAL_FILE = os.path.join(HERE, "pottery_calibration.json")
FONT_REGULAR = os.path.join(HERE, "Poppins-Regular.ttf")
FONT_MEDIUM = os.path.join(HERE, "Poppins-Medium.ttf")

# what to show (True / False)
SHOW_HINTS = True                # key hints at the bottom
SHOW_LABELS = True               # "left hand / right hand" words under the little bars
SHOW_STATUS = False              # connection title in the corner (a warning still appears if the ESP32 is not found)
SHOW_RAW = False                 # raw sensor numbers next to the bars

N_ROWS = 90                      # number of height slices in the pot
POT_X, BASE_Y, POT_H = W // 2, 560, 370
DY = POT_H / (N_ROWS - 1)
R_START, R_MIN, R_MAX = 85.0, 16.0, 165.0
SQUEEZE_SPEED = 35.0             # px per second at full press (was 60: lower = less sensitive)
WIDEN_SPEED = 60.0               # px per second while SPACE is held
SIGMA = 3.5                      # softness of the brush, in rows
SPIN_SPEED = 1.6                 # wheel speed, radians per second
STRIP = 2                        # height in pixels of one drawn slice of the pot
RATIO = 0.30                     # how much we look down into the pot (bigger = more top view)
RIDGE_AMP = 0.6                  # strength of the hand-thrown ridges, in pixels

# --- sensitivity of the right hand (thinning) ---
SQUEEZE_START = 0.35             # right sensor must be at least this covered (0 - 1) before anything happens
SQUEEZE_CURVE = 1.8              # above 1 = light covering counts even less, full covering still counts fully
CROSSTALK = 0.40                 # how much of the LEFT sensor's darkness also shows up on the RIGHT sensor
                                 # (the left hand's shadow). 0 = none removed. Tune with keys 1 / 2.

CLAY = (198, 148, 122)           # terracotta
RIM = (218, 172, 146)
INK = (64, 54, 52)
SOFT = (130, 118, 120)
PAPER = (246, 240, 226)          # calibration screen background
WARN = (186, 72, 58)
ACCENT = (200, 128, 102)


# ---------------- fonts ----------------
_fonts = {}
def font(size, medium=False):
    key = (size, medium)
    if key not in _fonts:
        path = FONT_MEDIUM if medium else FONT_REGULAR
        if os.path.exists(path):
            _fonts[key] = pygame.font.Font(path, size)
        else:
            for name in ("Helvetica Neue", "Helvetica", "Arial", "Segoe UI", "Verdana"):
                p = pygame.font.match_font(name)
                if p:
                    _fonts[key] = pygame.font.Font(p, size)
                    break
            else:
                _fonts[key] = pygame.font.SysFont(None, size + 8)
    return _fonts[key]


def text(surf, s, size, color, pos, anchor="topleft", medium=False):
    img = font(size, medium).render(s, True, color)
    rect = img.get_rect(**{anchor: pos})
    surf.blit(img, rect)
    return rect


# ---------------- reading the ESP32 ----------------
def find_port():
    if serial is None:
        return None
    for p in serial.tools.list_ports.comports():
        t = (p.device + " " + (p.description or "")).lower()
        if any(k in t for k in ("usbserial", "slab", "wch", "cp210", "ch340", "usbmodem", "uart")):
            return p.device
    return None


class SensorReader:
    """Reads lines like '2310,1984' from the ESP32 in a background thread."""

    def __init__(self, port):
        self.port = port
        self.values = None
        self.last_time = 0.0
        self.error = None
        self.lock = threading.Lock()
        self.running = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            ser = serial.Serial(self.port, BAUD, timeout=1)
            time.sleep(1.5)                  # the ESP32 restarts when the port opens
            ser.reset_input_buffer()
            while self.running:
                parts = ser.readline().decode(errors="ignore").strip().split(",")
                if len(parts) != 2:
                    continue
                try:
                    a, b = int(parts[0]), int(parts[1])
                except ValueError:
                    continue
                with self.lock:
                    self.values = (a, b)
                    self.last_time = time.time()
        except Exception as e:
            self.error = str(e)

    def get(self):
        with self.lock:
            return self.values, self.last_time


# ---------------- calibration ----------------
def load_calibration():
    try:
        with open(CAL_FILE) as f:
            d = json.load(f)
        return d["bright"], d["dark"]
    except Exception:
        return None


def save_calibration(bright, dark):
    with open(CAL_FILE, "w") as f:
        json.dump({"bright": bright, "dark": dark}, f)


def to_pressure(value, bright, dark):
    span = bright - dark
    if span < 150:
        return 0.0
    p = min(1.0, max(0.0, (bright - value) / span))
    return 0.0 if p < 0.06 else (p - 0.06) / 0.94      # small dead zone


def right_squeeze(raw_b, tgt_a):
    """Turns the right sensor's pressure (0 - 1) into the thinning amount (0 - 1).

    1. Takes away the part of the right reading that is only the left hand's shadow.
    2. Ignores light covering (below SQUEEZE_START).
    3. Makes the rest gentler with a curve, so only a real cover gives a strong squeeze.
    """
    shadow = CROSSTALK * tgt_a
    p = max(0.0, raw_b - shadow) / max(0.05, 1.0 - shadow)
    p = min(1.0, p)
    if p < SQUEEZE_START:
        return 0.0
    p = (p - SQUEEZE_START) / (1.0 - SQUEEZE_START)
    return p ** SQUEEZE_CURVE


# ---------------- the clay ----------------
def new_clay():
    return [R_START] * N_ROWS


def apply_brush(radii, cursor, squeeze, widen, dt):
    c = cursor * (N_ROWS - 1)
    lo, hi = max(0, int(c - 3 * SIGMA)), min(N_ROWS - 1, int(c + 3 * SIGMA) + 1)
    for i in range(lo, hi + 1):
        w = math.exp(-((i - c) ** 2) / (2 * SIGMA ** 2))
        radii[i] += (-SQUEEZE_SPEED * squeeze + WIDEN_SPEED * widen) * w * dt
        radii[i] = max(R_MIN, min(R_MAX, radii[i]))


def radius_at(radii, y):
    f = (BASE_Y - y) / DY
    i = int(f)
    if i < 0:
        return radii[0]
    if i >= N_ROWS - 1:
        return radii[-1]
    t = f - i
    return radii[i] * (1 - t) + radii[i + 1] * t


def ridge(y):
    """Uneven hand-thrown bands going around the pot."""
    return 0.6 * math.sin(0.55 * y + 1.1) + 0.45 * math.sin(0.23 * y + 2.3) + 0.3 * math.sin(0.85 * y + 0.4)


# ---------------- look: background, wheel ----------------
def make_background():
    """Soft pale studio background with slightly darker corners."""
    sw, sh = 100, 70
    small = pygame.Surface((sw, sh))
    for y in range(sh):
        for x in range(sw):
            d = math.hypot((x - sw / 2) / (sw / 2), (y - sh * 0.45) / (sh / 2))
            t = min(1.0, d / 1.35) ** 1.6
            small.set_at((x, y), (int(249 - 20 * t), int(245 - 24 * t), int(247 - 20 * t)))
    return pygame.transform.smoothscale(small, (W, H))


def draw_wheel(surf, spin):
    cx, cy, rx, ry, thick = POT_X, BASE_Y + 12, 335, 66, 28
    side, side_dark = (196, 188, 190), (168, 160, 164)
    top, lip = (230, 224, 224), (246, 242, 242)
    pygame.draw.ellipse(surf, side_dark, (cx - rx, cy + thick - ry, 2 * rx, 2 * ry))     # underside
    pygame.draw.rect(surf, side, (cx - rx, cy, 2 * rx, thick))                           # rim wall
    pygame.draw.rect(surf, side_dark, (cx + int(rx * 0.5), cy, int(rx * 0.5), thick))    # shadowed side
    pygame.draw.ellipse(surf, top, (cx - rx, cy - ry, 2 * rx, 2 * ry))                   # top surface
    pygame.draw.ellipse(surf, lip, (cx - rx, cy - ry, 2 * rx, 2 * ry), 4)
    for k in (0.90, 0.76, 0.58, 0.38):                                                   # grooves
        e = (cx - rx * k, cy - ry * k, 2 * rx * k, 2 * ry * k)
        pygame.draw.ellipse(surf, (208, 200, 202), e, 2)
        pygame.draw.ellipse(surf, (248, 245, 245), (e[0], e[1] + 3, e[2], e[3]), 1)
    for k in range(10):                                                                  # faint marks show the wheel turning
        a = spin + k * 2 * math.pi / 10
        p1 = (cx + rx * 0.94 * math.cos(a), cy + ry * 0.94 * math.sin(a))
        p2 = (cx + rx * 0.84 * math.cos(a), cy + ry * 0.84 * math.sin(a))
        pygame.draw.line(surf, (214, 207, 208), p1, p2, 2)


# ---------------- look: the clay ----------------
LEVELS = 72
G_W = 128
_grad = []
_streaks = []


def mul(color, k):
    return tuple(max(0, min(255, int(c * k))) for c in color)


def norm3(v):
    n = math.sqrt(sum(c * c for c in v))
    return tuple(c / n for c in v)


LIGHT = norm3((-0.50, 0.62, 0.60))
VIEW = (0.0, 0.33, 0.94)
HALF = norm3(tuple(a + b for a, b in zip(LIGHT, VIEW)))


def clay_color(u, nz):
    """u: -1 (left edge) .. 1 (right edge) across the pot. nz: how much the surface faces up."""
    nr = math.sqrt(max(0.0, 1 - nz * nz))
    n = (nr * u, nz, nr * math.sqrt(max(0.0, 1 - u * u)))
    diffuse = max(0.0, sum(a * b for a, b in zip(n, LIGHT)))
    spec = max(0.0, sum(a * b for a, b in zip(n, HALF))) ** 22
    bounce = 0.12 * max(0.0, n[0])
    k = 0.30 + 0.80 * diffuse + bounce
    return tuple(max(0, min(255, int(CLAY[i] * k + (255, 236, 226)[i] * 0.62 * spec))) for i in range(3))


def build_pot_tools():
    for k in range(LEVELS):
        nz = -0.9 + 1.8 * k / (LEVELS - 1)
        g = pygame.Surface((G_W, 1)).convert()
        for x in range(G_W):
            g.set_at((x, 0), clay_color(-1 + 2 * (x + 0.5) / G_W, nz))
        _grad.append(g)
    rnd = random.Random(11)
    for _ in range(9):                                       # soft smears that drift as the pot turns
        _streaks.append({
            "th": rnd.uniform(0, 2 * math.pi), "y0": rnd.uniform(0.05, 0.7), "len": rnd.uniform(0.15, 0.45),
            "w": rnd.choice((8, 10, 14)), "dark": rnd.random() < 0.6, "a": rnd.randint(14, 30), "wob": rnd.uniform(0, 6),
        })


def level_for(nz):
    return max(0, min(LEVELS - 1, int(round((nz + 0.9) / 1.8 * (LEVELS - 1)))))


def arc_points(cx, cy, rx, ry, a0, a1, n=26):
    return [(cx + rx * math.cos(a0 + (a1 - a0) * i / n), cy - ry * math.sin(a0 + (a1 - a0) * i / n)) for i in range(n + 1)]


def glow_arc(surf, pts, ox, oy):
    """A soft white highlight along the rim."""
    pts = [(x - ox, y - oy) for x, y in pts]
    for width, alpha in ((11, 34), (7, 70), (3, 185)):
        pygame.draw.lines(surf, (255, 246, 240, alpha), False, pts, width)


def draw_opening(surf, cx, top_y, rt, ry):
    lip = max(3.0, rt * 0.10)
    # thick rim
    pygame.draw.ellipse(surf, RIM, (cx - rt, top_y - ry, 2 * rt, 2 * ry))
    irx, iry = rt - lip, max(2.0, ry - lip * RATIO * 1.3)
    w, h = int(2 * irx), int(2 * iry)
    if w >= 8 and h >= 5:
        inner = pygame.Surface((w, h), pygame.SRCALPHA)
        for yy in range(h):                                   # far wall lit, near wall in shade
            t = yy / max(1, h - 1)
            c = tuple(int(a + (b - a) * t ** 0.75) for a, b in zip((196, 146, 120), (112, 74, 60)))
            pygame.draw.line(inner, c + (255,), (0, yy), (w, yy))
        for k in range(1, 6):                                 # ridges on the far inside wall (arcs, like the real thing)
            f = 1 - 0.13 * k
            cyk = h * (0.50 + 0.085 * k)
            col = (255, 232, 220, 46) if k % 2 else (86, 52, 42, 60)
            pts = arc_points(w / 2, cyk, w / 2 * f, h / 2 * f * 0.92, 0.12, math.pi - 0.12, 30)
            pygame.draw.lines(inner, col, False, pts, 3)
        mask = pygame.Surface((w, h), pygame.SRCALPHA)
        pygame.draw.ellipse(mask, (255, 255, 255, 255), mask.get_rect())
        inner.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        surf.blit(inner, (cx - irx, top_y - iry))
        pygame.draw.ellipse(surf, mul(CLAY, 0.62), (cx - irx, top_y - iry, w, h), 2)
    # glossy highlights on the rim
    gw, gh = int(2 * rt + 24), int(2 * ry + 24)
    gl = pygame.Surface((gw, gh), pygame.SRCALPHA)
    ox, oy = cx - rt - 12, top_y - ry - 12
    mr, mry = rt - lip / 2, ry - lip * RATIO / 2
    glow_arc(gl, arc_points(cx, top_y, mr, mry, 0.35, 1.05), ox, oy)               # back rim, right
    glow_arc(gl, arc_points(cx, top_y, mr, mry, math.pi + 0.55, math.pi + 1.15), ox, oy)   # front rim, left
    surf.blit(gl, (ox, oy))
    pygame.draw.ellipse(surf, mul(CLAY, 0.66), (cx - rt, top_y - ry, 2 * rt, 2 * ry), 1)


def draw_pot(surf, radii, spin):
    cx = POT_X
    top_y = BASE_Y - POT_H
    ys = list(range(top_y, BASE_Y + 1, STRIP))
    rs = [max(2.0, radius_at(radii, y) + RIDGE_AMP * ridge(y)) for y in ys]
    r0, rt = radii[0], radii[-1]

    # soft shadow on the wheel, a little to the right
    sw_, sh_ = int(2 * r0 + 110), int(RATIO * 2 * r0 + 54)
    sh = pygame.Surface((sw_, sh_), pygame.SRCALPHA)
    for k in range(9):
        f = 1 - k * 0.085
        pygame.draw.ellipse(sh, (70, 56, 66, 15), (sw_ * (1 - f) / 2, sh_ * (1 - f) / 2, sw_ * f, sh_ * f))
    surf.blit(sh, sh.get_rect(center=(cx + 26, BASE_Y + 10)))

    # rounded underside
    ry0 = r0 * RATIO
    y = BASE_Y
    while y < BASE_Y + ry0:
        w = int(2 * r0 * math.sqrt(max(0.0, 1 - ((y - BASE_Y) / ry0) ** 2)))
        if w > 2:
            surf.blit(pygame.transform.smoothscale(_grad[level_for(-0.55)], (w, STRIP)), (cx - w // 2, y))
        y += STRIP

    # body: thin horizontal slices, each lit according to how it tilts (this is what makes the ridges)
    for n, y in enumerate(ys):
        a, b = max(0, n - 2), min(len(rs) - 1, n + 2)
        slope = (rs[b] - rs[a]) / (STRIP * (b - a))           # radius change per pixel going down
        nz = slope / math.sqrt(1 + slope * slope)             # > 0 means the surface faces up
        w = int(2 * rs[n])
        if w > 2:
            surf.blit(pygame.transform.smoothscale(_grad[level_for(nz)], (w, STRIP)), (cx - w // 2, y))

    # outline that also smooths the edge
    edge = mul(CLAY, 0.64)
    pygame.draw.aalines(surf, edge, False, [(cx - r, y) for r, y in zip(rs, ys)])
    pygame.draw.aalines(surf, edge, False, [(cx + r, y) for r, y in zip(rs, ys)])

    # smears that drift sideways as the pot turns
    ow, oh = int(2 * R_MAX + 40), POT_H + 40
    ox, oy = cx - ow // 2, top_y - 20
    ov = pygame.Surface((ow, oh), pygame.SRCALPHA)
    for s in _streaks:
        th = s["th"] + spin
        c = math.cos(th)
        if c < 0.2:
            continue
        pts = []
        for j in range(0, int(s["len"] * POT_H), 6):
            yy = int(top_y + s["y0"] * POT_H + j)
            if yy > BASE_Y:
                break
            t2 = th + 0.10 * math.sin(yy * 0.03 + s["wob"])
            pts.append((cx + radius_at(radii, yy) * math.sin(t2) - ox, yy - oy))
        if len(pts) > 1:
            col = (92, 54, 44, int(s["a"] * c)) if s["dark"] else (255, 228, 212, int(s["a"] * c))
            pygame.draw.lines(ov, col, False, pts, s["w"])
    surf.blit(ov, (ox, oy))

    draw_opening(surf, cx, top_y, rs[0], max(4.0, rs[0] * RATIO))


def draw_cursor(surf, radii, cursor, squeeze):
    i = int(round(cursor * (N_ROWS - 1)))
    y = BASE_Y - i * DY
    r = radii[i]
    reach = 18 + 26 * squeeze
    for col, wd in (((255, 255, 255), 7), ((92, 72, 68), 3)):          # white outline, dark arrow
        for sgn in (-1, 1):                                           # arrows push in when you squeeze
            tip = POT_X + sgn * (r + 10)
            tail = tip + sgn * (22 + reach)
            pygame.draw.line(surf, col, (tail, y), (tip + sgn * 6, y), wd)
            ah = 8 + (wd - 3) // 2
            pygame.draw.polygon(surf, col, [(tip, y), (tip + sgn * 14, y - ah), (tip + sgn * 14, y + ah)])


# ---------------- small corner display ----------------
def draw_hud(surf, status, status_ok, pA, pB, raw):
    pw = 196
    ph = 112 if SHOW_LABELS else 84
    if SHOW_STATUS:
        ph += 26
    px, py = W - pw - 16, 16
    card = pygame.Surface((pw, ph), pygame.SRCALPHA)
    pygame.draw.rect(card, (255, 255, 255, 215), card.get_rect(), border_radius=16)
    surf.blit(card, (px, py))
    top = py + 12
    if SHOW_STATUS:
        text(surf, status, 15, (90, 150, 100) if status_ok else WARN, (px + pw // 2, top), "midtop")
        top += 26
    for col, (name, p, rv) in enumerate((("left hand", pA, raw[0]), ("right hand", pB, raw[1]))):
        mx = px + pw // 4 + col * (pw // 2)
        pygame.draw.rect(surf, (232, 226, 228), (mx - 11, top, 22, 60), border_radius=9)
        h = int(60 * p)
        if h > 0:
            pygame.draw.rect(surf, ACCENT, (mx - 11, top + 60 - h, 22, h), border_radius=9)
        if SHOW_RAW:
            text(surf, str(rv) if rv is not None else "-", 12, SOFT, (mx + 17, top), "topleft")
        if SHOW_LABELS:
            text(surf, name, 14, INK, (mx, top + 68), "midtop", medium=True)


def draw_hints(surf):
    text(surf, "C calibrate   ·   SPACE push out   ·   R new clay   ·   S save picture   ·   ESC quit", 16, SOFT, (22, H - 34))


# ---------------- full-screen calibration page ----------------
def draw_calibration(surf, state, has_data, message, secs_left):
    surf.fill(PAPER)                                          # opaque: hides the pot completely
    if state == "ready":
        cy = H // 2
        text(surf, "Calibrated", 60, INK, (W // 2, cy - 70), "center", medium=True)
        text(surf, "Press SPACE to start", 36, INK, (W // 2, cy + 20), "center")
        text(surf, "C = calibrate again", 22, SOFT, (W // 2, cy + 90), "center")
        return
    if state in ("cal_open_wait", "cal_open_collect"):
        title = "Step 1 of 2"
        lines = ["Keep your hands away from both sensors", "then press SPACE"]
    else:
        title = "Step 2 of 2"
        lines = ["Cover both sensors with your hands and hold", "then press SPACE"]
    cy = H // 2
    text(surf, title, 60, INK, (W // 2, cy - 110), "center", medium=True)
    if "collect" in state:
        text(surf, "Reading... hold still", 38, INK, (W // 2, cy + 10), "center")
        text(surf, "%.1f" % max(0.0, secs_left), 32, SOFT, (W // 2, cy + 70), "center")
    else:
        for k, line in enumerate(lines):
            text(surf, line, 36, INK, (W // 2, cy + 10 + k * 54), "center")
        if not has_data:
            text(surf, "Waiting for ESP32 data...", 28, WARN, (W // 2, cy + 150), "center")
        elif message:
            for k, part in enumerate(message.split(" - ")):
                text(surf, part, 26, WARN, (W // 2, cy + 150 + k * 36), "center")


# ---------------- main ----------------
def main():
    global CROSSTALK, SQUEEZE_START
    args = sys.argv[1:]
    demo = "--demo" in args
    port = next((a for a in args if not a.startswith("--")), None)
    reader = None
    if not demo:
        port = port or find_port()
        if serial is None or port is None:
            print("No ESP32 found - running in DEMO mode. (Use: python pottery_game.py /dev/cu.xxxx)")
            demo = True
        else:
            print("Using port:", port)
            reader = SensorReader(port)

    pygame.init()
    screen = pygame.display.set_mode((W, H))
    pygame.display.set_caption("Pottery wheel")
    clock = pygame.time.Clock()
    background = make_background().convert()
    build_pot_tools()

    radii = new_clay()
    spin, cursor, pA, pB = 0.0, 0.0, 0.0, 0.0
    cal = load_calibration() if not demo else None
    state = "play" if (demo or cal) else "cal_open_wait"
    samples, t0 = [], 0.0
    bright, dark = (cal if cal else (None, None))
    message, msg_until = "", 0.0
    save_now = False
    wait_release = False

    running = True
    while running:
        dt = clock.tick(60) / 1000.0
        spin += SPIN_SPEED * dt
        now = time.time()
        values, last = (None, 0.0) if reader is None else reader.get()
        has_data = values is not None and now - last < 1.0

        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                running = False
            elif e.type == pygame.KEYDOWN:
                if e.key == pygame.K_ESCAPE:
                    running = False
                elif e.key == pygame.K_r and state == "play":
                    radii = new_clay()
                elif e.key == pygame.K_s and state == "play":
                    save_now = True
                elif e.key == pygame.K_c and not demo:
                    state, message = "cal_open_wait", ""
                elif e.key == pygame.K_1 and state == "play":
                    CROSSTALK = max(0.0, round(CROSSTALK - 0.05, 2))
                    message, msg_until = "crosstalk removal %.2f" % CROSSTALK, time.time() + 2
                elif e.key == pygame.K_2 and state == "play":
                    CROSSTALK = min(0.9, round(CROSSTALK + 0.05, 2))
                    message, msg_until = "crosstalk removal %.2f" % CROSSTALK, time.time() + 2
                elif e.key == pygame.K_3 and state == "play":
                    SQUEEZE_START = max(0.0, round(SQUEEZE_START - 0.05, 2))
                    message, msg_until = "squeeze starts at %.2f" % SQUEEZE_START, time.time() + 2
                elif e.key == pygame.K_4 and state == "play":
                    SQUEEZE_START = min(0.9, round(SQUEEZE_START + 0.05, 2))
                    message, msg_until = "squeeze starts at %.2f" % SQUEEZE_START, time.time() + 2
                elif e.key == pygame.K_SPACE and state == "ready":
                    state, wait_release = "play", True           # SPACE is still down: don't push clay yet
                elif e.key == pygame.K_SPACE and has_data:
                    if state == "cal_open_wait":
                        state, samples, t0, message = "cal_open_collect", [], time.time(), ""
                    elif state == "cal_closed_wait":
                        state, samples, t0, message = "cal_closed_collect", [], time.time(), ""

        # ----- calibration steps -----
        secs_left = 0.0
        if state in ("cal_open_collect", "cal_closed_collect"):
            if has_data:
                samples.append(values)
            secs_left = 2.0 - (time.time() - t0)
            if secs_left <= 0 and samples:
                med = [statistics.median(s[k] for s in samples) for k in (0, 1)]
                if state == "cal_open_collect":
                    bright, state = med, "cal_closed_wait"
                else:
                    dark = med
                    if all(bright[k] - dark[k] >= 150 for k in (0, 1)):
                        save_calibration(bright, dark)
                        state, message = "ready", ""          # wait for the player to press SPACE
                    else:
                        state = "cal_open_wait"
                        message = "Not enough difference - cover the sensors fully and check the wiring - try again"

        # ----- read the two pressures -----
        if demo:
            _, my = pygame.mouse.get_pos()
            tgt_a = min(1.0, max(0.0, (BASE_Y - my) / POT_H))
            tgt_b = 1.0 if pygame.mouse.get_pressed()[0] else 0.0
            raw = (None, None)
        elif state == "play" and has_data:
            tgt_a = to_pressure(values[0], bright[0], dark[0])
            raw_b = to_pressure(values[1], bright[1], dark[1])
            tgt_b = right_squeeze(raw_b, tgt_a)          # less sensitive, ignores the left hand's shadow
            raw = values
        else:
            tgt_a, tgt_b = 0.0, 0.0
            raw = values if has_data else (None, None)
        k = min(1.0, dt * 12)
        pA += (tgt_a - pA) * k
        pB += (tgt_b - pB) * k
        cursor += (pA - cursor) * min(1.0, dt * 8)

        if state == "play":
            space_down = pygame.key.get_pressed()[pygame.K_SPACE]
            if not space_down:
                wait_release = False
            widen = 1.0 if (space_down and not wait_release) else 0.0
            apply_brush(radii, cursor, pB, widen, dt)

        # ----- draw the game -----
        screen.blit(background, (0, 0))
        draw_wheel(screen, spin)
        draw_pot(screen, radii, spin)
        if save_now:                                           # picture without the cursor and the corner display
            name = os.path.join(HERE, "pottery_%d.png" % int(time.time()))
            pygame.image.save(screen, name)
            message, msg_until, save_now = "Saved " + os.path.basename(name), time.time() + 3, False
        if state == "play":
            draw_cursor(screen, radii, cursor, pB)
            status = "demo mode (mouse)" if demo else ("controller connected" if has_data else "waiting for ESP32...")
            draw_hud(screen, status, demo or has_data, pA, pB, raw)
            if SHOW_HINTS:
                draw_hints(screen)
            if not SHOW_STATUS and not demo and not has_data:        # only speak up when something is wrong
                text(screen, "Waiting for ESP32 data...", 22, WARN, (W // 2, 20), "midtop")
            if reader is not None and reader.error:
                text(screen, "Could not open the port: " + reader.error[:46], 20, WARN, (W // 2, 50), "midtop")
            if message and time.time() < msg_until:
                text(screen, message, 26, INK, (W // 2, H - 64), "midbottom", medium=True)
        else:
            draw_calibration(screen, state, has_data, message, secs_left)    # covers everything, only the words show
        if not pygame.key.get_focused():                       # keys would go to another window (e.g. a search bar)
            text(screen, "Click this window first, so the keyboard works here", 22, WARN, (W // 2, H - 24), "midbottom")
        pygame.display.flip()

    if reader:
        reader.running = False
    pygame.quit()


if __name__ == "__main__":
    main()