> 과거 조사·전환 기록입니다. 2026-09-14부터 현재 코드는 KTX 전용이며 SRT 어댑터와 srtgo 의존성은 제거했습니다. 현재 사용법은 README.md를 참고하세요.

# SRT·KTX 통합과 API 영향 조사

조사일: 2026-09-12 (한국시간)

후속 코드 변경: `reservation.py`는 이제 KTX 기본값의 제한 실행 진입점이며,
`train_cli.py`도 같은 `reservation_guard.py`를 사용합니다. 아래의 SRT 전용 스크립트,
과거 날짜, 무한 재시도 설명은 **조사 당시 코드**에 관한 기록입니다.
현재 제한과 사용 방법은 [README.md](README.md)를 참고하세요.
이 문서 이후 `korail-mobile-api`로 전환하여 실제 로그인·열차 조회를 확인했습니다.
최신 검증 범위는 [AUTH_MIGRATION.md](AUTH_MIGRATION.md)를 참고하세요.

## 결론

2026년 9월 1일 이후 기존 SRT 노선의 예매는 코레일+의 KTX 예매로 전환됐다.
현재 `reservation.py`는 SRT 전용 클라이언트이므로 역 목록만 합쳐서는 통합 열차를
예약할 수 있다고 볼 수 없다. Korail 클라이언트로의 전환과 인증·검색·예약·결제 검증이 필요하다.

통합 역 목록과 이를 적용하는 `train_cli.py`를 추가했다. 이후 메뉴도 통합해
운영사 선택 없이 Korail로 연결하고, `KTX만` 옵션을 없애 KTX 검색을 기본 적용했다.
실제 로그인, 열차 검색, 예약, 결제는 수행하지 않았다. 아래의 “현재 코드”는 설치된
소스를 확인한 결과이며, “전환 검토”는 실서버 호환성 검증 완료를 뜻하지 않는다.

## 공식적으로 확인된 운영 변경

- 통합 앱 코레일+는 2026-08-03 출시, 9월 운행 열차 예매는 08-05 10시 개시.
  [국토교통부 안내](https://tilit.molit.go.kr/atmo/USR/N0201/m_36515/dtl.jsp?id=95092278)
- 기관통합·통합운행은 2026-09-01 시작.
  [국토교통부 2026-08-31 발표](https://molit.go.kr/USR/NEWS/m_71/dtl.jsp?id=95092365)
- 기존 SRT 승차권은 08-31 운행분까지다. 09-01 이후 수서 출·도착 열차는
  코레일+ 또는 코레일 홈페이지에서 KTX로 예매한다. 통합회원 계정이 필요하며,
  두 회사에 모두 가입했던 회원의 통합회원번호·비밀번호는 기존 코레일 계정과 같다.
  [SR 통합 운영 Q&A, 2026-08-07](https://www.srail.or.kr/cms/article/view.do?pageId=TK0502000000&postNo=913)

이 공지들은 운영·판매 창구의 전환을 확인해 준다. 기존 SRT API의 모든 URL이
폐쇄됐다는 뜻이나, 새로운 공개 REST API 명세가 발표됐다는 뜻으로 해석하지 않았다.
확인한 공식 안내에서는 통합 이후 모바일 API의 요청·응답 명세를 찾지 못했다.

## 프로젝트 기준점

- 의존성: `srtgo @ git+https://github.com/lapis42/srtgo.git`
- 설치 버전: `2.2.3.dev4+gfaedd2145`
- 설치 메타데이터의 커밋: `faedd2145f6e6275d813519a1a1c125c68ef7ba3`
- 확인한 파일: `venv/Lib/site-packages/srtgo/{srtgo,srt,ktx}.py`
- upstream 저장소는 2025-09-24 보관 처리됐고 개발·지원 중단을 공지했다.
  따라서 재설치만으로 2026년 통합 대응이 해결된다고 기대할 수 없다.
  [srtgo upstream](https://github.com/lapis42/srtgo)

설치된 소스와 현재 upstream 파일에는 차이가 있어, 아래 표는 **로컬 설치본** 기준이다.

## 역 목록 변경

기존 SRT 32개 + KTX 32개 = 문자열만 중복 제거하면 46개다.
동일 역인 `김천(구미)` / `김천구미`를 Korail 표기인 `김천구미`로 맞추면 **45개**다.

`stations.py`는 통합 역 목록을 가나다순으로 정렬한다. 직접 추가한 역도 표시할 때 같은 순서로 정렬한다.
`train_cli.py`로 실행하면 통합 메뉴에 이 목록이 적용된다. 기존 KTX 즐겨찾기도
공백·빈 항목·별칭·중복을 정리해 읽고, 직접 추가한 역은 유지한다.

통합 메뉴에는 SRT 선택이 없다. 의존성 내부 SRT 클라이언트는 자체 `STATION_CODE`로
역명을 검증하므로 서울·용산 등 이름만 추가하면 요청 전 `ValueError`가 발생한다.
특히 SRT 코드표는 `김천(구미)`를 요구하므로 Korail 표시 이름을 그대로 넘길 수 없다.
역 코드 자체를 임의로 통합하거나 새로 만들어 넣지 않았다.

이 45개는 **기존 프로그램 선택지의 합집합**이다. 최신 전국 정차역 목록으로 인증한
데이터는 아니며, 역이 목록에 있다는 사실이 임의의 두 역 사이 직통 운행을 보장하지 않는다.

## 현재 클라이언트의 차이

| 항목 | 현재 SRT 코드 | 설치된 Korail 코드 / 전환 시 변경 |
|---|---|---|
| 생성자 | `SRT(srt_id=..., srt_pw=...)` | `Korail(korail_id=..., korail_pw=...)`, 통합회원 자격 확인 |
| 역 검색 입력 | `STATION_CODE`의 4자리 코드로 변환 | 역명을 `txtGoStart`, `txtGoEnd`에 전달 |
| 매진 열차 포함 | `available_only=False` | `include_no_seats=True` |
| 종료 시각 | `time_limit="123000"` | 해당 인자 없음. 반환 열차의 `dep_time`으로 상한 필터 필요 |
| 열차 종류 | 응답 중 `stlbTrnClsfCd == "17"`만 보존 | 기본 `TrainType.ALL="109"`; KTX 검색은 `TrainType.KTX="100"` |
| 잔여석 확인 | `train.seat_available()` | `train.has_seat()` |
| 승객·좌석 옵션 | `Adult`, `SeatType` | `AdultPassenger`, `ReserveOption` |
| 결제 키워드 | `number`, `password`, `validation_number`, `expire_date` | `card_number`, `card_password`, `birthday`, `card_expire` |
| 클라이언트 재초기화 | `SRT.clear()` 존재 | `Korail.clear()` 없음. 같은 파일의 `clear()`는 `NetFunnelHelper` 메서드 |

따라서 `from srtgo.srt import SRT` 한 줄만 Korail로 교체하면 검색·좌석 확인·결제 등이
실패한다. SRT의 `17` 필터는 KTX로 분류되는 통합 열차를 제거할 수 있다.
Korail 검색도 현재 `srtCheckYn="N"`이므로 통합 후 수서축 열차가 `100` 그룹으로
모두 조회되는지 확인해야 한다. 이 플래그를 `Y`로 바꾸는 것만으로 해결된다고 단정하지 않는다.

## HTTP 엔드포인트 비교

아래는 두 기존 구현의 주소 비교다. “통합 API의 검증된 새 주소” 표가 아니다.

SRT base: `https://app.srail.or.kr:443`

Korail base: `https://smart.letskorail.com:443/classes/com.korail.mobile`

| 기능 | SRT base 뒤 경로 | Korail base 뒤 경로 | 로컬 HTTP 방식 SRT / Korail |
|---|---|---|---|
| 로그인 | `/apb/selectListApb01080_n.do` | `.login.Login` | POST / POST |
| 검색 | `/ara/selectListAra10007_n.do` | `.seatMovie.ScheduleView` | POST / GET |
| 예약 | `/arc/selectListArc05013_n.do` | `.certification.TicketReservation` | POST / GET |
| 결제 | `/ata/selectListAta09036_n.do` | `.payment.ReservationPayment` | POST / POST |

SRT 검색은 `outDataSets.dsOutput1`을, Korail 검색은 `trn_infos.trn_info`를 해석한다.
예약·승차권 객체도 서로 다르므로 도메인 치환만으로 호환되지 않는다.

## 최근 모바일 API 구현과의 차이 및 미확인 사항

외부 구현자의 2026년 7월 코레일 앱 6.5.0 분석은 API `Version=250601003`을 기록한다.
로컬 `srtgo` Korail 값은 `240531001`이며 DynaPath 인증 헤더 처리가 없다.
해당 구현은 로그인에 `x-dynapath-m-token` 처리가 필요하다고 명시한다.
이는 **통합 전의 제3자 관측**이며, 9월 통합 후 요구값을 입증하지 않는다.
버전 문자열만 바꿔 인증이 해결된다고 볼 수 없다.
[korail-mobile-api 구현자 문서](https://github.com/yakisoba0728/korail-mobile-api)

같은 프로젝트의 API 분석은 `.seatMovie.ScheduleView`를 POST로 기록한다.
로컬 구현은 GET이므로 현재 수락되는 HTTP 방식·필수 필드·페이지 이동을 다시 확인해야 한다.
URL이 같아도 서버 호환성이 유지된다는 보장은 없다.
[API별 관측 기록](https://github.com/yakisoba0728/korail-mobile-api/blob/main/docs/api-status-by-service.md)

실제 전환을 완료하려면 다음 증거가 추가로 필요하다.

1. 통합 계정으로 로그인 성공 및 실패 응답·세션 만료 처리 확인.
2. 같은 운행일에 서울축과 수서축 검색 결과를 공식 앱과 대조해 열차 누락 여부 확인.
   응답 역 코드·열차 그룹·차종을 기준으로 매핑하고, 검색 결과 페이지도 확인.
3. 검색 어댑터의 매진 포함·종료 시각 필터·승객 타입·좌석 옵션 변환 검증.
4. 예약·결제 객체 및 인자 변환 검증. 결제 성공 반환값을 확인하고, 실패 시 예약 상태와
   결제 여부를 조회해 중복 예약·중복 결제를 막도록 처리.

기존 스크립트에는 과거 날짜 `20260110`이 설정돼 있고 검색 예외를 모두 재시도한다.
통합 API와 무관하게 이 설정으로는 정상 검색할 수 없다. 이 파일을 API 확인 목적으로
실행하면 자동 예약·결제 루프까지 이어지므로 이번 조사에서는 실행하지 않았다.

## 검증 범위

오프라인 테스트는 역의 별칭 중복 제거, 서로 다른 역 보존,
수서축 선택지, 기존·직접 입력 즐겨찾기, SRT 코드표 호환 유지, 클라이언트 로딩 없는
목록 출력 경로를 확인했다. 설치된 실제 `srtgo`에서도 원본 목록 일치, 통합 즐겨찾기,
메뉴 시작·종료를 확인했다(키링 조회·메뉴 선택은 mock 사용).
메뉴 통합에 대해서는 운영사 선택 없는 Korail 연결, KTX 검색 고정, 기존 승객 옵션 호환,
설정 취소 시 저장 방지 및 프로젝트 진입점 연결도 검증한다.
이는 로컬 목록·UI 연결 검증이며 실서버 예매 검증과 구분한다.
