"""Shared form spacing and actions for the studio's standard dialogs.

Keep these layout-only helpers separate from input validation and command wiring.
They must never connect signals or change button roles.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialogButtonBox, QFormLayout


def style_form(form: QFormLayout) -> None:
    """Give editable forms the same compact labels, fields and row rhythm."""
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    form.setHorizontalSpacing(18)
    form.setVerticalSpacing(10)


def style_dialog_actions(buttons: QDialogButtonBox) -> None:
    """Standardize footer spacing without changing accept/reject semantics."""
    buttons.setObjectName("dialogActions")
    layout = buttons.layout()
    if layout is not None:
        layout.setSpacing(8)
