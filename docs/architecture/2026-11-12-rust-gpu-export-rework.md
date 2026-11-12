# Rust GPU 내보내기 병목 조사

## CapCut 4.0.0.1539 설치본

REA의 PE import 분석에서 `VECreator.dll`은 `videoeditor.dll`과 DXGI를 참조했다.
`ttvideoeditor.dll`은 `D3D11CreateDevice`, `DXVA2CreateVideoService`,
`avcodec_send_packet` / `avcodec_receive_frame`, `avcodec_send_frame` /
`avcodec_receive_packet`, `avcodec_get_hw_config`를 참조했다. 같은 DLL에
`av_hwdevice_ctx_create`, `av_hwframe_get_buffer`, `av_hwframe_transfer_data`
참조도 있다. `avcodec-58.dll`은 Media Foundation API를 참조한다.
동봉된 `ffmpeg.exe`의 `-hwaccels`, `-decoders`, `-encoders` 출력에서 CUDA,
DXVA2, QSV, D3D11VA, H.264/HEVC CUVID 디코더 및 NVENC 인코더를 확인했다.
같은 PC에서 이미 렌더링된 1080p60 H.264 파일 1,800프레임을 동봉 FFmpeg의
`h264_cuvid` → CUDA 프레임 → `h264_nvenc`로 처리하자 2.933초가 걸렸다.
이 측정은 오디오, 효과 합성, 파일 쓰기가 없는 트랜스코딩이다. 초기 자동
CUDA 디코딩 시도에서는 표면을 37개 요청해 실패했고, 디코더와 스레드 수를
명시한 뒤 CUDA 프레임 경로가 성공했다.

이는 설치본에 하드웨어 코덱과 GPU 프레임을 다루는 경로가 있음을 보여준다.
어떤 경로가 내보내기에 선택되는지, GPU 텍스처가 디코더에서 인코더까지
유지되는지, NVENC를 실제 사용하는지는 동일 작업을 실행해 추적해야 한다.
설치본의 코드와 셰이더는 사용하지 않았다. `avcodec-58.dll`의 Ghidra 문자열
분석은 Java 힙 부족으로 중단됐다. 이 보고서의 CapCut 근거는 PE import 결과다.

| DLL | REA 증거 ID |
| --- | --- |
| `VECreator.dll` | `ev_a4c8693d9d0fd0f3c93497c5f1d66f922af6caf73b56a47c5a754ef53efc24e3` |
| `ttvideoeditor.dll` | `ev_757d8dcd6634e28cc6b8d11bea21aee609da605b98046621fe5275cb6ca792eb` |
| `avcodec-58.dll` | `ev_95d7d1ff2f5bf158cb86fe6c73a5b7bcbc3b7b1e824355b4e9c37b2c02183758` |

[OpenCut](https://github.com/OpenCut-app/OpenCut/blob/main/README.md)과
[OpenCut Classic의 wgpu 합성기](https://github.com/OpenCut-app/opencut-classic/tree/main/rust/crates/compositor)는
타임라인과 GPU 합성을 분리하는 공개 사례다. 영상 소스가 있는 프로젝트의 GPU
디코딩과 프레임 전달에는
[NVIDIA의 FFmpeg 하드웨어 가속 문서](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.1/ffmpeg-with-nvidia-gpu/index.html)를 참고했다.

## Music Motion Studio

Qt가 가사, 텍스트, 키프레임 및 효과를 BGRA 레이어로 그린다. Python은 레이어를
해시하고 직렬화한다. Rust는 레이어를 읽어 해시와 형상을 검증하고 변경된 레이어를
D3D11에 업로드한다. D3D11에서 합성과 NV12 변환을 끝낸 텍스처를 같은 장치의
NVENC에 전달한다. NV12를 CPU로 읽어오는 단계는 0회다. FFmpeg가 오디오를
준비하고 완성된 H.264와 함께 MP4로 저장한다.

기준 커밋 `3102513`에서 시작해 i5-14600KF, RTX 5060(드라이버 621.01)에서
`giggles.pvsproj`의 첫 30초를 1920×1080 60fps로 측정했다. 같은 Rust 실행
파일에서 기존 설정과 변경 설정을 A-B-B-A 순서로 비교했다. 각 열은 두 번의
평균이다.

| 지표 | 기존 | 공유 메모리 + 병렬 검증 |
| --- | ---: | ---: |
| CLI 전체 시간 | 69.11초 | 62.46초 |
| 전체 처리량(1,800프레임 ÷ CLI 전체 시간) | 26.05fps | 28.82fps |
| 프레임 스트리밍 처리량 | 29.04fps | 32.55fps |
| 파이프 쓰기 시스템 호출 누적 | 34.58초 | 0.36초 |
| 공유 메모리 쓰기 누적 | — | 3.72초 |
| Rust 패킷 수신·검증(입력 대기 포함) | 53.72초 | 46.09초 |
| D3D11 업로드·제출 | 3.91초 | 4.85초 |
| D3D11 GPU 완료 대기 | 0.014초 | 0.016초 |
| NVENC 호출 | 3.94초 | 3.92초 |
| Qt 캡처 | 31.46초 | 34.45초 |
| 내보내기 프로세스 트리 CPU 시간(표본) | 140.17 코어·초 | 133.16 코어·초 |
| 내보내기 프로세스 트리 평균 CPU 사용률(20 논리 코어 대비) | 10.00% | 10.47% |
| 장치 전체 평균 GPU / 인코더 / 디코더 사용률 | 13.21% / 4.94% / 3.63% | 14.02% / 5.38% / 3.64% |

공유 메모리만 사용한 별도 A-B-B-A의 평균은 69.77초에서 68.07초로 줄었다.
같은 공유 메모리 설정의 5초 클립에서 병렬 검증을 켜자 Rust 매핑 데이터
복사·파싱·검증이 5.18초에서 4.02초로 줄었다. 단계별 시간에는 다른 프로세스나
스레드와 겹치는 구간이 있다. GPU 표본은 같은 장치의 다른 프로그램도 포함한다.

네 MP4에서 디코딩한 1,800개 영상 프레임과 PCM 오디오의 SHA-256이 각각
일치했다. 해상도, 60/1 FPS, BT.709 색 정보, 시작 시각 0초, 영상과 오디오
길이 30초도 일치했다. 이 비교에는 해당 클립의 가사, 키프레임 및 효과가
포함된다. MP4 파일 자체의 SHA-256도 네 번 모두 일치했다.

4K60 3초 검증에서는 변경 설정이 180프레임 중 148개를 공유 메모리로,
32개를 파이프로 전송했다. 결과 MP4의 SHA-256은 기준 파일과 일치했다.
전체 시간은 기준 27.74초, 변경 27.91초였다. 이 해상도의 성능 향상은
확인되지 않았다.

자동 프로필은 기존 MMSB 패킷을 한 번에 최대 32 MiB의 공유 메모리로 보내고,
파이프로 길이와 ACK를 주고받는다. 매핑을 할당할 수 없거나 용량을 넘는 경우
파이프를 사용한다. Rust는 큰 레이어 묶음의 SHA-256 검증을 최대 두 스레드에서
실행한다. 크기, 레이어 순서, 불투명 베이스, CFR/PTS 검증을 마친 프레임만
인코더에 전달한다. 영상 인코더, 인코딩 설정, Qt 페인트, FFmpeg 오디오 및 MP4
결합은 기존 경로를 사용한다.

1080p60 실시간에 도달하려면 프레임당 전체 16.7ms 이내가 필요하다. 변경 설정의
Qt 캡처만 평균 프레임당 약 19.1ms이고, 프레임 생산 전체는 약 24.5ms였다.
Z 밴드마다 반복하는 Qt 상태 적용과 래스터화를 줄이거나, 결과 픽셀을 유지하면서
동적 레이어를 GPU 합성 명령으로 바꿔야 다음 단계에 진입할 수 있다.
영상 소스가 있는 프로젝트는 현재 Rust 경로가 요구하는 프레임 단위 디코딩,
탐색, 배속, 반복, 마스크, 색 필터를 제공할 때까지 기존 FFmpeg 내보내기를 쓴다.
이번 클립은 Qt/오디오 기반으로 생성하므로 Video Decode 점유율은 성능 목표에
포함하지 않았다.

## 재현

저장소 루트에서 PowerShell로 실행한다. 기존 영상과 JSON을 덮어쓰지 않도록
새 측정 폴더를 사용한다.

```powershell
.\.venv\Scripts\python.exe scripts/rust_prerequisites.py
cargo build --release --manifest-path native/gpu_export_core/Cargo.toml --bin d3d11_qt_canvas_stream_export
$project = "$env:USERPROFILE\Desktop\giggles.pvsproj"
$output = (New-Item -ItemType Directory "build/gpu-ab-$(Get-Date -Format yyyyMMdd-HHmmss)").FullName
$settings = @('--project', $project, '--fps', '60', '--scale', '1.5', '--smoke-seconds', '30', '--gpu-profile', 'automatic')
foreach ($trial in 'a1', 'b1', 'b2', 'a2') {
    $candidate = @()
    if ($trial.StartsWith('a')) {
        $candidate = @('--no-gpu-shared-packet-transport', '--no-gpu-parallel-packet-validation')
    }
    .\.venv\Scripts\python.exe -m tools.native_gpu_full_export @settings @candidate --output "$output/$trial.mp4" --report "$output/$trial.json"
    if ($LASTEXITCODE -ne 0) { throw "GPU export failed for $trial" }
}
```

JSON의 `complete_cli_wall_seconds`와 `native_report.native_stream_wall_fps`를 비교한다.
별도 창에서 `nvidia-smi --query-gpu=timestamp,utilization.gpu,utilization.encoder,utilization.decoder --format=csv -lms 500`으로 장치 전체 사용률을 수집한다.
`ffprobe -count_frames`로 1,800프레임과 해상도, 색 정보, 오디오 길이를 확인한다.
`ffmpeg -v error -i <MP4> -map 0:v:0 -c:v rawvideo -pix_fmt yuv420p -f hash -hash sha256 -`와
`ffmpeg -v error -i <MP4> -map 0:a:0 -c:a pcm_s16le -f hash -hash sha256 -`로 디코딩한 영상과 오디오를 비교한다.

## Qt 캡처의 트랙 색상 조회

`7aa2764`의 5초 프로파일에서 앨범 아트 기반 색상 조회가 601회 발생했다.
기존 앨범 아트 캐시에 색상이 있어도 조회할 때마다 파일 존재 여부와 수정 시각을
확인하고 절대 경로를 구했다. Rust GPU 내보내기의 분리된 Qt 장면에 한해 오디오와
커버 경로가 같은 색상을 한 작업에서 재사용한다. 유효한 색상이 없는 경우도
같은 작업에서 재조회하지 않으며, 다음 내보내기에서는 색상을 새로 조회한다.

같은 1080p60 30초 클립의 별도 A-B-B-A에서 Rust 워커와 자동 프로필을 그대로
사용했다. 기준 실행은 `CanvasSnapshot.capture_track` 호출에서
`personal_color_cache`만 생략했다. 전체 시간은 A1 86.29초, B1 81.21초,
B2 79.81초, A2 80.23초였다.

| 지표 | 기준 평균 | 트랙 색상 재사용 평균 |
| --- | ---: | ---: |
| 전체 내보내기 시간 | 83.26초 | 80.51초 |
| 전체 처리량 | 21.62fps | 22.36fps |
| 프레임 스트리밍 처리량 | 24.04fps | 24.75fps |
| Qt 캡처 | 48.83초 | 45.69초 |
| Qt 상태 준비 | 22.68초 | 13.76초 |

전체 시간은 평균 3.3%, 상태 준비는 39.4% 줄었다. 전체 시간은 실행별 편차가
있었고 두 번째 기준 실행은 변경 실행 범위 안에 있었다. 네 MP4의 SHA-256은
모두 일치했다. 이 측정의 절대 시간은 앞선 전송 실험과 따로 해석한다.
