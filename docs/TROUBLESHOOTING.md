# TARS 트러블슈팅 기록

> 원본: 개인 Notion 개발 로그 (2025-04 ~ 2026-11). 접속 정보·비밀번호는 제거했습니다.
> 형식: **문제 → 원인 → 시도 → 해결 → 결과/교훈**

## 요약

| # | 분류 | 문제 | 근본 원인 | 해결 |
|---|---|---|---|---|
| 1 | 디스플레이 | 3.5" HDMI LCD 해상도가 맞지 않음 | Bookworm KMS가 800×480 미만 해상도를 강제 무시 | 640×480 + UI 스케일 조정 |
| 2 | 디스플레이 | 터치 입력 불가 | Bookworm에서 LCD-show 터치 드라이버 비활성화 | `ads7846` dtoverlay 수동 설정 |
| 3 | 오디오 | `PaErrorCode -9997` Invalid Sample Rate | pocketsphinx 패키지 내부가 16kHz를 하드코딩 | 패키지 코드 44.1kHz로 수정, TTS 48kHz 고정 |
| 4 | 오디오 | `-9985` Device Unavailable / `-9998` 채널 수 | 출력 채널 0인 장치로 비프음 출력 시도, 장치 점유 | 인디케이터 비활성화, `.asoundrc` 정리, 점유 프로세스 정리 |
| 5 | 전원 | LCD 빨간 LED만 켜지고 Pi 재부팅 | 1.5m 저품질 USB 연장선 전압 강하 | 짧고 굵은 케이블로 교체 |
| 6 | 전원 | PCA9685 전원선에서 "펑" 소리, 배터리 전압 요동 | SG90용 얇은 전선(0.3~0.5A)을 메인 전원 연장에 사용 | 얇은 전선 제거, 전원 배선 재구성 |
| 7 | 기구 | LCD 화면이 계속 꺼짐 | 3D 프린팅 케이스가 LCD 뒷면 PWR/BKL 버튼을 누름 | 케이스 간섭 제거 |
| 8 | 오디오 HW | 소형 마이크·3W 스피커 품질 한계, full-duplex 불가 | Pi용 소형 부품의 수음·출력 품질 부족, 에코 | **eMeet OfficeCore M0 Plus**로 BoM 재설계 + WebRTC AEC |
| 9 | 오디오 HW | eMeet USB 재열거 후 소리·마이크 사라짐 | USB autosuspend, 죽은 장치에 물린 echo-cancel 모듈 | udev 규칙 + 재적용 서비스 + usbreset |

---

## 1. LCD 해상도 (OSOYOO 3.5" HDMI, 480×320)
- **문제**: 화면이 정상 비율로 나오지 않음
- **원인**: Raspberry Pi 5 기본 OS(Debian 12 Bookworm)는 KMS 드라이버가 최소 800×480을 강제하고, `config.txt`의 해상도 지정으로 덮어쓸 수 없음
- **검토한 대안**: DSI 디스플레이 재구매 / Pi 4 + Bullseye 다운그레이드 → 비용·주변기기 호환성 문제로 기각
- **해결**: KMS 유지, `hdmi_mode` 중 원 패널 비율(1.5:1)에 가장 가까운 최소값 **640×480** 적용, Xresources·LXTerminal 글꼴/아이콘 크기 확대
- **교훈**: 하드웨어 구매 전 OS 버전별 드라이버 지원 범위 확인

## 2. 터치 미동작
- **원인**: Bookworm에서 제조사 LCD-show 스크립트의 터치 드라이버가 비활성화됨
- **해결**: `/boot/firmware/config.txt`에 직접 설정
  ```ini
  dtparam=spi=on
  dtoverlay=ads7846,penirq=25,penirq_pull=2,speed=50000,cs=1,xmin=200,xmax=3900,ymin=200,ymax=3900,xohms=100
  ```

## 3~4. 마이크·스피커 스트림 오류 (3차에 걸친 디버깅)
| 에러 | 증상 | 원인 | 조치 | 결과 |
|---|---|---|---|---|
| `-9997` Invalid Sample Rate | wake word 감지 시 `RawInputStream` 열기 실패 | 장치가 44.1kHz 미지원 또는 PipeWire가 점유 | samplerate·`hw:3,0`·`int16` 지정, PipeWire/Pulse 소켓까지 종료, `arecord --dump-hw-params` 확인 | 1·2차 실패 |
| `-9998` Invalid Number of Channels | 비프음 `sd.play()` 시 실패 | `max_output_channels=0` 장치로 출력 | `use_indicators=False` | 해결 |
| `-9985` Device Unavailable | `OutputStream` 열기 실패 | MAX98357A가 다른 TARS 프로세스에 점유, ALSA 기본 장치 오설정 | `fuser -v /dev/snd/*`로 점유 확인, `.asoundrc` 기본 장치 제거 | 해결 |
| Segmentation fault | PortAudio 크래시 | 지원하지 않는 샘플레이트 강제 → NULL stream 참조 | blocksize·latency 조정 | 3차에서 근본 해결 |

- **3차 근본 원인**: `pocketsphinx/__init__.py` 내부가 wake word 감지 시 **16kHz를 하드코딩**해서 열고 있었음 → 앱 코드만 보고는 찾을 수 없었음
- **해결**: 패키지 코드 44.1kHz로 수정, `module_tts.py`의 재생 samplerate를 스피커 지원값 **48kHz**로 고정
- **교훈**: 앱 설정이 맞는데도 에러가 나면 서드파티 패키지 내부 기본값까지 추적

## 5. LCD 빨간 LED + Pi 강제 재부팅 (2025-11-23 ~ 25)
- **증상**: LCD 백라이트 꺼짐, Pi에서 작은 "펑" 소리 후 전원 차단, 디스플레이 설정 유실. 유선 어댑터에서는 정상
- **원인**: LiPo → Buck → Pi 구간의 **1.5m 저가 USB 연장선**(겉은 굵지만 내부 전력선이 얇음) → 부팅 시 1.2~2.5A 피크에서 전압 강하
- **해결**: 연장선 제거, 짧은 전원 경로로 교체
- **교훈**: Pi 5는 5V/5A급 피크 전류. 케이블 AWG·길이를 BoM 단계에서 명시

## 6. PCA9685 전원선 "펑" + 배터리 전압 요동 (2025-11-25 ~ 26)
- **증상**: 전압 8~15V 요동, LiPo 7.7V까지 하락, Pi·LCD·PCA 동시 불안정
- **원인**: SG90 서보용 얇은 전선(0.3~0.5A)을 벅컨버터 메인 출력 연장에 사용 → 서보 순간 3~5A 부하에서 발열·전압 강하·단락
- **사고 흐름**: 과부하 → 단락 → PCA9685 0V → 배터리 순간 드랍 → 벅 출력 불안정 → Pi/LCD 리셋 → SD 쓰기 중 설정 손상
- **해결**: 얇은 전선·납땜 부위 제거, 배터리 셀 밸런스 점검(IMAX B6 Cell Check)

## 7. 결론 정정 — LCD 꺼짐의 실제 원인 (2025-11-26)
- 전원 문제를 해결한 뒤에도 화면이 꺼짐 → **3D 프린팅 케이스가 LCD 뒷면 PWR/BKL 버튼을 누르고 있었음**
- **교훈**: 여러 증상이 동시에 나타날 때 하나씩 분리해서 재현하기. 전기 문제로 단정하기 전에 기구 간섭부터 확인

## 8. 오디오 하드웨어 재설계 (Pi 소형 부품 → eMeet)
- **문제**: Pi 5용 미니 USB 마이크 + MAX98357A + 3W 스피커 조합은 수음 거리·음질이 부족하고, 말하는 도중 끼어들기(full-duplex)가 안 됨(자기 목소리가 다시 들어감)
- **해결**: 약 8만 원대 USB 스피커폰 **eMeet OfficeCore M0 Plus**로 교체해 BoM 재설계
  - 하드웨어 AEC·노이즈 억제 + PulseAudio `module-echo-cancel`(WebRTC)
  - Gemini Live 음성 세션의 full-duplex(끼어들기)를 목표로 설계 (2026-07 Phase 0 실험 커밋, 동작 수준은 사용자 확인 필요)
- 상세: [`hardware/BOM.md`](hardware/BOM.md)

## 9. eMeet USB 재열거 후 오디오 실종
- **원인**: USB autosuspend로 장치가 절전에 들어간 뒤 깨어나지 못하거나, 재열거 후에도 echo-cancel 모듈이 죽은 장치(master)에 물린 채 남아 재적재되지 않음
- **해결** ([home-iot](https://github.com/customer0907/home-iot) `configs/emeet-fix/`)
  - udev: eMeet과 상위 USB 허브의 autosuspend 금지, 재열거 시 `emeet-reapply.service` 자동 실행
  - `tars-audio-setup.sh`: 싱크·소스가 **둘 다** 나타날 때까지 대기 → 좀비 echo-cancel 모듈 제거 → 재적재
  - `usbreset`으로 USB 스택 하드 리셋 스크립트
- **교훈**: "존재 여부"만 확인하던 로직을 "정상 장치에 연결됐는지"로 바꿔야 자동 복구가 됨

---

## 개발 타임라인
| 시작 | 작업 |
|---|---|
| 2025-04-03 | SD카드 OS 설치, Pi 구성 문서화 |
| 2025-06-17 | Raspberry Pi 5 셋업 |
| 2025-06-19 | 스피커 연결 테스트 |
| 2025-06-20 | Home Assistant 연결, API 키 연동 |
| 2025-06-30 | 3D 프린팅 |
| 2025-07-24 | LCD 터미널 설정 |
| 2025-08-25 | 카메라 연동 |
| 2025-10-31 | 서보 연결 및 PWM 코드 수정 |
| 2025-11-23 | 전원·LCD 장애 분석 (#5~#7) |
| 2025-12-12 | 블루투스 키보드 연동 |
| 2026-07 | Gemini Live 음성, 웹 검색, Discord·MQTT 음성 알림 |
| 2026-09 | eMeet 오디오 안정화 |
| 2026-10~11 | 알람 데몬, Android 폰 브리지, full-duplex 실험 |
