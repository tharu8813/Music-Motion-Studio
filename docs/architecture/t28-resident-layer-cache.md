# t28: production resident layer cache

현재 구현은 기존 Qt 픽셀 준비와 D3D11 합성/NVENC 경로를 유지하면서 불변 레이어의 반복 작업을 줄인다. **Rust GPU 렌더링 엔진 완성 또는 전체 내보내기 가속을 입증한 결과가 아니다.**

## 연결

- `native_layer_cache.py`: 기존 `CanvasSnapshot.split_mixed_capture_bands`와 `invariant_stream_keys`를 재사용한다. 동일 Z의 소스는 분리하지 않으며, 오디오 동적 스트림을 포함한 총 8개 제한을 지킨다.
- `produce_complete_project_frames`는 분리된 밴드와 export 전용 `NativeRasterCache`를 사용한다. `capture_native_canvas_track_frame`는 불변 밴드만 처음 한 번 CPU raster/straight-BGRA 변환하고 재사용한다. 가변 밴드는 기존 track/lyrics/keyframe 상태 해석과 painter를 매번 호출한다.
- `resident_layer_cache.rs`: stream key와 픽셀 SHA-256으로 GPU texture를 유지한다. 픽셀이 바뀌면 해당 texture만 업로드하고, opacity/Z 순서는 현재 프레임 값을 사용한다. 사라진 key는 제거한다.
- `d3d11_qt_canvas_stream_export`의 실제 production upload 블록이 이 cache를 사용한다. `CanvasPacketFrame` 또는 transport 필드는 변경하지 않는다. t27 binary transport 병합 시 이 블록과 보고서 필드를 유지해야 한다.

## 경계와 수명

CPU raster cache는 외부 편집과 분리된 project 복사본의 단일 export에만 존재한다. source/scale/프로젝트가 다른 export 사이에 공유하지 않는다. 취소·예외·iterator close에서도 `finally`가 raster cache와 Qt scene을 해제한다. Rust cache는 최대 8개 입력 texture만 유지하고, 완료 시 `clear`, 실패/취소 시 프로세스의 소유권 해제로 자원이 회수된다. 최대 입력 texture payload는 `8 * 3840 * 2160 * 4 = 265420800` bytes다. 이는 NV12 pool, video processor 중간 출력, 드라이버/NVENC 내부 메모리를 포함한 전체 VRAM 상한이 아니다.

합성과 dirty upload는 같은 D3D11 immediate context에 순서대로 제출한다. 이전 합성 후 NV12 pool slot으로 복사하므로 NVENC가 진행 중이어도 다음 입력 갱신은 이전 합성 뒤에 실행된다. 실제 encode 완료는 기존 pool/NVENC completion 계약을 유지한다.

## 측정과 재현

개발 prerequisite: 해당 Python 환경에서 `scripts/setup_rust_native.py` 실행 후 `scripts/rust_prerequisites.py` 통과. 이 작업에서는 Python 3.14의 작업 전용 가상환경과 Rust extension 0.3.0을 사용했다.

실제 production 경로 smoke:

```powershell
cargo build --manifest-path native/gpu_export_core/Cargo.toml --bin d3d11_qt_canvas_stream_export
.venv314/Scripts/python.exe -m tools.native_resident_cache_probe --executable native/gpu_export_core/target/debug/d3d11_qt_canvas_stream_export.exe --output-dir work/t28-resident-production-01
```

새 폴더만 허용하며 기존 사용자 파일을 덮어쓰지 않는다. 320x180, 60 FPS, 1초, 정적 배경/회전·반투명 텍스트/동적 progress의 3밴드 실제 Qt→D3D11→NVENC 실행 결과:

| 항목 | 기존 매 프레임 경로 계산값 | cache 실제 카운터 |
| --- | ---: | ---: |
| CPU 밴드 raster | 180 | 62 |
| GPU upload | 180 | 62 |
| 입력 texture allocation | 180 | 3 |
| CPU/GPU cache hit | 0 | 118 |

입력 resident bytes 691200, texture release 3. stream 단독 소요 시간 3.405초는 debug build/짧은 synthetic 프로젝트 결과이며 기존 엔진 전체 A/B 속도와 비교할 수 없다. 오디오 준비/최종 mux를 포함하지 않는다.

Rust GPU 비교 테스트는 128x128의 60프레임에서 새 upload 결과와 resident 결과의 NV12 전체 bytes를 비교했다. dirty pixel, opacity-only 변경, Z reorder를 포함해 일치했고 upload 180→4, allocation 180→3이었다. Qt 비교는 정적 회전/alpha/클립을 포함한 두 밴드 60프레임의 기존 결과와 byte-identical이며 raster 120→2였다.

검증: 관련 Python 56 tests 통과(기존 hardware opt-in 1 skip); Rust lib 29 tests 및 stream binary 3 tests 통과 후 자원 소유권 테스트 1개 추가 통과(cache 모듈 총 5개). 소유권 테스트는 stream key를 반복 교체하고 upload 실패 및 owner drop을 발생시켜 실제 살아 있는 자원 개수가 8 이하이며 마지막에 0인지 확인한다.

## 미완성 범위

- 임의 scale/rotation/clip/효과는 기존 CPU Qt raster에서 픽셀을 만들며, 정적 결과를 재사용한다. GPU에서 수행되는 것은 기존 planar opacity/alpha 합성, 색 변환, NVENC다. **일반 affine GPU transform은 추가 구현 및 production 동등성 검증이 필요하다.**
- 동적 visualizer/particle/텍스트/lyrics 픽셀 생성도 CPU에 남는다. cache miss마다 전체 밴드를 업로드한다. 부분 사각형 dirty upload는 구현하지 않았다.
- transport는 기존 full pixels 계약이다. resident hit여도 payload와 SHA-256 CPU 검사는 남고, 현 cache는 packet loader 검사와 별도로 SHA-256을 계산한다. 더 큰 해상도에서 이 비용이 전체 속도에 미치는 영향을 측정해야 한다.
- 자동 분리로 입력 stream 수가 늘 수 있으며, 8개라는 packet 한도와 별도로 실제 adapter의 video processor stream capability 검사가 적용된다.
- 10분 이상 전체 프로젝트/오디오/mux A/B와 장시간 전체 VRAM 측정은 t32 검증 전까지 미완료다. 이 cache의 카운터 감소만으로 이전 엔진보다 빠르다고 결론내리지 않는다.
