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
        self.rows: dict = {}
        # Tk is not thread-safe, and `after()` called from a worker thread is a
        # crash waiting for a busy moment. The worker only posts a result here;
        # the main thread picks it up on its own schedule.
        self.results: queue.Queue = queue.Queue()

        self.title("Calibração dos controles do jogo")
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

        ctk.CTkLabel(left, text="Passos", font=ctk.CTkFont(size=14, weight="bold"),
                     text_color=ACCENT).pack(anchor="w", padx=12, pady=(12, 2))
        ctk.CTkLabel(left, text="Clique em um passo para refazer só ele.",
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

        ctk.CTkButton(footer, text="Fechar", width=100, height=36, fg_color="#21262d",
                      command=self._close).pack(side="left")
        self.chk_chain = ctk.CTkCheckBox(footer, text="Avançar sozinho para o próximo passo",
                                         font=ctk.CTkFont(size=12), command=self._toggle_chain)
        self.chk_chain.select()
        self.chk_chain.pack(side="left", padx=16)

        ctk.CTkButton(footer, text="🔎 Testar este passo", width=180, height=36,
                      fg_color="#1f6feb", hover_color="#388bfd",
                      command=self.test_step).pack(side="right", padx=4)
        ctk.CTkButton(footer, text="📷 Atualizar captura", width=180, height=36,
                      command=self.refresh_capture).pack(side="right", padx=4)
        ctk.CTkButton(footer, text="Pular", width=90, height=36, fg_color="#21262d",
                      command=self.skip).pack(side="right", padx=4)
        ctk.CTkButton(footer, text="Refazer este passo", width=160, height=36, fg_color="#21262d",
                      command=self.redo).pack(side="right", padx=4)

        self._show_step()

    def _toggle_chain(self):
        self.chain = bool(self.chk_chain.get())

    # -------------------------------------------------------------- capture
    def refresh_capture(self):
        """Takes a fresh picture of the game page and shows it."""
        self._set_state("Capturando a tela do jogo...", MUTED)
        self.update_idletasks()
        try:
            image = self.ctx.browser.capture(scale=1.0)
        except Exception as exc:  # noqa: BLE001
            self._set_state(f"✖ Não foi possível capturar: {exc}", ERROR_COLOR)
            return
        if image is None or image.size == 0:
            self._set_state("✖ A captura veio vazia. O jogo está aberto na aba conectada?", ERROR_COLOR)
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
                f"⚠ estes passos estão FORA da página de {image.shape[1]}x{image.shape[0]} e "
                f"nunca vão funcionar: {nomes}. Foram gravados com a janela em outro tamanho — "
                f"clique em cada um na lista e refaça.", ERROR_COLOR)
            return

        self._set_state(
            f"Captura de {image.shape[1]}x{image.shape[0]} px. "
            f"Navegue no jogo e capture de novo quando a tela do passo estiver aberta.",
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
        kind = {"click": "um ponto", "area": "uma área", "region": "uma área com imagem de referência"}
        self.lbl_progress.configure(
            text=f"Passo {self.index + 1} de {len(STEPS)}  ·  {kind.get(step.type, step.type)}"
            + ("  ·  opcional" if step.optional else "")
        )
        self.lbl_title.configure(text=step.title)
        self.lbl_instruction.configure(text=step.instruction)

        done = self.calibration.is_calibrated(step.name)
        target = "o primeiro canto" if step.is_rectangle else "o ponto"
        self._set_state(
            ("Já calibrado — clique de novo para substituir. " if done else "")
            + f"Clique na captura, em {target}.",
            MUTED,
        )
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
        if self.screenshot is None:
            self._set_state("Capture a tela primeiro (botão 'Atualizar captura').", WARN_COLOR)
            return

        # A escala só existe depois que a janela foi desenhada. Um clique antes
        # disso viraria uma coordenada sem sentido gravada em silêncio - e uma
        # calibração errada só aparece de madrugada, na execução agendada.
        if self.display_scale < 0.05:
            self._set_state("A janela ainda está sendo desenhada. Clique de novo.", WARN_COLOR)
            self._draw()
            return

        step = self._current()
        page_x, page_y = self._to_page(event.x, event.y)

        # The capture rarely has the canvas's exact proportions, so there is
        # empty canvas beside or below it. A click there is off the page, and
        # recording it produced coordinates outside the game - a click that
        # reaches nothing and looks like a button that ignored it.
        height, width = self.screenshot.shape[:2]
        if not (0 <= page_x < width and 0 <= page_y < height):
            self._set_state(
                f"✖ clique fora da captura ({page_x}, {page_y}); a página tem {width}x{height}. "
                f"Clique dentro da imagem do jogo.", ERROR_COLOR)
            return

        if not step.is_rectangle:
            self.calibration.set_point(step.name, page_x, page_y)
            self.calibration.save()
            self._recorded(f"✔ ponto gravado em {page_x}, {page_y}")
            return

        self.corners.append((page_x, page_y))
        if len(self.corners) == 1:
            self._set_state(f"✔ primeiro canto em {page_x}, {page_y} — agora o canto oposto.", OK_COLOR)
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
            # Recorded, but not advanced: a reference like this is worse than
            # none, and chaining on would bury the warning under the next step.
            self._set_state(warning, WARN_COLOR)
            self._update_list()
            self._draw()
            return
        self._recorded(f"✔ área de {region['width']}x{region['height']} px gravada")

    def _recorded(self, message: str):
        self._set_state(message, OK_COLOR)
        self._update_list()
        self._draw()
        if self.chain and self.index < len(STEPS) - 1:
            self.after(450, lambda: self.go_to(self.index + 1))
        elif self.calibration.complete:
            self.after(300, self._finished)

    # ------------------------------------------------------------- verifying
    def test_step(self):
        """
        Exercises the calibrated step against the running game and shows the result.

        Calibrating and running were two separate worlds: a point could be a few
        pixels off, or covered by the store, and the only sign was a collection
        that quietly did nothing at three in the morning. Here the click is sent
        for real and the screen recaptured, so 'did the profile menu open?' is
        answered by looking at it.
        """
        step = self._current()
        if not self.calibration.is_calibrated(step.name):
            self._set_state("Este passo ainda não foi calibrado.", WARN_COLOR)
            return

        self._set_state("Testando...", MUTED)
        self.update_idletasks()
        threading.Thread(target=self._run_test, args=(step,), daemon=True).start()

    def _drain_results(self):
        """Applies whatever a test worker finished, on the thread that owns the widgets."""
        while True:
            try:
                message, colour, recapture = self.results.get_nowait()
            except queue.Empty:
                break
            if recapture:
                self.refresh_capture()
            self._set_state(message, colour)
        self.after(150, self._drain_results)

    def _run_test(self, step):
        recapture = False
        try:
            # The run rescales calibrated coordinates to the current page size;
            # the test has to go through the same path or it would prove nothing.
            self.ctx.sync_viewport()

            if step.type == "click":
                point = self.calibration.point(step.name)
                before = self.ctx.browser.capture(scale=1.0)
                self.ctx.browser.click(point[0], point[1], delay=1.2)
                after = self.ctx.browser.capture(scale=1.0)

                # Whether the click did anything is measured, not eyeballed: a
                # click that misses and one that lands on a dead spot look the
                # same from outside, and both were reported as 'nothing happens'.
                moved = changed_fraction(before, after)
                recapture = True
                if moved >= 0.01:
                    message = (f"✔ cliquei em ({point[0]}, {point[1]}) e a tela MUDOU "
                               f"({moved:.0%} dela). A captura abaixo é de depois do clique — "
                               f"abriu o que devia?")
                    colour = OK_COLOR
                else:
                    message = (f"✖ cliquei em ({point[0]}, {point[1]}) e a tela NÃO mudou "
                               f"({moved:.1%}). O clique caiu no vazio: ou o ponto está errado, "
                               f"ou algo está por cima dele (a loja, um modal).")
                    colour = ERROR_COLOR
                self.results.put((message, colour, recapture))
                return

            if step.type == "area":
                region = self.calibration.region(step.name)
                rows = self.ctx.ocr.read_rows(region)
                if rows:
                    message = f"✔ li nesta área: {' | '.join(rows[:6])}"
                    colour = OK_COLOR
                else:
                    message = ("✖ nenhum texto lido nesta área. A tela certa está aberta? "
                               "A área pega o texto todo?")
                    colour = ERROR_COLOR
            else:   # region: procura a imagem de referência na tela inteira
                match = self.ctx.vision.find(self.calibration.ref_path(step.name), None,
                                             base_scale=self.calibration.image_scale, attempts=1)
                if match:
                    message = (f"✔ imagem encontrada em ({match[0]}, {match[1]}) "
                               f"com semelhança {self.ctx.vision.last_score:.2f}")
                    colour = OK_COLOR
                else:
                    message = (f"✖ imagem não encontrada agora (melhor semelhança "
                               f"{self.ctx.vision.last_score:.2f}). Ela está visível na tela?")
                    colour = ERROR_COLOR
        except Exception as exc:  # noqa: BLE001
            message, colour = f"✖ falha no teste: {exc}", ERROR_COLOR

        self.results.put((message, colour, recapture))

    def redo(self):
        step = self._current()
        self.calibration.forget(step.name)
        self.corners.clear()
        self._show_step()
        self._set_state("Passo apagado. Clique na captura para gravar de novo.", WARN_COLOR)

    def skip(self):
        if self.index < len(STEPS) - 1:
            self.go_to(self.index + 1)

    def _finished(self):
        self.lbl_title.configure(text="Calibração completa", text_color=OK_COLOR)
        self._set_state("Todos os passos obrigatórios foram gravados em config/calibration.json.", OK_COLOR)

    def _set_state(self, message: str, color: str):
        self.lbl_state.configure(text=message, text_color=color)

    def _close(self):
        self.calibration.created_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.calibration.save()
        if self.on_done:
            self.on_done(self.calibration.complete)
        self.destroy()
