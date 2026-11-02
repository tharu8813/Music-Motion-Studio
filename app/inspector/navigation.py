"""Stable inspector destinations, independent of source-specific field sections."""

CATEGORY_OWNER = {"text": "special", "fill": "shape", "filter": "shape"}
CATEGORY_LABELS = {
    "special": ("콘텐츠", "Content"),
    "layout": ("배치 및 변형", "Transform"),
    "shape": ("디자인", "Design"),
    "animation": ("애니메이션", "Animation"),
    "other": ("고급", "Advanced"),
}


def consolidate_pages(inspector):
    """Keep type editors' forms while grouping their destinations by responsibility."""
    for page, destination in ((inspector.text_group, inspector.content_group),
                              (inspector.fill_group, inspector.appearance_group),
                              (inspector.filter_group, inspector.appearance_group)):
        layout = destination.layout()
        layout.insertWidget(layout.count() - 1, page)
