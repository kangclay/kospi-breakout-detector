# kospi-breakout-detector

KOSPI/KOSDAQ 전체 시장에서 매일 매수 후보를 선별하고, 별도로 기술적 전략을 백테스트하는 저장소입니다.

## 일일 운영 스크리너

`daily_screener.py`가 운영용 진입점입니다.

- KOSPI/KOSDAQ 시장 스냅샷에서 시가총액·거래대금으로 1차 유동성 필터
- 최근 가격 이력에서 20/60/120/200일 모멘텀, 이동평균 정배열, 신고가 접근도, 변동성, 낙폭 계산
- PER/PBR/배당수익률 기반 가치 점수
- 선택적으로 `data/fundamentals_latest.csv`를 연결해 ROE/ROIC/마진/성장률/부채비율을 점수에 반영
- 기본 매수 기준은 점수 82점 이상·팩터 충족률 70% 이상이며, 75점 이상은 관찰 후보(`WATCH`)로 남김
- KOSPI/KOSDAQ 지수의 60일선·200일선으로 시장국면 필터
- KRX 계정 환경변수가 없으면 Naver 공개 시총·지수 페이지를 유니버스/국면 조회의 fallback으로 사용하고, 개별 종목 과거 OHLCV는 pykrx로 조회
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
python daily_screener.py --min-score 82 --watch-score 75 --min-factor-coverage 0.70
```

재무 스냅샷은 `data/fundamentals_latest.csv.example`의 컬럼을 따릅니다. `available_date`가 기준일 이후인 행은 자동으로 제외합니다. 파일이 없으면 가격·가치 중심으로 실행되고 결과에 `재무 스냅샷: 미사용`으로 표시됩니다.

## 독립 매수·매도 타이밍 엔진

`trade_timing.py`는 기존 추천 선별과 분리되어, 한 종목의 일별 OHLCV에서 매수·대기·보유·청산 시점을 계산합니다. 자동 주문이나 기존 추천 목록 기록은 하지 않습니다.

- 매수: 20일 신고가 돌파와 거래량 확인(`BUY_NOW`), 또는 20일선 부근 상승 반전(`BUY_PULLBACK`)
- 리스크 관리: 최초 2.5 ATR 손절, 1R 수익 이후 최고가 기준 3 ATR 트레일링스탑
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

`Independent Trade Timing Engine` 워크플로는 `Daily Multi-Factor Screener`가 성공적으로 끝난 뒤 별도로 실행됩니다. 기존 멀티팩터 추천의 Telegram 메시지와 `recommendations` 탭은 수정하지 않습니다.

- `recommendations`: 기존 멀티팩터 추천의 읽기 전용 원본
- `타이밍엔진_보유입력`: 실제 보유 종목을 직접 입력하는 탭 (`진입일 | 티커 | 종목명(선택) | 진입가(원) | 메모(선택)`)
- `타이밍엔진_매수신호`: 최근 20일의 `daily_multifactor:` 추천만 대상으로 한 독립 매수 신호 자동 갱신 탭
- `타이밍엔진_보유신호`: 보유입력 탭을 대상으로 한 ATR 스탑/청산 신호 자동 갱신 탭

워크플로는 기존 `GSHEET_KEY`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` Secrets를 그대로 사용합니다. Telegram에는 `⏱️ 독립 매수·매도 타이밍`이라는 제목으로만 알림이 오며, 자동 주문은 절대 실행하지 않습니다.

로컬 실행:

```bash
python timing_runner.py --write-sheet --notify
```

실제 보유분의 매도 시점은 `ticker`·`entry_date`·`entry_price` 열(또는 `일자`·`티커`·`종가`)을 가진 CSV로 별도 관리합니다. 추천이 곧 매수가 되는 것은 아니므로, 보유분은 추천 목록과 분리하는 방식입니다.

```bash
python trade_timing.py --positions-csv positions.csv --as-of-date 20260912 --output reports/position_timing.json
```

KRX 직접 조회를 사용하려면 GitHub Actions Secrets에 `KRX_ID`, `KRX_PW`를 추가할 수 있습니다. 기본값은 별도 KRX 계정 없이도 동작하도록 Naver fallback을 사용합니다. Naver fallback은 현재 시총 표를 사용하므로 과거 기준일을 엄격히 재현하는 백테스트 데이터 소스로 사용하지 않습니다.

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
- [optimize.yml](/Users/goen/projects/kospi-breakout-detector/.github/workflows/optimize.yml)
  수동 실행으로 전략 최적화를 돌리고 `reports/` 결과를 아티팩트로 업로드합니다.

## Secrets

- `TELEGRAM_TOKEN`
- `TELEGRAM_CHAT_ID`
- `GSHEET_KEY`
- `KRX_ID` / `KRX_PW` (선택사항; 없으면 Naver fallback)

## Google Sheets 기록

추천 종목은 Google Sheets의 `recommendations` 탭에 기록됩니다. 탭이 없으면 자동으로 만들고, `일자 | 티커 | 종목명 | 종가 | 전략` 순서로 남깁니다.

## 주의

GitHub Actions가 최적 전략을 실제 운영에 쓰려면 `reports/best_strategy.json`을 커밋해 두거나, 최적화 결과를 받아 반영하는 별도 배포 흐름이 필요합니다.
