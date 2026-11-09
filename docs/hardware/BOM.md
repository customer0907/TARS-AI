# TARS BoM (Bill of Materials)

원본 스프레드시트: [`TARS_BoM.xlsx`](TARS_BoM.xlsx) · GPIO 핀맵: [`rpi5_gpio_pinout.png`](../images/rpi5_gpio_pinout.png)

## v1 — Raspberry Pi 5 단독형 (2025)

| 범주 | 부품 | 용도 | 수량 |
|---|---|---|---|
| 3D 프린팅 | Ender 3 V3 SE, TPU·PETG 필라멘트 | 외형(PETG), 다리 그립(TPU) | 1 |
| 핵심 | Raspberry Pi 5 (4GB/8GB) | 메인 컴퓨터 | 1 |
| 핵심 | OSOYOO 3.5" HDMI LCD | 얼굴 디스플레이 | 1 |
| 핵심 | PCA9685 16ch 12bit PWM 드라이버 | 서보 제어 (I2C) | 1 |
| 전원 | 12V→6V 벅 컨버터, USB 벅 컨버터(>5A) ×2 | 서보 / Pi 전원 | 3 |
| 전원 | LiPo 11.1V 3S 2200mAh 50C, IMAX B6AC 충전기 | 배터리 | 1 |
| 구동 | MG996R 서보 ×4, SG90 마이크로 서보 | 다리 / 손 | 5 |
| 기구 | 608 베어링, 스프링, 금속 로드 5mm·3mm, M4 링키지 | 관절 | - |
| 오디오 | 미니 USB 마이크, MAX98357A I2S 앰프, 3W 8Ω 스피커 | 음성 입출력 | 각 1 |
| 카메라 | OV5647 + 15→22핀 리본 케이블 | 비전 | 1 |
| 체결 | M3 볼트 키트, M3 무두볼트, M2.x 태핑 나사 | 조립 | - |

| 구매처 | 금액 |
|---|---|
| 국내 | 385,300원 |
| AliExpress | 279,400원 |
| 디바이스마트 | 92,300원 |
| **합계** | **757,000원** |

### v1의 한계
- 미니 USB 마이크: 샘플레이트 호환성 문제(-9997), 수음 거리 짧음
- MAX98357A + 3W 스피커: 음량·음질 부족, 마이크로 에코 재유입 → full-duplex 불가
- 전원: 긴 USB 연장선·얇은 서보 전선으로 전압 강하 사고 ([트러블슈팅 #5, #6](../TROUBLESHOOTING.md))

## v2 — 오디오 재설계 (2026)

| 변경 | v1 | v2 | 이유 |
|---|---|---|---|
| 마이크·스피커 | 미니 USB 마이크 + MAX98357A + 3W 스피커 | **eMeet OfficeCore M0 Plus** USB 스피커폰 (약 8만 원) | 스피커폰급 수음·출력, 에코 제거로 full-duplex 대화 |
| 에코 처리 | 없음 | PulseAudio `module-echo-cancel` (WebRTC) | 자기 목소리 재유입 차단 |
| 음성 처리 | Pi 로컬 | 홈서버 `tars.service` + Android 폰 브리지 | 연산 부하 분리, 원격 대화 |

> TODO(사용자 확인): v2 전체 부품 목록·가격, 로봇 본체(Pi)와 홈서버 역할 분담 확정
