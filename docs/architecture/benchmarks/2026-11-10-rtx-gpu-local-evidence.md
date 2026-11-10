# 2026-11-10 RTX GPU 내보내기 로컬 검증 기록

이 문서는 예전에 저장소 최상위에 실수로 추적된 **로컬 테스트 중간 산출물**을 정리하면서 핵심 측정값만 남긴 기록입니다. 원본은 Git 기록의 `27fba35bd8e07d30a7a8ec501de3614ee8c9c208`에 남아 있으며, 테스트용 BGRA 프레임·H.264 영상·PNG는 **소스 코드나 자동화 테스트 fixture가 아닙니다**.

## 180프레임 RTX 5060 스트림 테스트
- 원본: `native-stream-check-01.json`, `native-stream-check-01.h264`
- NVIDIA GeForce RTX 5060, 640×360, 60 FPS, 180/180프레임, 3초 CFR.
- 인코더 계측 구간: **1.0372186초**. H.264: 2,074,395바이트. NV12 CPU readback: 0.
- 상태: `measured`; 타임스탬프 단조 증가, NVENC 동기 완료.
- **주의:** 6개 입력 프레임을 반복 전송한 스트리밍 성능 실험이며, Qt로 모든 프레임을 그린 전체 프로젝트 렌더링 속도나 최종 MP4 오디오/화질 검증이 아닙니다.

## 실제 프로젝트 2번째 곡 6프레임 진단
- 원본: `qt-giggles-second-song-v1/` 폴더의 `parity.json`, `gpu-report.json`, `timeline.json`, raw BGRA·PNG·H.264 파일.
- 구간 시작: **223.25초**, 640×360, 60 FPS, 6프레임, 5 BGRA 레이어/프레임.
- Qt 기준 이미지 대비 손실 압축 영상의 평균 RGB MAE: **1.4649** 코드값, 최악 p95 오차 **5.0** 코드값.
- 입력/디코딩 프레임 변화 확인, 측정 인코딩 구간 **0.0198421초**, NV12 CPU readback 0.
- 원본 보고서에 `full_project_visual_parity_verified=false`, `native_gpu_text_painting_implemented=false`라고 명시되어 있습니다. 이 기록만으로 전체 길이/전체 GPU 렌더링을 검증했다고 주장하면 안 됩니다.

## 원본 복원

필요할 때만 기존 Git 객체에서 **별도 작업 디렉터리로** 복원하세요. 최신 프로젝트의 자동 빌드나 배포에는 해당 원시 테스트 출력물이 필요하지 않습니다.

```powershell
git show 27fba35:qt-giggles-second-song-v1/parity.json
git show 27fba35:native-stream-check-01.json
```

정리 후 생성되는 새 GPU 테스트 결과는 루트에 커밋하지 말고 로컬에 보관하거나 GitHub Actions artifact 기능으로 보존하세요.
