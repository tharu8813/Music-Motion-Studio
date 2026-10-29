"""Shared dark studio palette, typography and application chrome."""

from pathlib import Path
import os
import sys

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

from app.utils.font_loader import load_application_font
from app.ui.studio_icons import StudioIconStyle


COLORS = {
    "window": "#161819", "panel": "#1E2022", "field": "#191B1D",
    "button": "#292C2F", "hover": "#353A3E", "text": "#ECEEED",
    "muted": "#ADB3B5", "border": "#3C4246", "disabled": "#242729",
    # Distinct from explanatory text: disabled controls keep their values visible.
    "disabled_text": "#798184",
    "alternate": "#25282A", "shadow": "#101213", "accent": "#B4CDC3",
    "accent_hover": "#CEE0D8", "selection": "#30423C",
    "selection_border": "#718D82", "stage": "#121415",
    "warning": "#DEC18D", "warning_surface": "#352F25",
    "danger": "#E5A19B", "danger_surface": "#36272A",
    "success": "#B4CDC3", "info_surface": "#25312D",
}


def studio_stylesheet() -> str:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    sheet = (root / "app/ui/studio.qss").read_text(encoding="utf-8")
    for name, value in sorted(COLORS.items(), key=lambda item: -len(item[0])):
        sheet = sheet.replace(f"@{name}", value)
    icon_root = root / "assets/icons" if getattr(sys, "frozen", False) else root / "app/assets/icons"
    return sheet.replace("@spin_up_disabled", (icon_root / "spin_up_disabled.svg").as_posix()).replace(
        "@spin_down_disabled", (icon_root / "spin_down_disabled.svg").as_posix(),
    ).replace("@spin_up", (icon_root / "spin_up.svg").as_posix()).replace(
        "@spin_down", (icon_root / "spin_down.svg").as_posix(),
    ).replace("@check", (icon_root / "check.svg").as_posix())


def apply_studio_style(application: QApplication | None) -> None:
    """Install once, including dialogs created before the editor is visible."""
    if application is None or application.property("playlistCanvasStudioStyle"):
        return
    application.setStyle(StudioIconStyle("Fusion"))
    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    load_application_font(fonts / "segoeui.ttf")
    load_application_font(fonts / "seguisb.ttf")
    load_application_font(fonts / "seguisym.ttf")
    load_application_font(fonts / "malgun.ttf")
    load_application_font(fonts / "malgunbd.ttf")
    font = QFont()
    font.setFamilies(["Segoe UI", "Malgun Gothic", "Noto Sans CJK KR", "sans-serif"])
    font.setPointSizeF(9.75)
    application.setFont(font)
    palette = QPalette()
    roles = {
        "Window": "window", "WindowText": "text", "Base": "field",
        "AlternateBase": "alternate", "ToolTipBase": "panel", "ToolTipText": "text",
        "Text": "text", "Button": "button", "ButtonText": "text",
        "Highlight": "accent", "HighlightedText": "window", "Link": "accent",
        "PlaceholderText": "muted", "BrightText": "text",
        "Mid": "border", "Midlight": "hover", "Dark": "shadow",
        "Light": "hover", "Shadow": "shadow",
    }
    for role, color in roles.items():
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(COLORS[color]))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(COLORS["disabled_text"]))
    for role in (QPalette.ColorRole.Base, QPalette.ColorRole.Button):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(COLORS["disabled"]))
    application.setPalette(palette)
    application.setStyleSheet(studio_stylesheet())
    application.setProperty("playlistCanvasEffectiveTheme", "dark")
    application.setProperty("playlistCanvasStudioStyle", True)
