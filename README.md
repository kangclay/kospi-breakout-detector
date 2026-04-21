# kospi-breakout-detector

KOSPI 매수 신호를 탐지하고, 전략을 백테스트해서, 최적 전략을 GitHub Actions 운영 신호로 연결하는 저장소입니다.

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

## 로컬 실행

```bash
pip install -r requirements.txt
python optimize_signals.py --market KOSPI --days 365 --limit 80
python analyze_surge_patterns.py --market KOSPI --days 365 --limit 20
python detector.py
```

최적화 후 생성 파일:

- [reports/strategy_ranking.csv](/Users/goen/projects/kospi-breakout-detector/reports/strategy_ranking.csv)
- [reports/best_strategy.json](/Users/goen/projects/kospi-breakout-detector/reports/best_strategy.json)

예시 파일:

- [reports/best_strategy.example.json](/Users/goen/projects/kospi-breakout-detector/reports/best_strategy.example.json)

## GitHub Actions

- [daily_run.yml](/Users/goen/projects/kospi-breakout-detector/.github/workflows/daily_run.yml)
  평일 스케줄로 `detector.py`를 실행합니다.
- [optimize.yml](/Users/goen/projects/kospi-breakout-detector/.github/workflows/optimize.yml)
  수동 실행으로 전략 최적화를 돌리고 `reports/` 결과를 아티팩트로 업로드합니다.

## Secrets

- `TELEGRAM_TOKEN`
- `TELEGRAM_CHAT_ID`
- `GSHEET_KEY`

## 주의

GitHub Actions가 최적 전략을 실제 운영에 쓰려면 `reports/best_strategy.json`을 커밋해 두거나, 최적화 결과를 받아 반영하는 별도 배포 흐름이 필요합니다.
