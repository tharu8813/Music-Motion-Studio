# FFmpeg 합성 3단계: 동일 해상도 최종 스케일 제거

## 문제

실제 13곡 프로젝트 계측에서는 FFmpeg CPU 시간이 약 1,175 CPU초로 관측됐지만, 이 숫자는 **최종 인코딩, 오버레이 합성, 입력 디코딩을 모두 포함**한다. 해당 수치만으로 특정 필터가 병목이라고 확정할 수는 없다.

기존 최종 영상 필터 그래프는 Canvas가 이미 출력 크기와 동일한 예: 1920×1080 프레임을 공급하더라도 다음 작업을 매 프레임마다 요청했다.

```text
scale=1920:1080:force_original_aspect_ratio=decrease,
pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black
```

FFmpeg 내부적으로 동일 크기를 최적화할 가능성은 있지만, 별도 필터 협상과 프레임 처리 경로를 제거할 수 있다.

## 최적화 범위

- **오직 PipedVideoInput**: 검증된 1:1 픽셀 종횡비의 raw CFR Canvas 입력이며, `width == output_width`와 `height == output_height`가 모두 일치하는 경우에만 적용한다.
- `filter_complex` 레이어 합성 시 **마지막** `scale,pad`를 `null`로 교체한다. 레이어 Z 순서, 반전, 회전, 동적 키프레임, 알파 처리 및 FFmpeg의 기본 `fps/setpts`는 변경하지 않는다.
- 레이어가 없는 단일 입력 경로에서는 기존 `fps` 필터를 유지하되 반복 스케일링만 생략한다.
- 입력 해상도 불일치, 준비된 인코딩 영상, PNG/concat 등 기존 경로는 항상 원본 `scale,pad`를 사용한다.
- `-pix_fmt yuv420p`, FPS, 인코딩 품질/프리셋, 오디오 스트림 복사, MP4 저장 방식은 변경하지 않는다.

### 문제 발생 시 즉시 이전 동작으로 복귀

```powershell
$env:PLAYLIST_CANVAS_DISABLE_IDENTITY_SCALE = "1"
python tools/export_bottleneck_profile.py --output build/ffmpeg-scaling-before.json -- python main.py
```

최적화 적용:

```powershell
Remove-Item Env:PLAYLIST_CANVAS_DISABLE_IDENTITY_SCALE -ErrorAction SilentlyContinue
python tools/export_bottleneck_profile.py --output build/ffmpeg-scaling-after.json -- python main.py
```

프로그램 로그에서 `Final FFmpeg output scaling: identity=True`가 표시되어야 해당 내보내기에 최적화가 적용된 것이다. `False`라면 기존 처리가 유지되므로 성능 차이가 없어도 정상이다.

**주의:** 위 명령의 비교에는 이전 2단계의 파이프 쓰기 크기와 출력 옵션을 동일하게 사용해야 한다. 앞서 생성한 프로파일은 파이프 크기가 다른 버전의 것이므로 직접적인 3단계 성능 기준선으로 사용하지 않는다.

## 검증

- 출력 해상도가 다른 경우 기본 필터 문자열을 그대로 사용한다.
- 예시 64×48 10FPS FFmpeg 실제 영상(합성되는 알파 레이어 포함)에서 8개 프레임의 yuv420p raw bytes가 기존 필터와 새로운 `null` 필터에서 완전히 동일했다.
- `tests/test_ffmpeg_identity_scale.py`에 필터 생성, 레이어 합성 경로, FFmpeg 픽셀 일치 검증을 추가했다. 프로젝트의 Windows CI에서 별도로 검증한다.

성능 개선 폭은 Windows 1080p60 NVENC 및 실제 프로젝트에서 **미측정**이며, 데이터에 따라 차이가 작을 수 있다. GPU NVENC 사용률만으로 성능을 판단하지 말고, 벽시계 시간, FFmpeg CPU 시간과 실제 프레임 수·타임스탬프를 같이 확인한다.
