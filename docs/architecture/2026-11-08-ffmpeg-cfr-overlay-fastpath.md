# 내보내기 FFmpeg 합성 4단계: 중복 CFR 처리 및 고정 레이어 좌표 평가 제거

## 문제와 범위

13곡/36분 30초 내보내기에서 FFmpeg CPU 사용이 적지 않았지만, 계측된 CPU 시간에는 프레임 디코딩·레이어 합성·NVENC 전달 등이 함께 포함된다. 이 중 특정 함수가 병목이라고 확정할 수는 없다.

기존 `filter_complex` 첫 단계는 파이프가 **이미 정확한 출력 FPS로 전달하는 rawvideo 프레임**에 대해서도 매번 `fps=...`를 실행한다.

```text
[0:v]fps=60:start_time=0,settb=AVTB,setpts=N/(60*TB)[base]
```

또한 `overlay=x:y`는 x/y가 변하지 않아도 기본값 `eval=frame`이기 때문에 좌표를 매 프레임 다시 평가할 수 있다.

이번 변경은 다음 조건에서만 계산을 줄인다.

1. Canvas base 입력의 실제 FFmpeg 명령에 `-f rawvideo`, `-pix_fmt bgr0|rgba`, `-video_size`, 정확한 `-framerate`, `-i`가 있는 경우에만 **중복 base fps 필터**를 건너뛴다. 타임스탬프 정규화 `settb=AVTB,setpts=N/(fps*TB)`는 반드시 유지한다. PNG/concat, 인코딩된 임시 비디오, 프레임 레이트 불명인 소스에는 기존 `fps` 필터를 사용한다.
2. 해당 raw CFR 경로의 **위치가 고정된** 비주얼라이저·영상·정적 Z 레이어에만 FFmpeg `overlay:eval=init`를 사용한다. 키프레임으로 이동하는 소스(`keyframe_binding`)에는 기본 `eval=frame` 유지.
3. 이전 단계의 고해상도 최종 `scale/pad` 제거는 독립적으로 동작한다. 출력과 입력 해상도가 다르면 scale/pad를 그대로 사용한다.

오디오, 색상, 텍스트, 가사, 프레임 수, 비주얼라이저 위치/반전, NVENC 품질/프리셋, Z 레이어 순서, 이전 단계의 사용자 설정을 변경하지 않는다.

## 롤백 및 A/B 비교 (Windows PowerShell)

신규 경로를 모두 비활성화한 기준선:

```powershell
$env:PLAYLIST_CANVAS_DISABLE_CFR_FASTPATH = "1"
$env:PLAYLIST_CANVAS_DISABLE_FIXED_OVERLAY_EVAL = "1"
python tools/export_bottleneck_profile.py --output build/ffmpeg-cfr-baseline.json -- python main.py
```

변경을 활성화한 뒤 동일 프로젝트·출력 옵션으로 비교:

```powershell
Remove-Item Env:PLAYLIST_CANVAS_DISABLE_CFR_FASTPATH -ErrorAction SilentlyContinue
Remove-Item Env:PLAYLIST_CANVAS_DISABLE_FIXED_OVERLAY_EVAL -ErrorAction SilentlyContinue
python tools/export_bottleneck_profile.py --output build/ffmpeg-cfr-fastpath.json -- python main.py
```

`PLAYLIST_CANVAS_DISABLE_IDENTITY_SCALE`(3단계), `PLAYLIST_CANVAS_PIPE_WRITE_CHUNK_MIB`(2단계)은 두 측정에서 같은 값이어야 비교가 타당하다. 준비 시간·디스크 캐시·동시 실행 프로그램·전원 관리도 최대한 일치시킨다.

로그의 `FFmpeg Canvas fast paths: raw_cfr=True fixed_overlay_eval=True`는 이번 단계 적용 여부를 보여준다. `False`라면 레거시 처리이므로 성능 차이가 없어도 정상이다.

## 검증 및 한계

- 샘플 FFmpeg 7.1.5로 60FPS raw BGR0 입력 24프레임과 알파 오버레이 2개를 합성했다. 기존 방식과 두 최적화를 적용한 방식의 **yuv420p 전체 24프레임 바이트와 SHA256 해시가 일치**했다.
- `tests/test_ffmpeg_cfr_overlay_fastpath.py`에 조건 검증, 정적 레이어 순서, 키프레임 이동 시 eval=frame 보존, 실제 FFmpeg 픽셀 검사를 추가했다.
- 사용자 Windows의 1080p60 NVENC 36분 30초 전체 내보내기는 아직 새 버전으로 재측정하지 않았다. `eval=init`의 좌표 계산 절감 효과는 해상도 전체 합성·데이터 전송 비용보다 작을 수 있으므로 **속도 향상률은 미확정**이다.
- FFmpeg `overlay` 필터 자체의 풀프레임 픽셀 연산 비용은 이번 단계에서 줄이지 않았다. 다음 단계는 각 Z 레이어의 실제 덮는 영역과 FFmpeg 컬러 포맷 협상 시간을 따로 측정하고 개선해야 한다.
