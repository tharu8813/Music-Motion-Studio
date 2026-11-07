# 내보내기 CPU·GPU 병목 계측 (1단계)

실제 내보내기 성능을 최적화하기 전에 어디서 작업이 멈추는지 확인하는 **진단 전용 기능**이다. 영상 품질·FPS·인코딩 설정·렌더링 순서를 변경하지 않는다. 실행 중인 앱이나 영상 파일을 자동으로 수정하지 않는다.

## Windows에서 측정

먼저 개발 환경의 Python에 진단 전용 라이브러리를 설치한다.

```powershell
python -m pip install psutil
```

앱 소스 실행을 직접 측정할 때는 저장소 루트에서:

```powershell
python tools/export_bottleneck_profile.py --output build/export-bottleneck.json -- python main.py
```

설치된 실행 파일을 측정할 때는 마지막 명령을 해당 실행 파일 경로로 바꾼다.

```powershell
python tools/export_bottleneck_profile.py --output build/export-bottleneck.json -- "C:\Program Files\Music Motion Studio\Music Motion Studio.exe"
```

시작한 프로그램에서 프로젝트를 연 다음 평소대로 영상 내보내기를 실행한다. 내보내기가 끝나면 **앱을 종료**한다. 도구가 수집한 정보를 `build/export-bottleneck.json`에 저장한다. 앱은 정상 프로세스로 실행되며 취소·실패한 내보내기도 기록할 수 있다.

기존 실행 중인 앱을 재시작하지 않고 프로세스 샘플만 측정하려면 작업 관리자에서 PID를 확인하고:

```powershell
python tools/export_bottleneck_profile.py --pid 12345 --duration 120 --output build/attach-bottleneck.json
```

이미 시작된 앱에 연결해도 Python 내부의 함수별 계측을 **사후에 활성화할 수 없다**. 함수별 시간이 필요하면 위의 실행 모드를 사용한다. `--duration 120`은 측정 시간(초)이며 내보내기 시간을 강제로 제한하거나 취소하지 않는다.

## 측정 결과 해석

`summary`에는 다음을 기록한다.

- `observed_cpu_core_seconds`: 에디터, Canvas 자식 프로세스, FFmpeg, 기타 자식 프로세스의 관측 CPU 시간. 1초 동안 논리 코어 두 개를 전부 사용하면 약 2 CPU초로 측정된다.
- `peak_process_tree_rss_mib`: 에디터와 관측된 자식 프로세스 전체의 RSS 합계 최대치.
- `device_gpu_mean_percent` / `device_nvenc_mean_percent`: NVIDIA 장치 전체의 GPU 사용률 및 하드웨어 인코더 사용률. **이 앱만의 사용률이 아니다.**
- `stage_timings`: 앱에 이미 있는 `@timed` 계측값. 기존 `export.renderer_seconds`, `canvas.scene_render_seconds`, `export.frame_delivery_seconds`, `ffmpeg.process_wall_seconds` 등의 프레임 준비, CPU 렌더링, 전달, 인코딩 경계 시간이 포함된다. 병렬 Canvas 작업자는 `*.timings.worker-<PID>.json`에 별도 저장되며 읽을 때 합산한다.
- `samples`: 약 0.5초 간격의 CPU 코어 사용량·메모리 및 약 2초 간격의 GPU 장치 상태.

**주의:** 단계 시간은 중첩되거나 병렬 실행될 수 있는 포괄적인 *벽시계 시간*으로, 모두 더해서 실제 내보내기 시간으로 사용하면 안 된다. CPU 사용률과 단계별 시간, NVENC 장치 사용률을 같이 살펴 원인을 좁혀야 한다. GPU 커널 실행 시간·GPU 필터별 시간은 측정하지 않으며, GPU 활용도가 낮다는 사실만으로 CPU 병목을 확정하지 않는다.

## 내보내기 속도 분석 권장 순서

1. 동일 프로젝트, 동일 출력 옵션으로 2~3번 기록한다. Windows 전원 상태와 동시에 실행 중인 무거운 프로그램을 최대한 일정하게 맞춘다.
2. `canvas_workers` CPU 사용량과 `canvas.scene_render_seconds`를 확인한다. 렌더링 시간이 크면서 NVENC 사용률이 지속적으로 낮다면 Canvas 공급 병목 후보이다.
3. `ffmpeg` CPU 사용량, `ffmpeg.process_wall_seconds`, GPU 장치 인코더 사용률을 비교한다. 필터 합성에 CPU가 쓰이는 경우도 있으므로 FFmpeg CPU 사용이 곧 소프트웨어 영상 인코딩은 아니다.
4. `export.frame_queue_seconds`와 `export.frame_delivery_seconds`가 크면 출력 소비 대기 또는 픽셀 변환/파이프 전달 지연 가능성을 별도로 확인한다.
5. 실제 변경 전·후는 출력 프레임 수, 픽셀 해시, 오디오/타임스탬프, 취소 시 정리를 검증해야 한다.

관련 코드: `app/utils/performance.py`, `app/preview/canvas_process_stream.py`, `app/preview/export_session.py`, `app/renderer/canvas_pipe.py`. 진단 스크립트는 제품 실행 환경에 `psutil`을 추가로 요구하지 않는다.
