"""v2 trust boundary plus optional real D3D11 readback against the retained Qt painter."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
import numpy as np
from PySide6.QtGui import QImage, QColor, QPainter, QTransform
from PySide6.QtWidgets import QApplication
from app.preview.native_canvas_layer_packets import NativeCanvasBgraLayer, NativeCanvasFrame, save_native_canvas_frame, straight_bgra_bytes
from app.renderer.native_dynamic_motion import dynamic_layer_frame, dynamic_gpu_layer
from app.renderer.native_frame_transport import frame_chunks, HEADER
from app.models.source import Source, SourceType
from app.animation.keyframes import Keyframe
from app.renderer.native_project_overlays import project_visualizers
from tests.test_native_canvas_project_probe import project_fixture

PROBE = os.environ.get("PLAYLIST_CANVAS_AFFINE_PROBE")

class AffineContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def base(self, width=256, height=192):
        return NativeCanvasBgraLayer("base", width, height, bytes([8,16,32,255]) * width * height)

    def test_v1_rejects_nonidentity_and_v2_preserves_odd_source_size_and_envelope(self):
        layer = NativeCanvasBgraLayer("layer:0", 3, 5, bytes([1,2,3,128])*15,
                                     .5, (1,0,0,1,-10,-11), (0,0,256,192))
        with self.assertRaises(ValueError):
            NativeCanvasFrame(256,192,(self.base(),layer))
        packet = NativeCanvasFrame(256,192,(self.base(),layer),2)
        chunks = list(frame_chunks(packet,7))
        magic,version,pts,meta,size = HEADER.unpack(chunks[0])
        self.assertEqual((magic,version,pts,size),(b"MMSB",1,7,256*192*4+60))
        data = json.loads(chunks[1])
        self.assertEqual((data["schema"],data["format"]),(2,"mms_canvas_bgra_layers_v2"))
        self.assertEqual(data["layers"][1]["inverse_transform"],[1,0,0,1,-10,-11])
        self.assertEqual(data["layers"][1]["width"],3)
        self.assertEqual(meta,len(chunks[1]))

    def test_invalid_transform_clip_and_base_are_rejected(self):
        for m in ((0,)*6,(1,0,0,1,float("nan"),0),(1,0,0,1,1e20,0)):
            with self.assertRaises(ValueError):
                NativeCanvasBgraLayer("layer:0",2,2,bytes(16),inverse_transform=m)
        for clip in ((2,0,1,2),(0,0,257,192),(0.5,0,2,2)):
            with self.assertRaises(ValueError):
                layer=NativeCanvasBgraLayer("layer:0",2,2,bytes(16),clip=clip)
                NativeCanvasFrame(256,192,(self.base(),layer),2)
        with self.assertRaises(ValueError):
            NativeCanvasFrame(256,192,(NativeCanvasBgraLayer("base",2,2,bytes([0,0,0,255])*4),),2)

    @unittest.skipUnless(PROBE, "Set PLAYLIST_CANVAS_AFFINE_PROBE for real D3D11 readback")
    def test_compact_integer_roi_gpu_copy_is_byte_exact_in_bgra_and_nv12(self):
        # The copy path cannot degrade a glyph: a cropped plane is moved
        # directly to its integer pixel offset without texture sampling.
        small_image = QImage(37, 15, QImage.Format.Format_ARGB32)
        small_image.fill(0)
        painter = QPainter(small_image)
        painter.fillRect(4, 3, 24, 9, QColor(244, 102, 23, 137))
        painter.fillRect(9, 6, 18, 7, QColor(18, 178, 245, 204))
        painter.end()
        local_bytes = straight_bgra_bytes(small_image)
        width, height, x, y = 256, 192, 21, 31
        full_bytes = bytearray(width * height * 4)
        for row in range(small_image.height()):
            dst = ((y + row) * width + x) * 4
            src = row * small_image.width() * 4
            full_bytes[dst:dst + small_image.width() * 4] = local_bytes[
                src:src + small_image.width() * 4
            ]
        full = NativeCanvasBgraLayer(
            "layer:0", width, height, bytes(full_bytes),
        )
        cropped = NativeCanvasBgraLayer(
            "layer:0", small_image.width(), small_image.height(), local_bytes,
            1.0, (1, 0, 0, 1, -x, -y),
            (x, y, x + small_image.width(), y + small_image.height()),
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            original = save_native_canvas_frame(
                NativeCanvasFrame(width, height, (self.base(), full)),
                root, stem="expanded",
            )
            compact = save_native_canvas_frame(
                NativeCanvasFrame(width, height, (self.base(), cropped), 2),
                root, stem="compact",
            )
            for label, manifest in (("expanded", original), ("compact", compact)):
                pixels_path = root / (label + ".bgra")
                proc = subprocess.run(
                    [PROBE, str(manifest), "1", str(pixels_path)],
                    capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(pixels_path.read_bytes(), bytes(full_bytes))
            results = []
            for label, manifest in (("expanded", original), ("compact", compact)):
                path = root / (label + ".nv12")
                proc = subprocess.run(
                    [PROBE, str(manifest), "1", str(path), "--nv12"],
                    capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                results.append(path.read_bytes())
            self.assertEqual(results[0], results[1])

    @unittest.skipUnless(PROBE, "Set PLAYLIST_CANVAS_AFFINE_PROBE for real D3D11 readback")
    def test_real_gpu_transform_matches_qt_rotation_scale_opacity_seek_and_half_open_clip(self):
        project=project_fixture()
        source=Source(SourceType.AUDIO_VISUALIZER,"gpu parity",x=90,y=80,width=48,height=24,scale=1.5,opacity=.65,z_index=9)
        source.keyframes.global_curves={name:[Keyframe(0,start),Keyframe(2,end)]
            for name,start,end in (("x",70,140),("y",65,110),("rotation",-25,75),("scale",1.5,2.25),("opacity",.2,.85))}
        project.sources.append(source)
        overlay=project_visualizers(project,project.playlist)[0]
        metrics=[]
        with TemporaryDirectory() as directory:
            root=Path(directory)
            for scale in (1.,.5):
                image=QImage(round(overlay.width*scale),round(overlay.height*scale),QImage.Format.Format_RGBA8888)
                image.fill(QColor(220,140,80,128))
                for t in (0,.3,.7,1.1,1.7,1.9):
                    reference=dynamic_layer_frame(image,overlay,256,192,seconds=t,output_scale=scale)
                    local=dynamic_gpu_layer(image,overlay,256,192,seconds=t,output_scale=scale)
                    local=NativeCanvasBgraLayer("layer:0",local.width,local.height,local.pixels,local.opacity,local.inverse_transform,local.clip)
                    frame=NativeCanvasFrame(256,192,(self.base(),local),2)
                    case=f"s{scale}-t{t}"
                    manifest=save_native_canvas_frame(frame,root,stem=case.replace('.','_'))
                    output=root/(case+".bgra")
                    result=subprocess.run([PROBE,str(manifest),"1",str(output)],capture_output=True,text=True,timeout=30)
                    self.assertEqual(result.returncode,0,result.stderr)
                    self.assertGreater(json.loads(result.stdout)["shader_draws"],0)
                    gpu=np.frombuffer(output.read_bytes(),np.uint8).reshape(192,256,4).astype(float)
                    cpu=np.frombuffer(reference.pixels,np.uint8).reshape(192,256,4).astype(float)
                    actual=gpu[:,:,:3]*gpu[:,:,3:4]/255*local.opacity
                    expected=cpu[:,:,:3]*cpu[:,:,3:4]/255
                    mask=(actual.max(axis=2)>1)|(expected.max(axis=2)>1)
                    error=float(np.abs(actual-expected)[mask].mean())
                    self.assertLess(error,6.,case)
                    for channel in (actual,expected): self.assertGreater(float(channel.max()),1.)
                    for axis in (0,1):
                        a=np.argwhere(actual[:,:,0]>actual[:,:,0].max()*.2)[:,axis]
                        b=np.argwhere(expected[:,:,0]>expected[:,:,0].max()*.2)[:,axis]
                        self.assertAlmostEqual(float(a.mean()),float(b.mean()),delta=1.5,msg=case)
                    # Compare the actual alpha compositor + NV12 surface too,
                    # independent of lossy H.264 encoding and decoder colour policy.
                    cpu_layer=NativeCanvasBgraLayer("layer:0",256,192,reference.pixels)
                    cpu_manifest=save_native_canvas_frame(NativeCanvasFrame(256,192,(self.base(),cpu_layer)),root,
                        stem=(case+"-cpu").replace('.','_'))
                    nv12=[]
                    for label,path in (("gpu",manifest),("cpu",cpu_manifest)):
                        raw=root/(case+label+".nv12")
                        run=subprocess.run([PROBE,str(path),"1",str(raw),"--nv12"],capture_output=True,text=True,timeout=30)
                        self.assertEqual(run.returncode,0,run.stderr)
                        nv12.append(np.frombuffer(raw.read_bytes(),np.uint8).astype(float))
                    nv12_error=float(np.abs(nv12[0]-nv12[1]).mean())
                    self.assertLess(nv12_error,1.5,case)
                    metrics.append({"case":case,"mean_premultiplied_bgr_error":error,"mean_nv12_error":nv12_error})
            # Exact pixel-centre translation and a half-open clip oracle.
            image=QImage(9,7,QImage.Format.Format_RGBA8888); image.fill(QColor(255,0,0,128))
            layer=NativeCanvasBgraLayer("layer:0",9,7,bytes([0,0,255,128])*63,1,(1,0,0,1,-20,-30),(21,31,27,35))
            manifest=save_native_canvas_frame(NativeCanvasFrame(256,192,(self.base(),layer),2),root,stem="clip")
            output=root/"clip.bgra"
            result=subprocess.run([PROBE,str(manifest),"1",str(output)],capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            pixels=np.frombuffer(output.read_bytes(),np.uint8).reshape(192,256,4)
            self.assertTrue(np.all(pixels[31:35,21:27]==[0,0,255,128]))
            self.assertEqual(int(np.count_nonzero(pixels[:,:,3])),24)
        evidence=os.environ.get("PLAYLIST_CANVAS_AFFINE_EVIDENCE")
        if evidence:
            with Path(evidence).open("x",encoding="utf-8") as f:
                json.dump({"status":"pass","dynamic_cases":metrics,"half_open_clip_exact":True},f,indent=2)

    @unittest.skipUnless(PROBE, "Set PLAYLIST_CANVAS_AFFINE_PROBE for real D3D11 readback")
    def test_real_gpu_shear_reflection_and_transparent_edge_colours(self):
        image=QImage(47,29,QImage.Format.Format_ARGB32)
        image.fill(0)
        painter=QPainter(image)
        painter.fillRect(2,3,19,17,QColor(240,40,10,180))
        painter.fillRect(21,9,23,18,QColor(20,200,230,95))
        painter.end()
        with TemporaryDirectory() as directory:
            root=Path(directory)
            for index,forward in enumerate((QTransform(1.2,.25,.35,.9,25,30),QTransform(-1.1,.1,.2,.95,100,40))):
                inverse,valid=forward.inverted(); self.assertTrue(valid)
                reference=QImage(256,192,QImage.Format.Format_ARGB32);reference.fill(0)
                painter=QPainter(reference);painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                painter.setTransform(forward);painter.drawImage(0,0,image);painter.end()
                layer=NativeCanvasBgraLayer("layer:0",47,29,straight_bgra_bytes(image),1,
                    (inverse.m11(),inverse.m12(),inverse.m21(),inverse.m22(),inverse.dx(),inverse.dy()))
                manifest=save_native_canvas_frame(NativeCanvasFrame(256,192,(self.base(),layer),2),root,stem=f"shear-{index}")
                output=root/f"shear-{index}.bgra"
                run=subprocess.run([PROBE,str(manifest),"1",str(output)],capture_output=True,text=True,timeout=30)
                self.assertEqual(run.returncode,0,run.stderr)
                gpu=np.frombuffer(output.read_bytes(),np.uint8).reshape(192,256,4).astype(float)
                cpu=np.frombuffer(straight_bgra_bytes(reference),np.uint8).reshape(192,256,4).astype(float)
                a=gpu[:,:,:3]*gpu[:,:,3:4]/255;b=cpu[:,:,:3]*cpu[:,:,3:4]/255
                mask=(a.max(axis=2)>1)|(b.max(axis=2)>1)
                self.assertLess(float(np.abs(a-b)[mask].mean()),6.)

    def test_frozen_cli_route_does_not_start_editor_or_migrate_settings(self):
        from unittest.mock import patch
        import main
        with patch("sys.argv",["studio.exe","--native-gpu-export","--project","clip.pvsproj"]), \
             patch("tools.native_gpu_full_export.main",return_value=7) as export, \
             patch("main.configure_logging") as configure:
            self.assertEqual(main.main(),7)
            export.assert_called_once_with(["--project","clip.pvsproj"])
            configure.assert_not_called()

    def test_real_export_rejects_empty_font_database_before_audio_or_encoding(self):
        from unittest.mock import patch
        from app.renderer.native_project_export import _require_developer_runtime
        from app.renderer.ffmpeg_renderer import RenderError
        with patch("scripts.rust_prerequisites.require_developer_environment"), \
             patch("PySide6.QtGui.QFontDatabase.families",return_value=[]):
            with self.assertRaisesRegex(RenderError,"Qt has no fonts"):
                _require_developer_runtime()

    def test_missing_application_is_an_error_before_querying_qt_fonts(self):
        from unittest.mock import patch
        from app.renderer.native_project_export import _require_developer_runtime
        from app.renderer.ffmpeg_renderer import RenderError
        with patch("scripts.rust_prerequisites.require_developer_environment"), \
             patch("PySide6.QtWidgets.QApplication.instance",return_value=None), \
             patch("PySide6.QtGui.QFontDatabase.families") as fonts:
            with self.assertRaisesRegex(RenderError,"QApplication"):
                _require_developer_runtime()
            fonts.assert_not_called()
