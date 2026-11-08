# 내보내기 최적화 7단계: 실제 전후 성능·영상/음성 무결성 A/B 검증

1~6단계 구현과 GitHub PR 병합 자체는 **실제 속도 향상 입증이 아니다**.
이 절차는 같은 PC, 같은 프로젝트, 같은 출력 설정으로 **두 번 내보내기**한 결과를 비교한다.

## 사전 조건

- Windows, 동일 `.venv` 및 최신 main 코드에서 수행한다. `python -m pip install psutil`이 필요하다.
- 같은 13곡 프로젝트, 동일한 1080p / 60FPS / NVENC 프리셋, 동일 음악·가사·영상·키프레임, 동일 저장 디스크를 사용한다.
- PC 전원 관리/백그라운드 프로그램, 출력 인코더와 작업 모드는 두 측정에서 바꾸지 않는다.
- 두 내보내기 모두 종료 후 **Music Motion Studio 앱까지 종료**하여 진단 JSON을 만든다.
- 각 내보내기 결과 MP4를 별도 이름으로 보관한다. 예: `export-before.mp4`와 `export-after.mp4`.
- 대용량 장시간 영상의 경우 디스크 여유 공간을 확인한다. 필요하면 먼저 30~60초짜리 동일 구간의 짧은 복제 프로젝트로 기능/품질을 검사한다.

## 1. 같은 코드에서 기존 최적화 비활성화

PowerShell, 저장소 루트:

```powershell
$env:PLAYLIST_CANVAS_PIPE_WRITE_CHUNK_MIB = "1"
$env:PLAYLIST_CANVAS_DISABLE_IDENTITY_SCALE = "1"
$env:PLAYLIST_CANVAS_DISABLE_CFR_FASTPATH = "1"
$env:PLAYLIST_CANVAS_DISABLE_FIXED_OVERLAY_EVAL = "1"
$env:PLAYLIST_CANVAS_DISABLE_OPACITY_KEYFRAME_ROI = "1"
$env:PLAYLIST_CANVAS_DISABLE_WORKER_LYRIC_CACHE = "1"
python tools/export_bottleneck_profile.py --output build/ab-before.json -- python main.py
```

프로그램에서 영상을 한 번 내보내고, 영상 파일을 `export-before.mp4` 등 별도 이름으로 남긴 뒤 프로그램을 닫는다.

**제약:** 이 실험은 2~6단계에서 제공한 환경변수들을 끄는 비교로, 1단계 이후 모든 중간 커밋의 과거 구현을 그대로 재현하는 것은 아니다. 중간에 수정한 버그나 별도의 고정 렌더링 규칙은 동일 최신 코드에 적용된다.

## 2. 동일 코드에서 최적화 활성화

```powershell
$names = @(
  "PLAYLIST_CANVAS_DISABLE_IDENTITY_SCALE",
  "PLAYLIST_CANVAS_DISABLE_CFR_FASTPATH",
  "PLAYLIST_CANVAS_DISABLE_FIXED_OVERLAY_EVAL",
  "PLAYLIST_CANVAS_DISABLE_OPACITY_KEYFRAME_ROI",
  "PLAYLIST_CANVAS_DISABLE_WORKER_LYRIC_CACHE"
)
foreach ($name in $names) {
    Remove-Item "Env:$name" -ErrorAction SilentlyContinue
}
$env:PLAYLIST_CANVAS_PIPE_WRITE_CHUNK_MIB = "4"
python tools/export_bottleneck_profile.py --output build/ab-after.json -- python main.py
```

동일 프로젝트와 출력 옵션으로 다시 한 번 내보내고 `export-after.mp4`로 보존, 프로그램을 닫는다.

## 3. 전후 성능 및 MP4 비교

우선 프로파일만 비교하려면(FFmpeg, FFprobe 없이 사용 가능):

```powershell
python tools/export_ab_verify.py `
  --baseline-profile build/ab-before.json `
  --optimized-profile build/ab-after.json `
  --output build/ab-report.json `
  --markdown build/ab-report.md
```

실제 MP4까지 비교하려면 **ffmpeg.exe와 ffprobe.exe가 모두 설치되어 있어야 한다.**
PATH에서 찾지 못하면 각각 `--ffmpeg "C:\...\ffmpeg.exe"`, `--ffprobe "C:\...\ffprobe.exe"`를 제공한다.

```powershell
python tools/export_ab_verify.py `
  --baseline-profile build/ab-before.json `
  --optimized-profile build/ab-after.json `
  --baseline-video "C:\Videos\export-before.mp4" `
  --optimized-video "C:\Videos\export-after.mp4" `
  --output build/ab-report.json `
  --markdown build/ab-report.md
```

비교 결과는 실행 화면과 `build/ab-report.json`, `build/ab-report.md`에 출력된다. 둘 다 임시 측정 파일로 사용자 파일을 수정하지 않는다.

## 결과 해석

- 새 프로파일의 `export.total_wall_seconds`는 **출력 옵션 확정 후 내보내기 실행 시작부터 작업자 종료 이벤트까지**의 벽시계 시간이며, 오디오·Canvas 준비 및 인코딩을 포함한다. 완료 대화상자에 머무는 시간은 포함하지 않는다. 실제 성공 완료는 `export.completed_wall_seconds`, 취소·실패는 `export.incomplete_wall_seconds`로 구분한다. A/B 비교에서는 성공 시간만 사용하고 실패·취소만 발생한 새 프로파일은 거부한다. 구버전 JSON에서 관련 값이 없다면 누락으로 표시한다.
- `export.renderer_seconds`는 FFmpeg 최종 렌더링의 벽시계 시간이다. 전체 준비/캔버스/음원 처리 시간이 포함되지 않을 수 있다.
- `canvas.scene_render_seconds`와 `export.frame_delivery_seconds`는 병렬 worker들의 **포괄적 시간의 합계**다. 합산해 내보내기 총 시간이라고 말하면 안 된다.
- CPU core-seconds/최대 RSS는 **앱이 실행된 전체 시간**에서 샘플링한다. 두 측정은 앱을 열고 기다린 시간이 비슷해야 공정하다. NVIDIA 사용률은 **GPU 장치 전체**의 이용률이다.
- MP4 검사 `pass`는 출력 해상도, FPS, 영상 프레임 개수(컨테이너가 기록한 경우), 영상·오디오 길이 및 스트림 구조의 주요 일치 검사만 통과했다는 뜻이다. 오디오 샘플 완전 동일·모든 영상 픽셀 동일을 의미하지 않는다.
- 선택된 **초반·중반·후반** 세 시점의 디코딩된 영상 프레임 SHA256을 비교한다. 같은 프레임이라도 손실 압축·인코더 또는 탐색의 차이로 해시가 달라질 수 있다. `review`는 *검토 필요*이며 자동 오류 판정이 아니다.
- 가사/비주얼라이저의 정확한 반전과 움직임까지 판정하려면 **해당 이슈가 나타났던 프레임**을 추가로 눈으로 비교해야 한다. 세 프레임 샘플만으로 모든 렌더링 버그를 제외할 수 없다.
- FFprobe가 프레임 수를 반환하지 않는 경우 `frame_count`는 `null`이 되고 검증 누락 경고가 표시된다. 검사 도구는 없는 수치를 추정하지 않는다.
- `capture_status=export_observed`는 FFmpeg 프로세스 또는 단계 시간이 관측됐다는 뜻으로, 내보내기 성공 자체를 보증하지 않는다. 두 결과 MP4가 완전히 기록됐는지도 확인해야 한다.

### 판정

- `measurement_only`: 프로파일 비교만 했고 MP4 안정성은 검사하지 않음
- `pass`: 검사한 MP4 스트림 구조/길이가 일치하고 샘플 디코딩 프레임도 같음. 손실 압축 등으로 표본이 달라지면 `review` 가능
- `review`: 파일이 구조적으로는 일치하지만 샘플 프레임이 다르거나 일부 품질 검증이 미완료
- `fail`: 출력 해상도, FPS, 확인 가능한 프레임 수, 영상/오디오 길이 등 **주요 무결성 검사 실패**

검증에 사용하는 JSON은 개인 음악 파일 자체를 내보내지 않는다. 그러나 출력 파일 경로가 직접적으로 보고서에 포함되지 않도록 조치했더라도, 원본 진단에는 장치 메타데이터가 포함되므로 외부 공개 전 민감한 내용을 검토한다.

## CI

`tests/test_export_ab_verify.py`와 `tests/test_export_total_wall_timer.py`를 Windows CI에서 기존 단위 테스트와 독립된 초기 검증 단계에 실행한다. FFmpeg/FFprobe가 없어도 도구의 메트릭 계산, 판정, 오류 처리, JSON/Markdown 저장 테스트가 동작한다.

**실제 최적화 결과 값은 Windows 사용자 프로젝트를 실행해야만 얻을 수 있다.** PR 빌드 통과만으로 개선 퍼센트를 확정해서는 안 된다.
