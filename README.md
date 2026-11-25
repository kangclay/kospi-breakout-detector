# kospi-breakout-detector

KOSPI/KOSDAQ 전체 시장에서 매일 매수 후보를 선별하고, 별도로 기술적 전략을 백테스트하는 저장소입니다.

## 미국주식 일일 스크리너

`us_screener.py`는 국내 모델과 완전히 분리된 Nasdaq-100 가격·유동성 스크리너입니다. Alpaca 무료 IEX 일봉을 읽기만 하며 주문 API는 호출하지 않습니다.

- Nasdaq 공식 구성종목(2026-11-02 기준)을 `data/us_nasdaq100.csv`에 고정해 재현성 확보
- 20/60/120/200일 모멘텀, 추세, 120일 고가 접근도, 변동성, 낙폭, 달러 거래대금으로 미국 전용 0~100점 산출
- QQQ의 60일선·200일선으로 미국 시장국면 판정
- 시장 ON에서 82점 이상이고 20일 고가 돌파·거래량 1.3배 확인 시 `US_BUY_CANDIDATE`, 확인 전은 `US_ENTRY_WATCH`
- 시장 OFF에서는 88점 이상도 `US_OFF_WATCH` 관찰 전용
- 실제 후보는 `us_recommendations`, 관찰 신호는 `us_observations` 탭에 기록하며 실제 추천은 14일간 중복 방지
- Telegram 메시지는 `🇺🇸 US Daily Price-Factor Screen`으로 국내 알림과 구분
- 미국 전용 점수는 국내 재무 멀티팩터 점수와 서로 비교할 수 없음

GitHub Actions는 미국장 마감 후인 평일 23:20 UTC(한국시간 다음 날 08:20)에 별도 실행됩니다. 저장소 Secret에 `ALPACA_API_KEY`, `ALPACA_API_SECRET`, 기존 `GSHEET_KEY`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`가 필요합니다.

로컬 실행:

```bash
python us_screener.py --notify --log-sheet
```

## 일일 운영 스크리너

`daily_screener.py`가 운영용 진입점입니다.

- KOSPI/KOSDAQ 시장 스냅샷에서 시가총액 1,000억원·당일/20일 평균 거래대금 20억원으로 유동성 필터
- 우선주·스팩·리츠는 별도 전략이 필요하므로 운영 유니버스에서 제외
- 최근 가격 이력에서 20/60/120/200일 모멘텀, 이동평균 정배열, 신고가 접근도, 변동성, 낙폭 계산
- PER/PBR/배당수익률 기반 가치 점수
- OpenDART에서 ROE·영업이익률·부채비율·매출/영업이익 증가율을 불러와 품질·성장 점수에 반영
- 필요하면 `data/fundamentals_latest.csv`를 연결해 OpenDART 대신 자체 재무 스냅샷 사용
- 시장 `ON`은 82점, `UNKNOWN`은 85점 이상·팩터 충족률 70% 이상을 요구하며, 75점 이상은 일반 관찰 후보(`WATCH`)로 남김
- 종목 조건 통과 후에도 20일 고가 돌파와 거래량 20일 평균의 1.3배를 확인해야 `BUY_CANDIDATE`; 확인 전에는 `ENTRY_WATCH`
- 시장 `OFF`에서는 88점·팩터 충족률 90% 이상도 `OFF_WATCH` 관찰 전용이며 실제 추천 탭과 타이밍 엔진에 전달하지 않음
- 동일 종목의 실제 추천은 14일 동안 다시 기록하지 않고, 관찰 신호는 `observations` 탭에서 종목당 한 행으로 갱신
- KOSPI/KOSDAQ 지수의 60일선·200일선으로 시장국면 필터
- KRX Data Marketplace Open API의 일별 전종목 OHLCV·시가총액·거래대금으로 유니버스와 가격 이력을 구성
- `reports/daily_screen.csv`와 `reports/daily_screen.json` 생성

로컬 실행:

```bash
python daily_screener.py --top-n 10
```

재현 가능한 특정 기준일 실행:

```bash
python daily_screener.py --as-of-date 20260710 --top-n 10
```

매수·관찰 기준은 실행 시 조정할 수 있습니다.

```bash
python daily_screener.py --min-score 82 --unknown-min-score 85 --off-watch-min-score 88 --watch-score 75 --min-factor-coverage 0.70
```

재무 스냅샷은 로컬 CSV를 우선하고, 파일이 없으면 `DART_API_KEY`로 OpenDART를 조회합니다. 공시 제출기한을 기준으로 해당 날짜에 이미 공개됐을 정기보고서만 사용하므로 미래 공시를 과거 추천에 섞지 않습니다. 자체 CSV는 `data/fundamentals_latest.csv.example`의 컬럼을 따르며, `available_date`가 기준일 이후인 행은 자동 제외됩니다.

GitHub Actions의 일일 운영은 재무 데이터가 없으면 실패하도록 설정했습니다. OpenDART에서 인증키를 발급받아 저장소의 `Settings → Secrets and variables → Actions → New repository secret`에서 이름을 `DART_API_KEY`로 등록하세요. 인증키는 채팅이나 코드에 직접 넣지 않습니다.

## 독립 매수·매도 타이밍 엔진

`trade_timing.py`는 기존 추천 선별과 분리되어, 한 종목의 일별 OHLCV에서 매수·대기·보유·청산 시점을 계산합니다. 자동 주문이나 기존 추천 목록 기록은 하지 않습니다.

- 매수: 20일 신고가 돌파와 거래량 확인(`BUY_NOW`), 또는 20일선 부근 상승 반전(`BUY_PULLBACK`)
- 리스크 관리: 최초 2 ATR과 최대 손실 6% 중 더 엄격한 스탑, 수익률 10% 이후 최고가 대비 8%와 3 ATR 중 더 엄격한 트레일링스탑
- 청산: 장중 스탑 도달은 스탑가, 갭하락은 시가로 계산

신규 진입 타이밍 확인:

```bash
python trade_timing.py --ticker 005930 --as-of-date 20260912
```

보유 포지션의 청산·보유 상태 확인:

```bash
python trade_timing.py --ticker 005930 --as-of-date 20260912 --entry-date 2026-09-01 --entry-price 70000
```

여러 추천 종목의 매수 시점은 `ticker` 또는 `티커` 열이 있는 CSV로 계산합니다.

```bash
python trade_timing.py --recommendations-csv recommendations.csv --as-of-date 20260912 --output reports/entry_timing.json
```

### GitHub Actions · Google Sheets · Telegram 연동

`Independent Trade Timing Engine` 워크플로는 `Daily Multi-Factor Screener`가 성공적으로 끝난 뒤 별도로 실행됩니다. 실제 `BUY_CANDIDATE`만 `recommendations` 탭을 통해 전달됩니다.

- `recommendations`: 기존 멀티팩터 추천의 읽기 전용 원본
- `observations`: `ENTRY_WATCH`·`OFF_WATCH`의 최근 상태를 종목당 한 행으로 갱신하는 관찰 전용 탭
- `타이밍엔진_보유입력`: 실제 보유 종목을 직접 입력하는 탭 (`진입일 | 티커 | 종목명(선택) | 진입가(원) | 메모(선택)`)
- `타이밍엔진_매수신호`: 최근 20일의 `daily_multifactor:` 추천만 대상으로 한 독립 매수 신호 자동 갱신 탭
- `타이밍엔진_보유신호`: 보유입력 탭을 대상으로 한 ATR 스탑/청산 신호 자동 갱신 탭

워크플로는 기존 `GSHEET_KEY`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` Secrets를 그대로 사용합니다. Telegram에는 `⏱️ 독립 매수·매도 타이밍`이라는 제목으로만 알림이 오며, 자동 주문은 절대 실행하지 않습니다.

Telegram API가 실패해도 스크리닝·시트 기록은 성공 처리됩니다. GitHub Actions 로그에는 토큰을 노출하지 않고 HTTP 상태와 Telegram 오류 설명만 남습니다.

로컬 실행:

```bash
python timing_runner.py --write-sheet --notify
```

실제 보유분의 매도 시점은 `ticker`·`entry_date`·`entry_price` 열(또는 `일자`·`티커`·`종가`)을 가진 CSV로 별도 관리합니다. 추천이 곧 매수가 되는 것은 아니므로, 보유분은 추천 목록과 분리하는 방식입니다.

```bash
python trade_timing.py --positions-csv positions.csv --as-of-date 20260912 --output reports/position_timing.json
```

GitHub Actions의 운영 경로는 KRX Data Marketplace Open API를 사용합니다. `KRX_OPEN_API_KEY`를 GitHub Actions Secret으로 등록하고, KRX에서 아래 서비스를 각각 이용 신청해야 합니다.

- 유가증권 일별매매정보 (`stk_bydd_trd`)
- 코스닥 일별매매정보 (`ksq_bydd_trd`)
- KOSPI 시리즈 일별시세정보 (`kospi_dd_trd`)
- KOSDAQ 시리즈 일별시세정보 (`kosdaq_dd_trd`)

일별 시세는 KRX가 확정한 거래일 데이터만 사용합니다. 따라서 장중 값이나 변경되기 쉬운 HTML 페이지를 추천 근거로 쓰지 않습니다. API 키가 없는 로컬 연구 실행에서는 기존 pykrx/Naver 경로가 호환용으로 남아 있지만, GitHub Actions 운영 실행은 키가 없으면 명확히 실패합니다.

## 흐름

1. `optimize_signals.py`
   여러 진입 프리셋, 손절폭, 최대 보유일, 거래량 배수를 백테스트합니다.
2. `analyze_surge_patterns.py`
   급등 사례를 정의하고 공통 특징과 새 quant surge 전략을 검증합니다.
3. `reports/best_strategy.json`
   운영용 최적 전략 결과를 저장합니다.
4. `detector.py`
   `best_strategy.json` 기반 신호와 `QUANT SURGE` 별도 섹션을 함께 탐지합니다.
   파일이 없으면 기존 돌파/추세 프리셋 알림으로 fallback 합니다.

## 연구용 백테스트

```bash
pip install -r requirements.txt
python optimize_signals.py --market KOSPI --days 365 --limit 80
python analyze_surge_patterns.py --market KOSPI --days 365 --limit 20
python detector.py
```

`optimize_signals.py`와 기존 `detector.py`는 연구·호환용으로 남겨두었습니다. 기존 최적화 결과는 여러 종목의 거래를 실제 동시 보유 포트폴리오로 계산하지 않으므로, 운영 매수 목록을 만드는 경로로 사용하지 않습니다.

최적화 후 생성 파일:

- [reports/strategy_ranking.csv](/Users/goen/projects/kospi-breakout-detector/reports/strategy_ranking.csv)
- [reports/best_strategy.json](/Users/goen/projects/kospi-breakout-detector/reports/best_strategy.json)

예시 파일:

- [reports/best_strategy.example.json](/Users/goen/projects/kospi-breakout-detector/reports/best_strategy.example.json)

## GitHub Actions

- [daily_run.yml](/Users/goen/projects/kospi-breakout-detector/.github/workflows/daily_run.yml)
  평일 스케줄로 `daily_screener.py`를 실행합니다.
- [us_daily_run.yml](/Users/goen/projects/kospi-breakout-detector/.github/workflows/us_daily_run.yml)
  미국장 마감 후 `us_screener.py`를 독립 실행합니다.
- [optimize.yml](/Users/goen/projects/kospi-breakout-detector/.github/workflows/optimize.yml)
  수동 실행으로 전략 최적화를 돌리고 `reports/` 결과를 아티팩트로 업로드합니다.

## Secrets

- `TELEGRAM_TOKEN`
- `TELEGRAM_CHAT_ID`
- `GSHEET_KEY`
- `KRX_OPEN_API_KEY` (필수: 일일 스크리너·독립 타이밍 엔진의 공식 시세 데이터)
- `DART_API_KEY` (필수: 일일 스크리너의 품질·성장 재무 팩터)
- `ALPACA_API_KEY` (필수: 미국주식 IEX 일봉 조회)
- `ALPACA_API_SECRET` (필수: 미국주식 IEX 일봉 조회)

## Google Sheets 기록

추천 종목은 Google Sheets의 `recommendations` 탭에 기록됩니다. 탭이 없으면 자동으로 만들고, `일자 | 티커 | 종목명 | 종가 | 전략` 순서로 남깁니다.

## 주의

GitHub Actions가 최적 전략을 실제 운영에 쓰려면 `reports/best_strategy.json`을 커밋해 두거나, 최적화 결과를 받아 반영하는 별도 배포 흐름이 필요합니다.
