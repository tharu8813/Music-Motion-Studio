# Native Canvas v2 변환 계약

2026-11-10 구현. 전송 envelope의 MMSB binary-v1(28 byte header)은 유지한다. metadata schema와 binary envelope 버전은 서로 다르다. 기존 schema 1 manifest와 옵션 생략 manifest stream CLI도 유지한다.

## 메타데이터와 좌표

- `schema=2`, `format=mms_canvas_bgra_layers_v2`. BGRA8_STRAIGHT, top_left, 기존 SHA-256·bytes·stream key·Z·opacity 검증을 유지한다.
- 출력 width/height는 기존 짝수 2..3840 × 2..2160이다. production 지원 최소 64와 FPS/scale 조건은 별도로 유지한다.
- 각 layer의 선택적 `width`, `height`는 소스 plane 크기이다. 생략하면 출력 크기이며, 양의 정수 1..3840 × 1..2160을 허용한다. 홀수 소스도 허용한다. bytes는 source_width × source_height × 4이며 frame payload 총 한도는 기존 265,420,800 bytes이다.
- `inverse_transform=[a,b,c,d,tx,ty]`: 출력 픽셀 중심 `(x+0.5,y+0.5)`에서 소스 픽셀 중심 좌표로 `sx=a*x+c*y+tx`, `sy=b*x+d*y+ty`를 적용한다. 식의 x/y는 이미 중심 좌표이며 추가 0.5를 더하지 않는다. 생략하면 identity이다. 값은 finite, 절댓값 ≤1e6이며 determinant 절댓값 ≥1e-12여야 한다. Rust는 f32로 변환한 뒤 determinant를 검사한다.
- `clip=[left,top,right,bottom]`: 출력 좌표계의 정수 half-open 사각형이다. `0≤left≤right≤width`, `0≤top≤bottom≤height`. 생략하면 full output이며 빈 clip은 투명 출력이다. x/y/width/height 형식이 아니다.
- 첫 base layer는 출력 크기·identity/full clip·opacity 1·모든 픽셀 alpha 255여야 한다. schema 1의 모든 layer도 full-frame identity여야 한다. v1에 비정상 변환을 넣어 조용히 무시하지 않는다.

## 샘플링과 GPU 수명

D3D11 pixel shader는 입력 straight RGB를 premultiplied alpha로 바꿔 bilinear interpolation한 후 straight로 돌린다. 소스 밖의 texel은 투명이며 clip 밖도 투명이다. layer opacity는 기존 video processor의 planar alpha로 한 번 적용한다. 변환 출력 BGRA → 기존 GPU 합성/NV12 → 동일 장치 NVENC 흐름이며 production readback은 없다.

입력 resident cache는 stream key·source dimensions·픽셀 SHA-256을 기준으로 한다. matrix/clip/opacity/Z 변화는 픽셀 재업로드를 요구하지 않는다. 출력 변환 texture는 변경된 화면의 layer별로 재사용하고 사라진 key는 정리한다. 입력 최대 8개와 출력 scratch 최대 8개의 payload 카운터를 각각 보고한다. 이것은 NV12 pool·driver·encoder 메모리를 포함한 전체 VRAM 상한이 아니다.

production dynamic audio layer는 기존 PythonVisualizerRenderer의 원본 이미지를 그대로 보내고 기존 motion binding의 절대 timestamp를 matrix로 변환한다. 정적 Canvas/텍스트/가사/effects와 동적 이미지 생성은 여전히 CPU다. `dynamic_layer_frame` Qt 경로는 독립 픽셀 비교 기준으로 유지한다. VIDEO 및 crossfade/AutoMix는 기존 preflight·전체 FFmpeg 대체 경로를 유지한다.

## 검증과 진단

`d3d11_affine_validate MANIFEST LAYER NEW_OUTPUT.bgra`는 명시적 진단 readback 전용이다. production worker에 호출되지 않으며, 출력이 있으면 거절한다. 기존 packet probe는 full-frame identity 외 입력을 거절한다.

`tests/test_native_affine_contract.py`는 v1 거절·source 크기·MMSB envelope·잘못된 matrix/clip/base를 검사한다. `PLAYLIST_CANVAS_AFFINE_PROBE`에 freshly built validator를 지정하면 실제 D3D11 readback을 보존된 Qt painter와 비교한다. 회전/scale/opacity/절대 timestamp/출력 scale, 정확한 clip, shear/reflection/서로 다른 색과 alpha를 포함한다. Qt와 shader의 resampler 차이가 있으므로 bitwise 일치로 표현하지 않는다. 픽셀 검증 결과는 전체 엔진 속도 향상 근거가 아니다.

`RenderResult.native_report`는 임시 `complete.json` 정리 후에도 native report·raster 카운터·audio 준비 시간·준비부터 mux/검증 완료까지의 wall 시간을 보존한다. 개발 CLI의 `--report NEW.json`으로 명시적으로 저장한다. 사용자 UI에 내부 구현 카운터를 노출하지 않는다.
