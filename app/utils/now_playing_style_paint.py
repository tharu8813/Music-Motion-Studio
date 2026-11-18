"""Shared now-playing card surfaces for the Canvas and the design gallery."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen


def paint_now_playing_surface(
    painter: QPainter, rect: QRectF, style: str,
    background: QColor, text: QColor, radius: float,
) -> QColor:
    """Draw the card and return the readable text color for this surface.

    For the original `card` design, preserve the brush/pen already supplied
    by the Canvas (including custom gradients and outlines).
    """
    card = QColor(background)
    foreground = QColor(text)
    if style == "minimal":
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(Qt.PenStyle.NoPen)
    elif style == "glass":
        if card.lightness() > 185:
            card = QColor("#1B2638")
            foreground = QColor("#F8FAFC")
        card.setAlpha(205)
        painter.setBrush(card)
        outline = QColor(foreground)
        outline.setAlpha(185)
        painter.setPen(QPen(outline, 1.5))
    elif style == "outline":
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(foreground, max(1.25, min(3.5, rect.height() * 0.018))))
    elif style == "accent":
        card.setAlpha(max(card.alpha(), 185))
        painter.setBrush(card)
        painter.setPen(Qt.PenStyle.NoPen)
    elif style == "gradient":
        if card.alpha() < 30:
            card = QColor("#203B45")
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0.0, card.lighter(145))
        gradient.setColorAt(1.0, card.darker(145))
        painter.setBrush(gradient)
        painter.setPen(Qt.PenStyle.NoPen)
    elif card.lightness() > 220 and foreground.lightness() > 190:
        foreground = QColor("#172033")

    painter.drawRoundedRect(rect, radius, radius)
    if style == "accent":
        stripe = QColor(foreground)
        stripe.setAlpha(max(180, stripe.alpha()))
        stripe_width = max(3.0, min(8.0, rect.width() * 0.018))
        inset = max(5.0, min(16.0, rect.height() * 0.10))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(stripe)
        painter.drawRoundedRect(
            QRectF(rect.left() + inset, rect.top() + inset,
                   stripe_width, max(0.0, rect.height() - inset * 2.0)),
            stripe_width / 2.0, stripe_width / 2.0,
        )
    return foreground
