# Rust GPU 내보내기 시각 요소 일치 검증

## 수정한 경로

- 반응형 배경, 현재 가사, 파티클을 실제 영상에서 확인하는 검증을 추가했다.
  기존의 최적화 A/B는 같은 Rust 경로끼리 비교했으므로 기존 FFmpeg 엔진과의
  시각적 일치를 별도로 확인해야 했다.
- FFmpeg와 Rust GPU가 파티클·시각화·파형·레벨 미터의 정의를 같은 함수에서
  만든다. 원본 좌표와 크기에 최종 출력 배율을 적용한 뒤 반올림한다.
  Rust의 중간 정수 반올림과 재확대 단계를 제거했다.
- 두 엔진이 같은 가사 공백 분석 절차를 사용한다. 분석이 끝난 트랙별 결과를
  첫 프레임 전에 확정해, 렌더링 도중 분석 완료 시점에 따라 가사 상태가 바뀌는
  경로를 수정했다.
- Canvas 레이어는 기존 엔진과 같은 프레임 캡처 스케줄 및 CFR 반복 규칙을
  사용한다. 가사 전환과 진입·퇴장 애니메이션을 서로 다른 시각에서 평가하던
  차이를 수정했다. 파티클과 오디오 시각화의 고정 프레임 시각은 공유한다.
- FFmpeg 합성의 기본 영상과 준비된 투명 레이어를 정수 프레임 번호로 맞춘다.
  마이크로초·밀리초 반올림으로 일부 프레임에서 직전의 가사와 시각화 레이어를
  합성하던 문제를 수정했다.
- 글자 외곽선·글자 그림자와 가사 행의 움직임을 작은 영역으로 자르지 않는다.
  320×180 재현 장면의 굵은 외곽선에서 잘리던 8,661개 픽셀을 확인했고,
  수정 후 전체 캔버스 캡처의 BGRA 바이트와 일치한다.

## 실제 프로젝트 검증

RTX 5060에서 `Big Bag new.pvsproj`의 첫 17초를 1280×720 30fps로 내보냈다.
현재 가사가 시작되는 12초 이후를 포함했다. 기존 FFmpeg 경로는 실제
`ExportSession`, `ExportCanvasCapturer`, `FFmpegRenderer.render`를 실행했다.
Rust 경로는 실제 `export_project_document`와 D3D11/NVENC를 실행했다.

두 영상의 510프레임을 모두 디코딩해 비교했다. FFmpeg libx264 CRF12와
Rust NVENC 사이의 RGB 평균 절대 차이는 0~255 범위에서 0.951이었고,
프레임별 평균의 최댓값은 1.058이었다. 동일한 Rust 영상과 보정 전 FFmpeg
영상을 비교했을 때 프레임별 평균의 최댓값은 5.879였다. 이 측정에는
인코더와 색 변환 차이도 포함된다.

같은 프로젝트에서 반응형 배경, 현재 가사의 음악 반응, 파티클을 각각 끈
Rust 영상도 생성했다. 배경 반응은 2초, 파티클은 2초, 현재 가사 반응은
15초 프레임에서 활성화한 영상과의 픽셀 차이를 확인했다.

## 회귀 검증

실제 오디오에서 만든 프레임 15개를 기존 `ExportCanvasCapturer`와 Rust의
프로덕션 프레임 생성기로 각각 그린다. 음악 반응 배경과 현재 가사가 포함된
기본 레이어의 BGRA 바이트 및 파티클 레이어의 BGRA 바이트가 일치해야 한다.
분수 좌표의 출력 배율, 비동기 가사 공백 분석 대기, 외곽선의 크롭 방지와
FFmpeg 30fps 레이어 합성의 프레임 순서도 검사한다.

```powershell
.\.venv\Scripts\python.exe -m unittest -v tests.test_native_gpu_full_export tests.test_native_layer_cache tests.test_native_dynamic_motion tests.test_native_project_runtime tests.test_export_session tests.test_lyrics_intro tests.test_text_music_reaction tests.test_ffmpeg_cfr_overlay_fastpath
```
