# 설계 및 측정 이력

현재 개발 순서와 완료 기준은 [안정화 및 성능 개선 로드맵](../DEVELOPMENT_ROADMAP.md)을 참조합니다. 이 폴더의 날짜별 문서·수치·실행 환경은 기록된 시점의 증거입니다. 최신 동작은 소스와 CI에서 확인하며 과거 측정값을 최신 성능 보증으로 사용하지 않습니다.

## 핵심 기술 계약과 참고 자료

- [프레임 전송 계약](../NATIVE_FRAME_TRANSPORT.md), [네이티브 프로젝트 런타임](../NATIVE_PROJECT_RUNTIME.md), [GPU 계측](../PROCESS_SCOPED_GPU_PROFILING.md)
- [구조 개요 (2026-10-05 역사적 기록)](overview.md), [성능 계측 기록](performance.md), [Rust GPU 엔진 설계 기록](rust-gpu-export-core.md)
- [A-B-B-A 판정 의미](2026-11-14-p1-abba-verdicts.md), [alpha trim 검증](2026-11-14-alpha-trim-abba.md), [미리보기 A-B-B-A](2026-11-15-preview-abba-comparison.md)
- [원시 benchmark 데이터](benchmarks/), [전체 리뷰 #136](https://github.com/tharu8813/Music-Motion-Studio/issues/136), [미리보기 #159](https://github.com/tharu8813/Music-Motion-Studio/issues/159)

## 2026-11-14 개별 최적화 통합 기록

기존 네 개의 소규모 변경 기록을 한곳에서 조회하도록 통합했습니다. 모두 과거 변경의 목적과 검증 범위를 기록한 것이며 최신 실행 계약은 코드·테스트에서 확인합니다. 원본은 정리 직전 커밋에 보존되어 있습니다.

| 변경 | 구현·검증 경계 | 원본 |
| --- | --- | --- |
| Native frame lookup | frozen export의 track ID·번호·intro/outro 길이를 1회 계산하고, 일반 조회는 기존 동적 방식 유지. 긴 플레이리스트와 분수 경계의 상태 일치 검사. 실제 속도 향상 별도 검증. | [원문](https://github.com/tharu8813/Music-Motion-Studio/blob/e00b401be4efc60bee5a2ef5cbcef092ff8cf9c6/docs/architecture/2026-11-14-native-frame-lookup.md) |
| Qt font template cache | 소스당 최대 64개 QFont 템플릿, 변경 시 cache key/무효화, detached copy로 호출자 변경 격리. Hangul/Latin raster 및 advance 검사. | [원문](https://github.com/tharu8813/Music-Motion-Studio/blob/e00b401be4efc60bee5a2ef5cbcef092ff8cf9c6/docs/architecture/2026-11-14-text-font-cache.md) |
| No-op keyframe scope | 숫자 keyframe이 없는 경우 빈 scope 생략, 편집기 blur 복원 예외 유지. 정적·동적 raster 일치 및 상태 복원 검사. | [원문](https://github.com/tharu8813/Music-Motion-Studio/blob/e00b401be4efc60bee5a2ef5cbcef092ff8cf9c6/docs/architecture/2026-11-14-p1-canvas-keyframe-scope.md) |
| Layer source type diagnostics | 최종 packet 순서별 kind/source_types/source_count를 진단에 기록. ID·미디어 경로·제목 제외; 출력 픽셀·패킷 계약 변경 없음. | [원문](https://github.com/tharu8813/Music-Motion-Studio/blob/e00b401be4efc60bee5a2ef5cbcef092ff8cf9c6/docs/architecture/2026-11-14-p1-layer-source-types.md) |

당시 720p60/30초 1,800프레임 기록은 22.878초 CLI, 85.02fps native, Qt capture 15.461초, SHA CPU 9.504초, 전송 10.393GB였다. 이 값들은 1080p 측정과 다르며 서로 겹치는 단계 시간을 합산하지 않는다. 실제 성능 개선은 동일 환경 A-B-B-A 및 decoded-video/PCM/PTS 일치와 별도로 판정한다.

## 날짜별 보존 기록

### 가사·번역

- [2026-10-28-lyrics-search-reliability](2026-10-28-lyrics-search-reliability.md)
- [2026-10-28-ttml-edit-safety](2026-10-28-ttml-edit-safety.md)
- [2026-10-28-word-timed-lyrics-sources](2026-10-28-word-timed-lyrics-sources.md)
- [2026-10-29-lrclib-provider](2026-10-29-lrclib-provider.md)
- [2026-11-01-lyrics-spacing](2026-11-01-lyrics-spacing.md)
- [2026-11-01-lyrics-translation-prompt](2026-11-01-lyrics-translation-prompt.md)
- [2026-11-01-lyrics-translation](2026-11-01-lyrics-translation.md)
- [2026-11-02-translation-stability](2026-11-02-translation-stability.md)
- [2026-11-02-translation-workspace](2026-11-02-translation-workspace.md)
- [2026-11-05-local-llm-model-management](2026-11-05-local-llm-model-management.md)
- [2026-11-08-canvas-worker-lyric-cache](2026-11-08-canvas-worker-lyric-cache.md)
- [2026-11-19-local-lyrics-quality-benchmark](2026-11-19-local-lyrics-quality-benchmark.md)
- [2026-11-19-lyrics-translation-quality](2026-11-19-lyrics-translation-quality.md)
- [2026-11-19-two-stage-lyrics-translation](2026-11-19-two-stage-lyrics-translation.md)

### 편집·모션·AutoMix

- [2026-10-15-automix-playback](2026-10-15-automix-playback.md)
- [2026-10-29-source-customization](2026-10-29-source-customization.md)
- [2026-11-02-keyframe-system](2026-11-02-keyframe-system.md)
- [2026-11-03-advanced-keyframes](2026-11-03-advanced-keyframes.md)
- [2026-11-03-custom-motion-editor](2026-11-03-custom-motion-editor.md)
- [2026-11-03-motion-editor-completion](2026-11-03-motion-editor-completion.md)
- [2026-11-03-motion-property-lanes](2026-11-03-motion-property-lanes.md)

### 미리보기·렌더링·성능·배포

- [2026-10-15-preview-entry](2026-10-15-preview-entry.md)
- [2026-11-04-pipeline-performance](2026-11-04-pipeline-performance.md)
- [2026-11-04-precision-timing-motion-stability](2026-11-04-precision-timing-motion-stability.md)
- [2026-11-04-preview-rendering-validation](2026-11-04-preview-rendering-validation.md)
- [2026-11-08-cfr-zero-frame-pruning](2026-11-08-cfr-zero-frame-pruning.md)
- [2026-11-08-export-ab-validation](2026-11-08-export-ab-validation.md)
- [2026-11-08-opacity-keyframe-zband-roi](2026-11-08-opacity-keyframe-zband-roi.md)
- [2026-11-09-qt-canvas-native-d3d11-bridge](2026-11-09-qt-canvas-native-d3d11-bridge.md)
- [2026-11-11-automatic-lz4-bgra](2026-11-11-automatic-lz4-bgra.md)
- [2026-11-11-native-canvas-per-band-timing](2026-11-11-native-canvas-per-band-timing.md)
- [2026-11-11-native-per-layer-lz4-cost](2026-11-11-native-per-layer-lz4-cost.md)
- [2026-11-11-native-preamble-write-coalescing](2026-11-11-native-preamble-write-coalescing.md)
- [2026-11-11-native-validated-sha-reuse](2026-11-11-native-validated-sha-reuse.md)
- [2026-11-11-qt-rgb32-direct-bgra](2026-11-11-qt-rgb32-direct-bgra.md)
- [2026-11-11-v2-zero-copy-reuse](2026-11-11-v2-zero-copy-reuse.md)
- [2026-11-12-native-qt-source-type-paint-profile](2026-11-12-native-qt-source-type-paint-profile.md)
- [2026-11-12-native-qt-text-stage-profile](2026-11-12-native-qt-text-stage-profile.md)
- [2026-11-12-native-visual-fidelity](2026-11-12-native-visual-fidelity.md)
- [2026-11-12-release-stabilization-audit](2026-11-12-release-stabilization-audit.md)
- [2026-11-12-rust-gpu-export-rework](2026-11-12-rust-gpu-export-rework.md)
- [2026-11-14-alpha-trim-abba](2026-11-14-alpha-trim-abba.md)
- [2026-11-14-native-final-raster-diagnostics](2026-11-14-native-final-raster-diagnostics.md)
- [2026-11-14-native-gpu-p1-benchmark](2026-11-14-native-gpu-p1-benchmark.md)
- [2026-11-14-p1-abba-verdicts](2026-11-14-p1-abba-verdicts.md)
- [2026-11-14-p1-qt-scene-render-profile](2026-11-14-p1-qt-scene-render-profile.md)
- [2026-11-14-p1-qt-state-detail-profile](2026-11-14-p1-qt-state-detail-profile.md)
- [2026-11-14-p1-qt-state-source-type-profile](2026-11-14-p1-qt-state-source-type-profile.md)
- [2026-11-14-p1-sha-equal-pixels-experiment](2026-11-14-p1-sha-equal-pixels-experiment.md)
- [2026-11-15-p1-qt-glyph-shadow-cache-profile](2026-11-15-p1-qt-glyph-shadow-cache-profile.md)
- [2026-11-15-p2-model-bundle-release-integrity](2026-11-15-p2-model-bundle-release-integrity.md)
- [2026-11-15-preview-abba-comparison](2026-11-15-preview-abba-comparison.md)
- [2026-11-15-preview-rss-window-growth](2026-11-15-preview-rss-window-growth.md)

## 날짜 없는 기술 기록

- [export-blur-2026-10-26](export-blur-2026-10-26.md)
- [export-bottleneck-profiling](export-bottleneck-profiling.md)
- [gpu-first-rust-export-migration](gpu-first-rust-export-migration.md)
- [ivy-export-profile-2026-10-26](ivy-export-profile-2026-10-26.md)
- [native-affine-v2](native-affine-v2.md)
- [nvenc-hardware-frame-bridge](nvenc-hardware-frame-bridge.md)
- [overview](overview.md)
- [parallel-canvas-2026-10-27](parallel-canvas-2026-10-27.md)
- [performance](performance.md)
- [refactor-2026-10-26](refactor-2026-10-26.md)
- [refactor-session-2026-10-26](refactor-session-2026-10-26.md)
- [rust-gpu-export-core](rust-gpu-export-core.md)
- [t29-native-source-compatibility](t29-native-source-compatibility.md)

원시 측정 자료와 다른 문서가 참조하는 결과는 이동하거나 삭제하지 않았습니다. 이전 문서 복원과 정리 기준은 [문서 안내](../README.md#정리-기준과-복원)를 참조합니다.
