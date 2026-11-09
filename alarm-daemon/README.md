# TARS Alarm Daemon

> TARS 음성 앱이 꺼져 있어도 알람이 울리도록 분리한 상주 데몬. 표준 라이브러리만 사용.

## 동작
- 일회성 알람 저장·복원 (`~/.local/state/tars-alarm`) → 재부팅 후에도 유지
- 놓친 알람은 유예 시간(기본 15분) 안이면 늦게라도 울림
- 문장 알람: Gemini TTS로 합성해 말로 깨움, 실패하면 비프음
- HTTP API (`127.0.0.1:8765`)
  - TARS 본체의 클라이언트: [`src/modules/module_alarm.py`](../src/modules/module_alarm.py) — 음성으로 받은 알람 요청("7시에 깨워줘")을 이 API로 등록·취소
- systemd user 서비스로 자동 재시작

## 사용
```bash
python3 tars_alarm.py add 07:00 -m "일어나, 7시야"   # 말로 깨우기
python3 tars_alarm.py add +30 라면                   # 30분 뒤
python3 tars_alarm.py list
python3 tars_alarm.py stop                          # 울리는 알람 끄기
```

## 설치
```bash
mkdir -p ~/tars-alarm && cp tars_alarm.py ~/tars-alarm/
cp tars-alarm.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now tars-alarm
```
- 환경 변수: `TARS_ALARM_PORT`, `TARS_ALARM_SOUND`, `TARS_ALARM_VOICE`, `TARS_ALARM_ENV_FILE`(`GEMINI_API_KEY`가 든 .env 경로)
