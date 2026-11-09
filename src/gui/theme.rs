//! The look of the interface: palette, fonts, spacing and the few custom widgets
//! (toggle switch, side navigation item, status pill) the window is built from.

use eframe::egui::{self, Color32, FontData, FontDefinitions, FontFamily, FontId, Margin, RichText, Rounding, Stroke, TextStyle};

pub const BG: Color32 = Color32::from_rgb(0x0f, 0x13, 0x1a);
pub const PANEL: Color32 = Color32::from_rgb(0x13, 0x18, 0x21);
pub const CARD: Color32 = Color32::from_rgb(0x19, 0x1f, 0x2a);
pub const BORDER: Color32 = Color32::from_rgb(0x27, 0x2f, 0x3d);
pub const INPUT: Color32 = Color32::from_rgb(0x0d, 0x11, 0x18);
pub const TEXT: Color32 = Color32::from_rgb(0xe6, 0xea, 0xf0);
pub const TEXT_DIM: Color32 = Color32::from_rgb(0xc3, 0xcb, 0xd6);
pub const MUTED: Color32 = Color32::from_rgb(0x8b, 0x95, 0xa5);
pub const ACCENT: Color32 = Color32::from_rgb(0x4c, 0x8d, 0xff);
pub const OK: Color32 = Color32::from_rgb(0x3f, 0xb9, 0x50);
pub const WARN: Color32 = Color32::from_rgb(0xe3, 0xa5, 0x2b);
pub const ERR: Color32 = Color32::from_rgb(0xf8, 0x51, 0x49);
pub const GREEN: Color32 = Color32::from_rgb(0x23, 0x86, 0x36);

const WIDGET: Color32 = Color32::from_rgb(0x23, 0x2b, 0x38);
const WIDGET_HOVER: Color32 = Color32::from_rgb(0x2c, 0x36, 0x46);
const WIDGET_ACTIVE: Color32 = Color32::from_rgb(0x34, 0x40, 0x54);

/// The family of the semibold face; falls back to the regular one where it is missing.
pub fn semibold() -> FontFamily {
    FontFamily::Name("semibold".into())
}

pub fn title(text: impl Into<String>, size: f32) -> RichText {
    RichText::new(text).family(semibold()).size(size).color(TEXT)
}

pub fn install(ctx: &egui::Context) {
    install_fonts(ctx);
    let mut style = (*ctx.style()).clone();
    style.text_styles = [
        (TextStyle::Heading, FontId::new(22.0, FontFamily::Proportional)),
        (TextStyle::Body, FontId::new(14.0, FontFamily::Proportional)),
        (TextStyle::Button, FontId::new(14.0, FontFamily::Proportional)),
        (TextStyle::Small, FontId::new(12.0, FontFamily::Proportional)),
        (TextStyle::Monospace, FontId::new(12.5, FontFamily::Monospace)),
    ]
    .into();
    let s = &mut style.spacing;
    s.item_spacing = egui::vec2(8.0, 8.0);
    s.button_padding = egui::vec2(12.0, 5.0);
    s.interact_size = egui::vec2(40.0, 28.0);
    s.window_margin = Margin::same(18.0);
    s.menu_margin = Margin::same(6.0);
    s.combo_width = 200.0;
    s.text_edit_width = 240.0;

    let mut v = egui::Visuals::dark();
    v.panel_fill = BG;
    v.window_fill = CARD;
    v.window_stroke = Stroke::new(1.0f32, BORDER);
    v.window_rounding = Rounding::same(12.0);
    v.menu_rounding = Rounding::same(8.0);
    v.window_shadow = egui::epaint::Shadow { offset: egui::vec2(0.0, 10.0), blur: 30.0, spread: 0.0, color: Color32::from_black_alpha(140) };
    v.popup_shadow = egui::epaint::Shadow { offset: egui::vec2(0.0, 6.0), blur: 16.0, spread: 0.0, color: Color32::from_black_alpha(120) };
    v.extreme_bg_color = INPUT;
    v.faint_bg_color = CARD;
    v.code_bg_color = INPUT;
    v.hyperlink_color = ACCENT;
    v.selection.bg_fill = ACCENT.gamma_multiply(0.45);
    v.selection.stroke = Stroke::new(1.0f32, TEXT);
    v.warn_fg_color = WARN;
    v.error_fg_color = ERR;
    let rounding = Rounding::same(7.0);
    let w = &mut v.widgets;
    w.noninteractive.bg_fill = CARD;
    w.noninteractive.weak_bg_fill = CARD;
    w.noninteractive.bg_stroke = Stroke::new(1.0f32, BORDER);
    w.noninteractive.fg_stroke = Stroke::new(1.0f32, TEXT_DIM);
    w.noninteractive.rounding = rounding;
    for (state, fill) in [(&mut w.inactive, WIDGET), (&mut w.hovered, WIDGET_HOVER), (&mut w.active, WIDGET_ACTIVE), (&mut w.open, WIDGET)] {
        state.bg_fill = fill;
        state.weak_bg_fill = fill;
        state.rounding = rounding;
        state.expansion = 0.0;
    }
    w.inactive.bg_stroke = Stroke::new(1.0f32, BORDER);
    w.inactive.fg_stroke = Stroke::new(1.0f32, TEXT);
    w.hovered.bg_stroke = Stroke::new(1.0f32, ACCENT.gamma_multiply(0.7));
    w.hovered.fg_stroke = Stroke::new(1.5f32, TEXT);
    w.active.bg_stroke = Stroke::new(1.0f32, ACCENT);
    w.active.fg_stroke = Stroke::new(2.0f32, TEXT);
    w.open.bg_stroke = Stroke::new(1.0f32, ACCENT);
    w.open.fg_stroke = Stroke::new(1.0f32, TEXT);
    style.visuals = v;
    ctx.set_style(style);
}

/// Segoe UI and Cascadia Mono from Windows when they are there; egui's own fonts stay
/// behind them for every glyph they lack (the emoji and the icons).
fn install_fonts(ctx: &egui::Context) {
    let dir = std::path::PathBuf::from(std::env::var("WINDIR").unwrap_or_else(|_| r"C:\Windows".into())).join("Fonts");
    let mut fonts = FontDefinitions::default();
    let mut load = |name: &str, files: &[&str]| -> bool {
        for f in files {
            if let Ok(bytes) = std::fs::read(dir.join(f)) {
                fonts.font_data.insert(name.to_string(), FontData::from_owned(bytes));
                return true;
            }
        }
        false
    };
    let regular = load("ui", &["segoeui.ttf"]);
    let bold = load("ui-semibold", &["seguisb.ttf", "segoeuib.ttf"]);
    let mono = load("ui-mono", &["CascadiaMono.ttf", "consola.ttf"]);
    let symbols = load("ui-symbols", &["seguisym.ttf"]);
    let proportional = fonts.families.entry(FontFamily::Proportional).or_default();
    if regular {
        proportional.insert(0, "ui".into());
    }
    if symbols {
        proportional.push("ui-symbols".into());
    }
    let proportional = proportional.clone();
    let monospace = fonts.families.entry(FontFamily::Monospace).or_default();
    if mono {
        monospace.insert(0, "ui-mono".into());
    }
    if symbols {
        monospace.push("ui-symbols".into());
    }
    let mut strong = if bold { vec!["ui-semibold".to_string()] } else { Vec::new() };
    strong.extend(proportional);
    fonts.families.insert(semibold(), strong);
    ctx.set_fonts(fonts);
}

fn lerp(a: Color32, b: Color32, t: f32) -> Color32 {
    let m = |x: u8, y: u8| (x as f32 + (y as f32 - x as f32) * t).round() as u8;
    Color32::from_rgb(m(a.r(), b.r()), m(a.g(), b.g()), m(a.b(), b.b()))
}

/// An on/off switch in place of a checkbox.
pub fn toggle(ui: &mut egui::Ui, on: &mut bool) -> egui::Response {
    let (rect, mut response) = ui.allocate_exact_size(egui::vec2(38.0, 22.0), egui::Sense::click());
    if response.clicked() {
        *on = !*on;
        response.mark_changed();
    }
    if ui.is_rect_visible(rect) {
        let t = ui.ctx().animate_bool_responsive(response.id, *on);
        let radius = rect.height() / 2.0;
        let mut bg = lerp(Color32::from_rgb(0x3a, 0x44, 0x55), ACCENT, t);
        if response.hovered() {
            bg = lerp(bg, Color32::WHITE, 0.08);
        }
        ui.painter().rect_filled(rect, radius, bg);
        let x = egui::lerp((rect.left() + radius)..=(rect.right() - radius), t);
        ui.painter().circle_filled(egui::pos2(x, rect.center().y), radius - 3.5, Color32::WHITE);
    }
    response.on_hover_cursor(egui::CursorIcon::PointingHand)
}

/// An entry of the side navigation.
pub fn nav_item(ui: &mut egui::Ui, selected: bool, icon: &str, label: &str) -> egui::Response {
    let (rect, response) = ui.allocate_exact_size(egui::vec2(ui.available_width(), 38.0), egui::Sense::click());
    if ui.is_rect_visible(rect) {
        let painter = ui.painter();
        let fill = if selected {
            ACCENT.gamma_multiply(0.16)
        } else if response.hovered() {
            Color32::from_white_alpha(8)
        } else {
            Color32::TRANSPARENT
        };
        painter.rect_filled(rect, 8.0, fill);
        if selected {
            let bar = egui::Rect::from_min_size(rect.min + egui::vec2(0.0, 9.0), egui::vec2(3.0, rect.height() - 18.0));
            painter.rect_filled(bar, 2.0, ACCENT);
        }
        let color = if selected { TEXT } else if response.hovered() { TEXT_DIM } else { MUTED };
        painter.text(rect.left_center() + egui::vec2(16.0, 0.0), egui::Align2::LEFT_CENTER, icon, FontId::proportional(15.0),
                     if selected { ACCENT } else { color });
        painter.text(rect.left_center() + egui::vec2(42.0, 0.0), egui::Align2::LEFT_CENTER, label, FontId::new(14.0, semibold()), color);
    }
    response.on_hover_cursor(egui::CursorIcon::PointingHand)
}

/// A rounded, tinted label for a state.
pub fn pill(ui: &mut egui::Ui, text: &str, color: Color32) {
    egui::Frame::none()
        .fill(color.gamma_multiply(0.14))
        .stroke(Stroke::new(1.0f32, color.gamma_multiply(0.35)))
        .rounding(20.0)
        .inner_margin(Margin::symmetric(10.0, 3.0))
        .show(ui, |ui| {
            ui.add(egui::Label::new(RichText::new(text).small().color(color)).wrap());
        });
}

pub fn primary(text: &str) -> egui::Button<'static> {
    egui::Button::new(RichText::new(text).family(semibold()).color(Color32::WHITE)).fill(ACCENT).stroke(Stroke::NONE)
        .min_size(egui::vec2(0.0, 32.0))
}

pub fn success(text: &str) -> egui::Button<'static> {
    egui::Button::new(RichText::new(text).family(semibold()).color(Color32::WHITE)).fill(GREEN).stroke(Stroke::NONE)
        .min_size(egui::vec2(0.0, 32.0))
}

pub fn secondary(text: &str) -> egui::Button<'static> {
    egui::Button::new(text).min_size(egui::vec2(0.0, 32.0))
}

pub fn danger(text: &str) -> egui::Button<'static> {
    egui::Button::new(RichText::new(text).color(ERR)).fill(ERR.gamma_multiply(0.08)).stroke(Stroke::new(1.0f32, ERR.gamma_multiply(0.4)))
}
