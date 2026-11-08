# 6단계: 병렬 Canvas 작업자의 가사 고급 설정 복사 캐시

## 문제

Canvas 내보내기의 `CanvasSnapshot.capture_track`은 각 프레임에서 가사 Source의 고급 설정을 모델에 적용한다. `Source.resolved_lyrics()`는 `subtitle_advanced_settings`의 중첩 값을 `deepcopy`한 뒤 대체 Source를 만들고, 호출이 끝나면 원본 편집 Source로 복원한다.

가사가 많은 60FPS 프로젝트에서는 같은 설정을 반복 복사하는 시간이 누적될 수 있다. 해당 캐시는 영상 픽셀이나 실시간 음악 분석을 저장하지 않으며, **변하지 않는 가사 스타일 설정의 복사본만** 재사용한다.

## 적용 범위 및 무효화 정책

- `CanvasProcessStream`의 **spawn된 독립 Windows 작업자**에서만 활성화한다. 이 작업자는 내보내기 시작 시 직렬화한 Source의 별도 복제본으로 작업하고, 편집 UI의 변경사항을 받지 않는다.
- `ExportCanvasCapturer(frozen_worker_scene=True)`는 소스 객체 정체성(`id` + 원본 객체의 강한 참조)을 기준으로 Source별 고급 설정 해석 결과를 보관한다. 초기 계산 후 같은 작업자가 다음 프레임을 그릴 때 재사용한다.
- 일반 캔버스 편집, 단일 프로세스 fallback, 미리보기에서는 활성화하지 않는다. 변경 가능한 속성을 잘못 캐시할 가능성을 방지한다.
- 호출 도중 예외·취소가 발생해도 `ExitStack`/컨텍스트 관리자에서 **원본 Source로 복구**한다. 기본 글꼴, 가사 텍스트/타이밍, 키프레임 계산, 애니메이션 동작은 계속 프레임마다 갱신된다.
- 가사에 `subtitle_advanced_categories`가 없으면 기존과 마찬가지로 복사가 발생하지 않으므로 최적화 효과도 거의 없다.

## 롤백 및 A/B 측정

같은 소스/음원/출력 설정에서 한 번씩 실행한 뒤 실제 내보내기 시간을 비교한다.

캐시 비활성화(기존 경로):

```powershell
$env:PLAYLIST_CANVAS_DISABLE_WORKER_LYRIC_CACHE = "1"
python tools/export_bottleneck_profile.py --output build/worker-lyrics-before.json -- python main.py
```

캐시 활성화(기본값):

```powershell
Remove-Item Env:PLAYLIST_CANVAS_DISABLE_WORKER_LYRIC_CACHE -ErrorAction SilentlyContinue
python tools/export_bottleneck_profile.py --output build/worker-lyrics-after.json -- python main.py
```

캐시는 **병렬 Canvas 작업자가 실행되는 내보내기**에서만 사용된다. 작업자 병렬 처리가 안전하지 않아서 직렬 경로가 선택된 프로젝트는 변경 전후 차이가 없어도 정상이다.

## 검증

- `tests/test_canvas_worker_lyrics_cache.py`: 가사 고급 설정이 켜진 동일 프로젝트를 세 프레임 렌더링해 기존/캐시된 픽셀 바이트를 비교한다.
- 원본은 고급 설정 해석을 매 프레임(3회) 수행하고, 캐시된 워커는 소스당 1회 수행하도록 검증한다.
- 편집 모델 변경 시 일반 UI 경로는 여전히 매번 최신 설정을 읽는다.
- 예외 시 컨텍스트 종료 후 원본 모델이 돌아오는지 확인한다.

**주의:** 캐시의 계산 절약이 전체 작업 시간을 얼마나 단축하는지는 Windows 1080p60 NVENC 실제 프로젝트에서 재측정해야 한다. 성능 증가는 가사 고급 설정이 있는 작업자에만 해당하며, 전체 CPU 병목의 완전한 해결을 의미하지 않는다.
