"""Export a real Qt SourceItem Canvas sample as native D3D11 BGRA layers.

This is a diagnostic fixture for the native GPU layer compositor, not a
production project renderer. It paints actual Qt shape/text SourceItems
through CanvasSnapshot, in separate opaque/transparent Z-order bands.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from PySide6.QtWidgets import QApplication

from app.canvas.live_canvas import CanvasScene
from app.canvas.source_item import SourceItem
from app.models.source import Source, SourceType
from app.preview.native_canvas_layer_packets import (
    capture_native_canvas_frame, save_native_canvas_frame,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--stem", default="qt-canvas-layers")
    parser.add_argument("--scale", type=float, default=0.5,
                        help="Artboard scale; 0.5 -> 640x360, 1.0 -> 1280x720")
    args = parser.parse_args(argv)

    app = QApplication.instance() or QApplication([])
    scene = CanvasScene()
    scene.addItem(SourceItem(Source(
        SourceType.SHAPE, "Qt Canvas background",
        x=0, y=0, width=1280, height=720, z_index=0,
        fill_color="#192C43",
    )))
    scene.addItem(SourceItem(Source(
        SourceType.SHAPE, "Lyric panel",
        x=140, y=270, width=1000, height=220,
        z_index=11, fill_color="#496FA3", opacity=0.32,
    )))
    scene.addItem(SourceItem(Source(
        SourceType.TEXT, "Actual Qt text/lyric painter",
        x=165, y=318, width=950, height=132, z_index=12,
        text="Canvas Lyrics · GPU Composition",
        font_size=58, fill_color="#FFE8AF", text_stroke_width=1.0,
    )))
    try:
        layers = capture_native_canvas_frame(
            scene, [(None, 5), (6, None)], output_scale=args.scale,
        )
        manifest_path = save_native_canvas_frame(
            layers, args.output_dir, stem=args.stem,
        )
        print(json.dumps({
            "status": "captured",
            "manifest": str(manifest_path),
            "resolution": [layers.width, layers.height],
            "layers": len(layers.layers),
            "qt_cpu_rasterized": True,
            "native_gpu_canvas_painting_implemented": False,
            "production_export_changed": False,
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        scene.clear()
        # Do not tear down an application owned by a hosting editor.
        _ = app


if __name__ == "__main__":
    raise SystemExit(main())
