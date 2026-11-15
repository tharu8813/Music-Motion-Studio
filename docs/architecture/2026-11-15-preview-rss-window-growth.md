# 미리보기 A-B-B-A RSS 구간별 증가량 (2026-11-15)

[로드맵 #159](https://github.com/tharu8813/Music-Motion-Studio/issues/159)의 장시간 미리보기 안정성 검증을 위한 보조 지표이다.

## 기록하는 값

기존 `tools.performance_benchmark preview`는 warmup 후 성능 측정을 시작할 때의 `rss_start_bytes`, 측정 루프의 마지막 `rss_end_bytes`, 관측 중 최대 `rss_peak_bytes`를 JSON에 담는다.

`tools.preview_ab_report`는 네 실행 A1, B1, B2, A2에서 두 경계가 유효할 때만 아래 항목을 계산한다.

- `rss_window_growth.growth_bytes_abba`: **측정 도중 종료 RSS − 시작 RSS**. 메모리를 반환한 실행은 음수가 될 수 있다.
- `baseline_mean_bytes`와 `candidate_mean_bytes`: 각각 A1·A2, B1·B2의 평균 증가량
- `candidate_minus_baseline_mean_bytes`: 후보 증가량 평균과 기준 증가량 평균의 **바이트 단위 차이**. 기준값이 음수 또는 0일 수 있으므로 왜곡하기 쉬운 퍼센트 변화율 대신 절대값 차이를 기록한다.
- `baseline_pair_spread_bytes`, `candidate_pair_spread_bytes`: 반복 측정 사이에 관측된 증가량 격차

시작·종료 값 중 하나라도 없거나 숫자가 아니거나 음수·NaN·무한대이면 결과는 `null`이다. 값이 없다는 이유로 0바이트를 만들어 내지 않는다. 과거 벤치마크 JSON에 같은 필드가 있으면 그대로 계산한다.

## 해석 주의

RSS의 **최대값**과 **시작부터 종료까지의 변화량**은 다른 수치다. 증가량이 크더라도 캐시 워밍, 적응형 렌더 버퍼, 운영체제 페이지 정책, 미디어 디코더의 지연 할당 때문일 수 있다. 한번의 양수 증가만으로 메모리 누수를 확정하지 않는다.

실제 Windows RTX 5060 환경에서는 동일 프로젝트와 `eco`·`balanced`·`quality` 개별 조건으로 최소 60초 이상의 반복 재생, Seek, 전체화면 전환을 확인하고, 반복 A-B-B-A 측정의 RSS 최대값·증가량·프레임 P95 및 실제 GPU 리소스 해제 여부를 함께 살핀다. 장시간 메모리 검증은 계측값과 실제 UI/하드웨어 관찰이 모두 필요하다.
