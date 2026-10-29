# 단어·음절별 시간 정보가 있는 가사 공급원 조사

2026-10-28 기준 조사 및 연동 결과이다. 당시 기본 검색은 KuGou와 AMLL 두 공급원을 조회했다. 2026-10-29에 LRCLIB 검색을 다시 추가했으며, [LRCLIB 복원 기록](2026-10-29-lrclib-provider.md)과 [검색 구현·검증 기록](2026-10-28-lyrics-search-reliability.md)을 함께 참고한다.

## 필요한 데이터

Apple Music과 유사한 강조를 하려면 줄 시작 시각뿐 아니라 각 단어 또는 음절의 시작·종료 시각이 필요하다. 일반 LRC를 TTML로 감싸거나 줄 길이에 비례해 단어 시간을 나누는 것은 이 데이터를 구한 것과 다르다. TTML 파일이라는 이유만으로 단어 동기화가 보장되지 않으므로 실제 시간 속성을 검사해야 한다.

## 확인 결과

| 공급원 | 데이터 | 이번 확인 | 판단 |
| --- | --- | --- | --- |
| KuGou | KRC의 줄 시각 및 단어·음절 상대 시작 시각과 지속시간 | `TNT / CORTIS`, 길이 122,000ms 후보의 KRC 수신. 40개 줄과 231개 시간 구간, 159종의 지속시간 확인 | 원하는 한국 곡에 대해 실제로 확인한 경로. 비공식 API이므로 변경 가능성이 있고, 앱에서 쓰려면 KRC 파서 및 단어 오프셋 변환 필요 |
| AMLL TTML DB / API | 커뮤니티에서 제작·검수하는 TTML. 단어 시각, 화음·보조 보컬 등 표현 가능 | `ME! / Taylor Swift, Brendon Urie`에서 시작·종료 시각을 가진 span 581개 확인. `TNT / CORTIS`, `Dynamite / BTS`, `Supernova / aespa`의 곡명·아티스트 조합 검색은 0건 | TTML 형식이 현재 앱과 잘 맞음. 수록 여부가 제한이므로 곡별 확인 필요 |
| QQ Music / LDDC | QRC 단어 시간 정보. LDDC는 QQ·KuGou 등에서 정밀 가사를 가져오는 별도 도구 | 공식 프로젝트 문서 확인. 이번 조사에서는 QQ 다운로드를 실측하지 않음 | 이미 완성된 단어별 가사를 내려받는 도구 후보. 코드를 앱에 포함하는 경우 GPL-3.0임을 확인할 것 |
| Apple Music 자체 | TTML / syllable-lyrics 비공개 경로를 다루는 도구가 존재 | MusanovaKit 문서상 privileged developer token 필요. 공개 MusicKit의 hasLyrics는 가사 존재 여부 | 일반 개발자 토큰만으로 안정적으로 가져올 수 있는 공개 다운로드 API로 판단하지 않음. 실제 호출 검증은 하지 않음 |

KRC 및 TTML의 시간 구간이 존재한다는 사실은 확인했지만 오디오와의 청각적 동기 정확도까지 검수한 것은 아니다. 위 개수는 보조 보컬 등도 포함할 수 있어 화면에 표시되는 단어 수와 같지 않다.

AMLL의 `q`는 가사 본문까지 검색하므로 결과가 있다는 것만으로 곡 일치를 판단하면 안 된다. 이번 조사에서도 `Dynamite`, `Supernova`의 q 검색은 다른 곡의 본문을 찾았다. 일치 검증에는 반환된 musicNames / artistNames 및 플랫폼 ID를 사용해야 한다.

## 접근 경로

- AMLL 문서: <https://amll.dev/reference/http-api/overview>
- 검색: `GET https://api.amll.dev/v1/lyrics/search?musicName=...&artistName=...`
- TTML 가져오기: `GET https://api.amll.dev/v1/lyrics/get?id=...`
- 검색 및 가져오기는 문서상 인증 불필요. 저장소 데이터나 API를 자체 운영하는 경로도 있음.
- KuGou: `lyrics.kugou.com`의 검색 및 다운로드에서 `fmt=krc`. LRC 다운로드 결과와 구분해서 다뤄야 함.
- 공급원은 곡의 다른 편집본·길이를 반환할 수 있으므로 제목·아티스트 외에 길이와 음원 버전을 확인해야 함.

## 근거

- [AMLL TTML DB](https://github.com/amll-dev/amll-ttml-db): 커뮤니티 제작·검수 기준과 플랫폼 ID 매핑. Apple의 공식 가사 저장소가 아니다. 외부에서 유래한 데이터는 해당 제공자의 조건을 따르며 자체 작성 부분만 CC0라고 구분한다.
- [AMLL API 원문 문서](https://amll.dev/reference/http-api/native): 검색 조건, ID 조회, TTML 응답.
- [LDDC](https://github.com/chenmozhijin/LDDC): QQ/QRC, KuGou/KRC와 정밀 가사 저장 도구.
- [LDDC KuGou 어댑터](https://github.com/chenmozhijin/LDDC/blob/main/LDDC/core/api/lyrics/kg.py): KRC 조회·변환 경로.
- [MusanovaKit](https://github.com/rryam/MusanovaKit): 비공개 가사 엔드포인트 및 privileged token 요건.
- [MusicKit hasLyrics](https://developer.apple.com/documentation/musickit/song/haslyrics): 가사 존재 여부를 나타내는 속성.

KuGou KRC와 AMLL TTML을 모두 앱에 연동했다. KuGou는 KRC를 TTML로 변환하며, AMLL은 TTML 원문을 보존한다. 최종 코드에서도 TNT의 231개 시간 구간과 AMLL ME!의 581개 시간 구간을 확인했다. 위 비교표의 도구·형식 조사는 연동 전 확인 사항을 포함한다.
