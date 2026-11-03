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
        ("처음에는 <b>간단 모드</b>의 공통 시간축을 사용합니다. 값을 바꾸면 해당 속성에만 점을 만듭니다. <b>고급 모드</b>에서 X·Y·크기·회전·투명도의 트랙과 구간 곡선을 따로 조절하세요. 모드를 전환해도 점과 베지어 곡선은 보존됩니다.",
         "Start with the shared timeline in <b>Simple mode</b>. Value edits key only changed properties. Use <b>Advanced mode</b> for independent X, Y, scale, rotation and opacity tracks and interval curves. Switching modes preserves keys and Bezier curves."), motion_art),
    GuidePage(("짧게 재생하고 안전하게 적용", "Preview, then apply"),
        ("<b>Space</b>로 재생, <b>K</b>로 자세 추가, <b>Ctrl+Z</b>로 되돌립니다. 시작 마지막 자세와 종료 첫 자세가 다르면 안내합니다. 원할 때만 <b>시작 애니메이션의 마지막 자세 가져오기</b>를 누르세요. <b>적용</b> 전에는 프로젝트가 바뀌지 않습니다.",
         "<b>Space</b> plays, <b>K</b> adds a pose and <b>Ctrl+Z</b> undoes. A notice flags differences between the entrance's final pose and the exit's first pose. Choose <b>Use entrance's final pose</b> to join them. The project changes only on <b>Apply</b>."), motion_art),
)
