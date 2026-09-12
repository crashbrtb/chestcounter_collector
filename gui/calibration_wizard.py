"""
Calibration wizard: marking the game's controls on a picture of the game.

Calibrating by clicking the live screen - the way the other tools in this family
do it - would mean converting monitor pixels into page pixels, through the
window position, the browser chrome and the display scaling. Every one of those
can change between the calibration and the run.

Working on a screenshot taken through CDP removes the whole chain: the image IS
the page, so a click on it is already a page coordinate. It also freezes the
game while the user works, which matters because the store opens itself and
tooltips come and go under the cursor.

The magnifier follows the pointer because the targets are small: a few pixels
off on the 'Open' button and the reference image ends up with a slice of the
background in it.

The steps are the run in order, and the wizard plays it: a step that marks a
button presses it before opening the next step, so the game arrives at the
screen that step describes. Marking without pressing left the two out of step -
the wizard asking for something inside a menu that was never opened - and the
whole sequence had to be navigated by hand alongside it.
"""

import queue
import threading
import time
import tkinter as tk
from typing import Callable, List, Optional, Tuple

import customtkinter as ctk
from PIL import Image, ImageTk

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

from core.calibration import STEPS, STEPS_BY_NAME, Calibration
from core.vision import changed_fraction

CANVAS_BG = "#0d1117"
PANEL_BG = "#16181d"
ACCENT = "#58a6ff"
OK_COLOR = "#3fb950"
WARN_COLOR = "#d29922"
ERROR_COLOR = "#f85149"
MUTED = "#8b949e"

MAGNIFIER_SIZE = 150
MAGNIFIER_ZOOM = 4


class CalibrationWizard(ctk.CTkToplevel):
    """Records points and regions by clicking on a capture of the game page."""

    def __init__(self, master, context, on_done: Optional[Callable[[bool], None]] = None):
        super().__init__(master)
        self.ctx = context
        self.calibration: Calibration = context.calibration
        self.on_done = on_done

        self.index = 0
        self.corners: List[Tuple[int, int]] = []
        self.screenshot = None            # numpy BGR, page pixels
        self.photo: Optional[ImageTk.PhotoImage] = None
        self.display_scale = 1.0
        self.chain = True
        # True while a worker is driving the game (performing a step, running a
        # test). Clicks on the capture are ignored meanwhile: the capture on
        # screen is already stale, so a point marked on it would be wrong.
        self.busy = False
        self.prompt = ""
        self.rows: dict = {}
        # Tk is not thread-safe, and `after()` called from a worker thread is a
        # crash waiting for a busy moment. The worker only posts a result here;
        # the main thread picks it up on its own schedule.
        self.results: queue.Queue = queue.Queue()

        self.title("Game Controls Calibration")
        self.geometry("1280x780")
        self.minsize(1050, 640)
        self.protocol("WM_DELETE_WINDOW", self._close)

        self._build()
        self.after(200, self.refresh_capture)
        self.after(150, self._drain_results)

    # ------------------------------------------------------------------- UI
    def _build(self):
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12, pady=(12, 0))

        # --- steps
        left = ctk.CTkFrame(body, fg_color=PANEL_BG, corner_radius=8, width=310)
        left.pack(side="left", fill="y", padx=(0, 10))
        left.pack_propagate(False)

        ctk.CTkLabel(left, text="Steps", font=ctk.CTkFont(size=14, weight="bold"),
                     text_color=ACCENT).pack(anchor="w", padx=12, pady=(12, 2))
        ctk.CTkLabel(left, text="Click a step to redo only that one.",
                     font=ctk.CTkFont(size=11), text_color=MUTED,
                     wraplength=280, justify="left").pack(anchor="w", padx=12, pady=(0, 8))

        self.steps_list = ctk.CTkScrollableFrame(left, fg_color=CANVAS_BG)
        self.steps_list.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        for position, step in enumerate(STEPS):
            button = ctk.CTkButton(
                self.steps_list, text="", anchor="w", height=30,
                fg_color="transparent", hover_color="#21262d",
                font=ctk.CTkFont(size=12),
                command=lambda index=position: self.go_to(index),
            )
            button.pack(fill="x", pady=1)
            self.rows[position] = button

        # --- capture and instruction
        right = ctk.CTkFrame(body, fg_color=PANEL_BG, corner_radius=8)
        right.pack(side="left", fill="both", expand=True)

        header = ctk.CTkFrame(right, fg_color="transparent")
        header.pack(fill="x", padx=14, pady=(12, 4))

        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.pack(side="left", fill="x", expand=True)
        self.lbl_progress = ctk.CTkLabel(titles, text="", font=ctk.CTkFont(size=11), text_color=MUTED)
        self.lbl_progress.pack(anchor="w")
        self.lbl_title = ctk.CTkLabel(titles, text="", font=ctk.CTkFont(size=17, weight="bold"),
                                      text_color=ACCENT)
        self.lbl_title.pack(anchor="w")

        self.magnifier = tk.Canvas(header, width=MAGNIFIER_SIZE, height=MAGNIFIER_SIZE,
                                   bg=CANVAS_BG, highlightthickness=1, highlightbackground="#30363d")
        self.magnifier.pack(side="right", padx=(10, 0))

        self.lbl_instruction = ctk.CTkLabel(right, text="", font=ctk.CTkFont(size=13),
                                            justify="left", wraplength=780, text_color="#c9d1d9")
        self.lbl_instruction.pack(anchor="w", padx=14, pady=(4, 6))

        self.canvas = tk.Canvas(right, bg=CANVAS_BG, highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Configure>", lambda _event: self._draw())

        self.lbl_state = ctk.CTkLabel(right, text="", font=ctk.CTkFont(size=12, weight="bold"),
                                      text_color=MUTED, justify="left", wraplength=780)
        self.lbl_state.pack(anchor="w", padx=14, pady=(0, 10))

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=16, pady=12)

        ctk.CTkButton(footer, text="Close", width=100, height=36, fg_color="#21262d",
                      command=self._close).pack(side="left")
        self.chk_chain = ctk.CTkCheckBox(
            footer, text="Advance automatically\n(performs the marked click in the game)",
            font=ctk.CTkFont(size=11), command=self._toggle_chain)
        self.chk_chain.select()
        self.chk_chain.pack(side="left", padx=16)

        ctk.CTkButton(footer, text="🔎 Test this step", width=150, height=36,
                      fg_color="#1f6feb", hover_color="#388bfd",
                      command=self.test_step).pack(side="right", padx=4)
        ctk.CTkButton(footer, text="🧰 Test chest capture", width=180, height=36,
                      fg_color="#1f6feb", hover_color="#388bfd",
                      command=self.test_chests).pack(side="right", padx=4)
        ctk.CTkButton(footer, text="📷 Refresh capture", width=160, height=36,
                      command=self.refresh_capture).pack(side="right", padx=4)
        ctk.CTkButton(footer, text="Skip", width=80, height=36, fg_color="#21262d",
                      command=self.skip).pack(side="right", padx=4)
        ctk.CTkButton(footer, text="Redo", width=90, height=36, fg_color="#21262d",
                      command=self.redo).pack(side="right", padx=4)

        self._show_step()

    def _toggle_chain(self):
        self.chain = bool(self.chk_chain.get())

    # -------------------------------------------------------------- capture
    def refresh_capture(self):
        """Takes a fresh picture of the game page and shows it."""
        self._set_state("Capturing game screen...", MUTED)
        self.update_idletasks()
        try:
            image = self.ctx.browser.capture(scale=1.0)
        except Exception as exc:  # noqa: BLE001
            self._set_state(f"✖ Could not capture: {exc}", ERROR_COLOR)
            return
        if image is None or image.size == 0:
            self._set_state("✖ Capture is empty. Is the game open on the connected tab?", ERROR_COLOR)
            return

        self.screenshot = image
        width, height = self.ctx.browser.viewport()
        if width and height:
            self.calibration.set_viewport(width, height)
        self._draw()

        fora = self.calibration.out_of_bounds(image.shape[1], image.shape[0])
        if fora:
            nomes = ", ".join(STEPS_BY_NAME[n].title for n in fora if n in STEPS_BY_NAME)
            self._set_state(
                f"⚠ These steps are OUTSIDE the {image.shape[1]}x{image.shape[0]} page and "
                f"will never work: {nomes}. They were recorded with the window at a different size — "
                f"click each in the list and redo.", ERROR_COLOR)
            return

        self._set_state(
            f"Capture size: {image.shape[1]}x{image.shape[0]} px. "
            f"Navigate in the game and refresh capture when the step screen is visible.",
            MUTED,
        )

    def _draw(self):
        """Redraws the capture, scaled to fit, plus what is already calibrated."""
        self.canvas.delete("all")
        if self.screenshot is None or cv2 is None:
            return

        canvas_w = max(self.canvas.winfo_width(), 10)
        canvas_h = max(self.canvas.winfo_height(), 10)
        height, width = self.screenshot.shape[:2]
        self.display_scale = min(canvas_w / width, canvas_h / height, 1.0)

        shown_w = max(1, int(width * self.display_scale))
        shown_h = max(1, int(height * self.display_scale))
        rgb = cv2.cvtColor(self.screenshot, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb).resize((shown_w, shown_h), Image.LANCZOS)
        self.photo = ImageTk.PhotoImage(image)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)

        step = STEPS[self.index]
        if step.is_rectangle:
            region = self.calibration.regions.get(step.name)
            if region:
                self._outline(region["left"], region["top"],
                              region["left"] + region["width"], region["top"] + region["height"], OK_COLOR)
        else:
            point = self.calibration.points.get(step.name)
            if point:
                self._crosshair(point[0], point[1], OK_COLOR)

        if len(self.corners) == 1:
            self._crosshair(self.corners[0][0], self.corners[0][1], WARN_COLOR)

    def _to_canvas(self, x: int, y: int) -> Tuple[float, float]:
        return x * self.display_scale, y * self.display_scale

    def _to_page(self, x: int, y: int) -> Tuple[int, int]:
        if self.display_scale <= 0:
            return x, y
        return int(round(x / self.display_scale)), int(round(y / self.display_scale))

    def _crosshair(self, x: int, y: int, color: str):
        cx, cy = self._to_canvas(x, y)
        self.canvas.create_line(cx - 9, cy, cx + 9, cy, fill=color, width=2)
        self.canvas.create_line(cx, cy - 9, cx, cy + 9, fill=color, width=2)
        self.canvas.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, outline=color, width=2)

    def _outline(self, x1: int, y1: int, x2: int, y2: int, color: str):
        ax, ay = self._to_canvas(x1, y1)
        bx, by = self._to_canvas(x2, y2)
        self.canvas.create_rectangle(ax, ay, bx, by, outline=color, width=2)

    def _on_motion(self, event):
        """Draws the magnifier around the pointer, in original resolution."""
        self.magnifier.delete("all")
        if self.screenshot is None or cv2 is None:
            return
        page_x, page_y = self._to_page(event.x, event.y)
        half = MAGNIFIER_SIZE // (2 * MAGNIFIER_ZOOM)
        height, width = self.screenshot.shape[:2]
        left, top = max(0, page_x - half), max(0, page_y - half)
        right, bottom = min(width, page_x + half), min(height, page_y + half)
        if right - left < 2 or bottom - top < 2:
            return

        crop = cv2.cvtColor(self.screenshot[top:bottom, left:right], cv2.COLOR_BGR2RGB)
        zoomed = Image.fromarray(crop).resize(
            (MAGNIFIER_SIZE, MAGNIFIER_SIZE), Image.NEAREST
        )
        self._magnifier_photo = ImageTk.PhotoImage(zoomed)
        self.magnifier.create_image(0, 0, anchor="nw", image=self._magnifier_photo)
        middle = MAGNIFIER_SIZE // 2
        self.magnifier.create_line(middle, 0, middle, MAGNIFIER_SIZE, fill=ACCENT)
        self.magnifier.create_line(0, middle, MAGNIFIER_SIZE, middle, fill=ACCENT)
        self.magnifier.create_text(middle, MAGNIFIER_SIZE - 10, text=f"{page_x}, {page_y}",
                                   fill="#ffffff", font=("Consolas", 9))

    # ----------------------------------------------------------------- steps
    def _current(self):
        return STEPS[self.index]

    def go_to(self, index: int):
        self.index = max(0, min(index, len(STEPS) - 1))
        self.corners.clear()
        self._show_step()

    def _show_step(self):
        step = self._current()
        kind = {"click": "a point", "area": "an area", "region": "an area with reference image"}
        self.lbl_progress.configure(
            text=f"Step {self.index + 1} of {len(STEPS)}  ·  {kind.get(step.type, step.type)}"
            + ("  ·  optional" if step.optional else "")
        )
        self.lbl_title.configure(text=step.title)
        self.lbl_instruction.configure(text=step.instruction)

        done = self.calibration.is_calibrated(step.name)
        target = "the first corner" if step.is_rectangle else "the point"
        self.prompt = (
            ("Already calibrated — click again to replace. " if done else "")
            + f"Click on the capture, at {target}."
        )
        self._set_state(self.prompt, MUTED)
        self._update_list()
        self._draw()

    def _update_list(self):
        for position, step in enumerate(STEPS):
            done = self.calibration.is_calibrated(step.name)
            mark = "✔" if done else ("○" if not step.optional else "·")
            current = "▸ " if position == self.index else "   "
            self.rows[position].configure(
                text=f"{current}{mark}  {position + 1:>2}. {step.title}",
                text_color=OK_COLOR if done else MUTED,
                fg_color="#1f2733" if position == self.index else "transparent",
            )

    def _on_click(self, event):
        if self.busy:
            self._set_state("Wait: the game is being driven by the previous step.", WARN_COLOR)
            return
        if self.screenshot is None:
            self._set_state("Capture the screen first ('Refresh capture' button).", WARN_COLOR)
            return

        # Scaling only exists after the window has been drawn.
        if self.display_scale < 0.05:
            self._set_state("The window is still rendering. Click again.", WARN_COLOR)
            self._draw()
            return

        step = self._current()
        page_x, page_y = self._to_page(event.x, event.y)

        height, width = self.screenshot.shape[:2]
        if not (0 <= page_x < width and 0 <= page_y < height):
            self._set_state(
                f"✖ Click outside capture ({page_x}, {page_y}); page size is {width}x{height}. "
                f"Click inside the game image.", ERROR_COLOR)
            return

        if not step.is_rectangle:
            self.calibration.set_point(step.name, page_x, page_y)
            self.calibration.save()
            self._recorded(f"✔ Point saved at {page_x}, {page_y}")
            return

        self.corners.append((page_x, page_y))
        if len(self.corners) == 1:
            self._set_state(f"✔ First corner at {page_x}, {page_y} — now click the opposite corner.", OK_COLOR)
            self._draw()
            return

        try:
            warning = self.calibration.set_region(
                step.name, self.corners[0], self.corners[1], self.screenshot
            )
            self.calibration.save()
        except ValueError as exc:
            self.corners.clear()
            self._set_state(f"✖ {exc}", ERROR_COLOR)
            self._draw()
            return

        region = self.calibration.regions[step.name]
        self.corners.clear()
        if warning:
            self._set_state(warning, WARN_COLOR)
            self._update_list()
            self._draw()
            return
        self._recorded(f"✔ Area of {region['width']}x{region['height']} px saved")

    def _recorded(self, message: str):
        self._set_state(message, OK_COLOR)
        self._update_list()
        self._draw()
        if self.chain:
            self.after(400, self._advance)
        elif self.calibration.complete:
            self.after(300, self._finished)

    # --------------------------------------------------------------- advancing
    def _advance(self):
        """
        Moves on - after doing in the game whatever this step marked.

        Advancing the wizard alone left the game a screen behind: step 4 asks
        for something inside the clan menu while the menu is still closed,
        because the button just marked in step 3 was never pressed. So the step
        that marks a button presses it, and the next step opens on the screen it
        expects. A step that only marks an area to read has nothing to press and
        merely takes a fresh capture.

        If the click changes nothing the wizard stays put: continuing would put
        the next step's marks on the wrong screen, which is the failure this is
        here to prevent.
        """
        step = self._current()
        last = self.index >= len(STEPS) - 1

        if not step.acts:
            if last:
                if self.calibration.complete:
                    self.after(300, self._finished)
                return
            self.refresh_capture()
            self._go_next("Nothing to press in this step.")
            return

        target = self.calibration.action_point(step.name)
        if target is None:
            self._go_next("Could not work out where to click for this step.")
            return

        self.busy = True
        self._set_state(f"Pressing '{step.title}' in the game before moving on...", MUTED)
        self.update_idletasks()
        threading.Thread(target=self._run_advance, args=(step, last), daemon=True).start()

    def _run_advance(self, step, last: bool):
        """Presses what the step marked, and reports whether the game reacted."""
        try:
            self.ctx.sync_viewport()
            target = self.calibration.action_point(step.name)
            if target is None:
                self._post("✖ This step has no point to press.", ERROR_COLOR)
                return

            before = self.ctx.browser.capture(scale=1.0)
            self.ctx.browser.click(target[0], target[1], delay=1.2)
            after = self.ctx.browser.capture(scale=1.0)
            moved = changed_fraction(before, after)

            if moved < 0.01:
                self._post(
                    f"✖ Pressed ({target[0]}, {target[1]}) and the screen DID NOT change "
                    f"({moved:.1%}), so the game is still on this step's screen. Staying here: "
                    f"redo this step, or navigate by hand and refresh the capture.",
                    ERROR_COLOR, recapture=True)
                return

            message = (f"✔ Pressed ({target[0]}, {target[1]}); the screen changed "
                       f"({moved:.0%}).")
            self._post(message, OK_COLOR, recapture=True, advance=not last)
        except Exception as exc:  # noqa: BLE001
            self._post(f"✖ Could not press this step: {exc}", ERROR_COLOR)

    def _go_next(self, note: str = "", colour: str = MUTED):
        """Opens the next step, keeping whatever was just reported visible above its prompt."""
        if self.index >= len(STEPS) - 1:
            if self.calibration.complete:
                self._finished()
            return
        self.go_to(self.index + 1)
        if note:
            self._set_state(note + "\n" + self.prompt, colour)

    def _post(self, message: str, colour: str, recapture: bool = False,
              advance: bool = False, chests=None):
        """A worker's result, for the main thread to apply."""
        self.results.put({"message": message, "colour": colour, "recapture": recapture,
                          "advance": advance, "chests": chests})

    # ------------------------------------------------------------- verifying
    def test_step(self):
        """
        Exercises the calibrated step against the running game and shows the result.
        """
        step = self._current()
        if not self.calibration.is_calibrated(step.name):
            self._set_state("This step has not been calibrated yet.", WARN_COLOR)
            return

        if self.busy:
            self._set_state("Wait: the game is already being driven.", WARN_COLOR)
            return
        self.busy = True
        self._set_state("Testing...", MUTED)
        self.update_idletasks()
        threading.Thread(target=self._run_test, args=(step,), daemon=True).start()

    def _drain_results(self):
        """Applies whatever a worker finished, on the thread that owns the widgets."""
        while True:
            try:
                result = self.results.get_nowait()
            except queue.Empty:
                break
            self.busy = False
            if result.get("recapture"):
                self.refresh_capture()
            if result.get("chests") is not None:
                ChestTestWindow(self, *result["chests"])
            if result.get("advance"):
                self._go_next(result["message"], result["colour"])
            else:
                self._set_state(result["message"], result["colour"])
        self.after(150, self._drain_results)

    def _run_test(self, step):
        recapture = False
        try:
            self.ctx.sync_viewport()

            if step.type == "click":
                point = self.calibration.point(step.name)
                before = self.ctx.browser.capture(scale=1.0)
                self.ctx.browser.click(point[0], point[1], delay=1.2)
                after = self.ctx.browser.capture(scale=1.0)

                moved = changed_fraction(before, after)
                recapture = True
                if moved >= 0.01:
                    message = (f"✔ Clicked at ({point[0]}, {point[1]}) and screen CHANGED "
                               f"({moved:.0%} changed). Below is the capture after click — "
                               f"did the intended element open?")
                    colour = OK_COLOR
                else:
                    message = (f"✖ Clicked at ({point[0]}, {point[1]}) and screen DID NOT change "
                               f"({moved:.1%}). The click hit nothing: either the point is wrong, "
                               f"or something is covering it (store, modal dialog).")
                    colour = ERROR_COLOR
                self._post(message, colour, recapture)
                return

            if step.type == "area":
                region = self.calibration.region(step.name)
                backend = self.ctx.ocr.backend()
                backend_name = backend.name
                rows = self.ctx.ocr.read_rows(region)
                from core.ocr import RapidOCRBackend
                rapid_available, _ = RapidOCRBackend.available()
                is_fallback = "tesseract" in backend_name.lower() and not rapid_available

                if rows:
                    sample = ' | '.join(rows[:6])
                    if is_fallback:
                        message = (
                            f"⚠ Read [{backend_name} - FALLBACK]: {sample}\n"
                            f"ALERTA: RapidOCR indisponível! O Tesseract tem precisão baixa para perfis e nomes. Execute install.bat para instalar RapidOCR."
                        )
                        colour = WARN_COLOR
                    else:
                        message = f"✔ Read [{backend_name}]: {sample}"
                        colour = OK_COLOR
                else:
                    message = (f"✖ No text read in this area using {backend_name}. Is the correct screen open? "
                               "Does the area cover the full text?")
                    colour = ERROR_COLOR
            else:   # region: search reference image across whole page
                match = self.ctx.vision.find(self.calibration.ref_path(step.name), None,
                                             base_scale=self.calibration.image_scale, attempts=1)
                if match:
                    message = (f"✔ Image found at ({match[0]}, {match[1]}) "
                               f"with similarity {self.ctx.vision.last_score:.2f}")
                    colour = OK_COLOR
                else:
                    message = (f"✖ Image not found currently (best similarity "
                               f"{self.ctx.vision.last_score:.2f}). Is it visible on screen?")
                    colour = ERROR_COLOR
        except Exception as exc:  # noqa: BLE001
            message, colour = f"✖ Test failed: {exc}", ERROR_COLOR

        self._post(message, colour, recapture)

    # --------------------------------------------------- the chest panel test
    def test_chests(self):
        """
        Asks the calibration the question the run asks: which chests do you see?

        The three chest steps only make sense together - the 'Open' button is
        found by its picture, and each chest's text is placed relative to the
        button found for it - so testing them one at a time proves nothing about
        the panel. Here the real detection runs against the live screen and its
        answer is drawn: four boxes and four readings, or the reason there are
        not.
        """
        if self.busy:
            self._set_state("Wait: the game is already being driven.", WARN_COLOR)
            return

        missing = self.calibration.require("chest_area", "open_button_area", "open_button")
        if missing:
            titles = ", ".join(STEPS_BY_NAME[name].title for name in missing)
            self._set_state(f"Calibrate these steps first: {titles}.", WARN_COLOR)
            return

        self.busy = True
        self._set_state("Looking for chests on the current screen and reading them...", MUTED)
        self.update_idletasks()
        threading.Thread(target=self._run_chest_test, daemon=True).start()

    def _run_chest_test(self):
        from modules.chest_collector import visible_chests

        try:
            self.ctx.sync_viewport()
            chests = visible_chests(self.calibration, self.ctx.vision, self.ctx.browser)
            image = self.ctx.browser.capture(scale=1.0)

            if not chests:
                self._post(
                    f"✖ No 'Open' button found on this screen (best similarity "
                    f"{self.ctx.vision.last_score:.2f}). Open the gifts tab, or redo the "
                    f"'Open button' step - the reference may have been cropped too loosely.",
                    ERROR_COLOR, recapture=True)
                return

            # Read each chest's own area rather than the whole panel at once:
            # the point here is which text belongs to which chest, and that is
            # exactly what a per-chest read shows.
            readings = [self.ctx.ocr.read_rows(chest["text_area"]) for chest in chests]

            complete = sum(1 for rows in readings if len(rows) >= 3 and all(rows[:3]))
            if complete == len(chests):
                message = (f"✔ {len(chests)} chest(s) found and all read completely. "
                           f"The panel normally shows four.")
                colour = OK_COLOR
            else:
                message = (f"⚠ {len(chests)} chest(s) found, {complete} read completely. "
                           f"Check the marked areas in the window that just opened.")
                colour = WARN_COLOR
            self._post(message, colour, chests=(image, chests, readings))
        except Exception as exc:  # noqa: BLE001
            self._post(f"✖ Chest test failed: {exc}", ERROR_COLOR)

    def redo(self):
        step = self._current()
        self.calibration.forget(step.name)
        self.corners.clear()
        self._show_step()
        self._set_state("Step cleared. Click on the capture to calibrate again.", WARN_COLOR)

    def skip(self):
        if self.index < len(STEPS) - 1:
            self.go_to(self.index + 1)

    def _finished(self):
        self.lbl_title.configure(text="Calibration complete", text_color=OK_COLOR)
        self._set_state("All required steps have been saved to config/calibration.json.", OK_COLOR)

    def _set_state(self, message: str, color: str):
        self.lbl_state.configure(text=message, text_color=color)

    def _close(self):
        self.calibration.created_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.calibration.save()
        if self.on_done:
            self.on_done(self.calibration.complete)
        self.destroy()


BUTTON_BOX = (90, 220, 80)      # BGR - where an 'Open' button was found
TEXT_BOX = (255, 170, 60)       # BGR - where that chest's text will be read


class ChestTestWindow(ctk.CTkToplevel):
    """
    What the collector sees in the chest panel, drawn and read back.

    The three chest steps fail quietly: a slightly loose 'Open' reference finds
    three buttons instead of four, and a text area a few pixels high cuts the
    source line off - both of which look like an ordinary run until the numbers
    are counted the next morning. Showing the boxes over the real screen, next
    to the text read out of each one, makes that visible in the second it takes
    to look.
    """

    def __init__(self, master, image, chests, readings):
        super().__init__(master)
        self.title("Chest capture test")
        self.geometry("1020x780")
        self.minsize(760, 560)
        self.transient(master)

        complete = sum(1 for rows in readings if len(rows) >= 3 and all(rows[:3]))
        headline = f"{len(chests)} chest(s) detected  ·  {complete} read completely"
        colour = OK_COLOR if complete == len(chests) and chests else WARN_COLOR

        ctk.CTkLabel(self, text=headline, font=ctk.CTkFont(size=16, weight="bold"),
                     text_color=colour).pack(anchor="w", padx=16, pady=(14, 0))
        ctk.CTkLabel(self,
                     text="Green box: the 'Open' button that was found.   "
                          "Blue box: the area whose text is saved for that chest.   "
                          "The panel normally shows four chests.",
                     font=ctk.CTkFont(size=11), text_color=MUTED,
                     justify="left").pack(anchor="w", padx=16, pady=(2, 8))

        self.preview = tk.Label(self, bg=CANVAS_BG, bd=0)
        self.preview.pack(padx=16, pady=(0, 10))
        self._render(image, chests)

        ctk.CTkLabel(self, text="What was read in each chest area",
                     font=ctk.CTkFont(size=13, weight="bold"), text_color=ACCENT
                     ).pack(anchor="w", padx=16, pady=(0, 4))

        listing = ctk.CTkScrollableFrame(self, fg_color=CANVAS_BG)
        listing.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        for position, (chest, rows) in enumerate(zip(chests, readings), start=1):
            self._entry(listing, position, chest, rows)

        ctk.CTkButton(self, text="Close", width=110, height=34, fg_color="#21262d",
                      command=self.destroy).pack(pady=(0, 14))

    def _render(self, image, chests):
        """Draws the boxes over the capture, cropped to the panel so they are readable."""
        if image is None or cv2 is None or getattr(image, "size", 0) == 0:
            self.preview.configure(text="No capture to show", fg="#c9d1d9")
            return

        drawn = image.copy()
        height, width = drawn.shape[:2]
        boxes = []
        for position, chest in enumerate(chests, start=1):
            bx, by, bw, bh = chest["button"]
            tx, ty, tw, th = chest["text_area"]
            cv2.rectangle(drawn, (tx, ty), (tx + tw, ty + th), TEXT_BOX, 2)
            cv2.rectangle(drawn, (bx, by), (bx + bw, by + bh), BUTTON_BOX, 2)
            cv2.putText(drawn, str(position), (max(0, tx + 6), max(14, ty + 22)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, TEXT_BOX, 2, cv2.LINE_AA)
            boxes.append((tx, ty, tx + tw, ty + th))
            boxes.append((bx, by, bx + bw, by + bh))

        if boxes:
            margin = 40
            left = max(0, min(b[0] for b in boxes) - margin)
            top = max(0, min(b[1] for b in boxes) - margin)
            right = min(width, max(b[2] for b in boxes) + margin)
            bottom = min(height, max(b[3] for b in boxes) + margin)
            if right - left > 20 and bottom - top > 20:
                drawn = drawn[top:bottom, left:right]

        shown = Image.fromarray(cv2.cvtColor(drawn, cv2.COLOR_BGR2RGB))
        scale = min(960 / shown.width, 380 / shown.height, 1.0)
        if scale < 1.0:
            shown = shown.resize((max(1, int(shown.width * scale)),
                                  max(1, int(shown.height * scale))), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(shown)
        self.preview.configure(image=self._photo)

    @staticmethod
    def _entry(parent, position: int, chest, rows):
        from utils.text_utils import parse_key_value_line

        name = rows[0].strip() if len(rows) >= 1 else ""
        player = parse_key_value_line(rows[1])[1] if len(rows) >= 2 else ""
        source = parse_key_value_line(rows[2])[1] if len(rows) >= 3 else ""
        ok = bool(name and player and source)

        card = ctk.CTkFrame(parent, fg_color=PANEL_BG, corner_radius=6)
        card.pack(fill="x", pady=3, padx=2)

        button = chest["button"]
        ctk.CTkLabel(card,
                     text=f"{'✔' if ok else '✖'} Chest {position}   ·   button at "
                          f"({button[0]}, {button[1]})",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=OK_COLOR if ok else ERROR_COLOR).pack(anchor="w", padx=12, pady=(8, 2))

        if not rows:
            ctk.CTkLabel(card, text="nothing read in this area — it is probably in the wrong "
                                    "place, or too small",
                         font=ctk.CTkFont(size=12), text_color=ERROR_COLOR,
                         justify="left").pack(anchor="w", padx=24, pady=(0, 8))
            return

        values = ctk.CTkFrame(card, fg_color="transparent")
        values.pack(anchor="w", padx=24, pady=(0, 8 if ok else 0))
        for label, value in (("Chest", name), ("Player", player), ("Source", source)):
            ctk.CTkLabel(values, text=f"{label}: {value or '— missing —'}",
                         font=ctk.CTkFont(size=12),
                         text_color="#c9d1d9" if value else WARN_COLOR).pack(side="left", padx=(0, 18))

        # The raw rows only matter when something is wrong with them: they are
        # what shows whether a line was cut in half or landed in the wrong chest.
        if not ok:
            ctk.CTkLabel(card, text="read: " + " | ".join(rows),
                         font=ctk.CTkFont(size=11), text_color=MUTED,
                         wraplength=900, justify="left").pack(anchor="w", padx=24, pady=(2, 8))
