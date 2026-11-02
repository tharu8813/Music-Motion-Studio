"""First-use tour using the application's shared illustrated guide dialog."""
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPen
from app.dialogs.welcome_dialog import GuidePage
from app.ui.design_system import COLORS

MOTION_GUIDE_KEY = "guides/custom_motion_editor_seen"


def motion_art(painter, rect, korean):
    painter.fillRect(rect, QColor(COLORS["panel"]))
    stage = QRectF(rect.left()+14, rect.top()+14, rect.width()*.68, rect.height()*.56)
    painter.fillRect(stage, QColor(COLORS["stage"]))
    selected = QRectF(stage.center().x()-36, stage.center().y()-20, 72, 40)
    painter.setPen(QPen(QColor(COLORS["accent"]), 2))
    painter.drawRect(selected)
    for corner in (selected.topLeft(), selected.topRight(), selected.bottomLeft(), selected.bottomRight()):
        painter.drawRect(QRectF(corner.x()-3, corner.y()-3, 6, 6))
    painter.setPen(QColor(COLORS["text"]))
    painter.drawText(QRectF(stage.right()+12, stage.top(), rect.width()*.25, stage.height()),
        Qt.AlignmentFlag.AlignTop, "X  ━  0 px\nY  ━  0 px\n" + ("크기  ━  100%" if korean else "Scale  ━  100%"))
    for i, label in enumerate(("X", "Y", "크기" if korean else "Scale", "회전" if korean else "Rotate", "투명도" if korean else "Opacity")):
        y = stage.bottom()+20+i*16
        painter.setPen(QColor(COLORS["muted"]))
        painter.drawText(QPointF(stage.left(), y+4), label)
        left, right = stage.left()+64, rect.right()-14
        painter.drawLine(QPointF(left, y), QPointF(right, y))
        painter.setPen(QPen(QColor(COLORS["accent"]), 4))
        for t in (0, .3+i*.06, 1):
            x = left+(right-left)*t
            painter.drawLine(QPointF(x, y-4), QPointF(x, y+4))


MOTION_GUIDE = (
    GuidePage(("화면을 보며 모션 만들기", "Create motion on the canvas"),
        ("요소를 끌어 이동하고 핸들로 크기·회전을 조절하세요. <b>휠</b>로 확대·축소하고 빈 곳 또는 가운데 버튼을 드래그해 화면을 이동합니다. <b>Space+드래그</b>도 가능합니다.",
         "Drag the source or its resize/rotation handles. <b>Wheel</b> zooms; drag empty space or use the middle button to pan. <b>Space+drag</b> also pans."), motion_art),
    GuidePage(("속성별 시간과 곡선", "Independent timing and curves"),
        ("X·Y·크기·회전·투명도를 따로 편집합니다. 점 사이를 선택해 곡선·프리셋을 바꾸세요. 오른쪽 <b>슬라이더</b>와 숫자 입력은 함께 움직입니다. 트랙을 꺼도 키프레임은 남습니다.",
         "Edit X, Y, scale, rotation and opacity independently. Select an interval for its curve or preset. <b>Sliders</b> and numeric fields stay in sync. Disabling a track preserves its keys."), motion_art),
    GuidePage(("짧게 재생하고 안전하게 적용", "Preview, then apply"),
        ("<b>Space</b>로 재생·일시 정지, <b>K</b>로 점 추가, <b>Ctrl+Z</b>로 되돌립니다. 시작 효과 뒤/끝 효과 앞에는 효과 길이의 <b>절반</b>만 미리보기 전용 구간을 둡니다. <b>적용</b> 전에는 프로젝트가 바뀌지 않습니다. 상단 안내 버튼으로 다시 볼 수 있습니다.",
         "<b>Space</b> plays/pauses, <b>K</b> adds a key and <b>Ctrl+Z</b> undoes. Read-only context lasts <b>half</b> the effect: after entrance, before exit. The project changes only on <b>Apply</b>. Reopen this tour with the guide button."), motion_art),
)
