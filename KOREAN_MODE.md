# TARS-AI Korean Mode

기존 TARS-AI를 한국어로 동작시키는 통합 가이드.

## 무엇이 바뀌었나 (이미 적용됨)

| 파일 | 변경 |
|---|---|
| `src/modules/module_melotts.py` | **새 파일** — MeloTTS-Korean 래퍼, `module_piper.py`와 동일한 async-generator 인터페이스 |
| `src/modules/module_tts.py` | `ttsoption=melotts_ko` 분기 추가 |
| `src/modules/module_main.py` | LLM 응답에서 한글 다 죽이던 ASCII-only regex → Unicode 보존 regex 로 교체 |
| `src/modules/module_stt.py` | `language="en"` 하드코딩 → `[STT] stt_language` 로 설정화 |
| `src/config.ini.template` | `stt_language`, `melotts_ko` 옵션, 한국어 systemprompt 가이드 추가 |
| `src/requirements.txt` | MeloTTS 설치 명령어 주석 추가 |

코드 수정 4줄 + 새 파일 1개. 영어 모드는 그대로 동작합니다.

## RPi5 셋업

### 1. 코드 받기

```bash
cd ~/TARS-AI
git pull
```

### 2. 한국어 TTS 추가 설치 (~3~5분, ~500MB 디스크)

```bash
source ~/tars-venv/bin/activate    # 본인 venv 경로에 맞게

pip install git+https://github.com/myshell-ai/MeloTTS.git
pip install g2pkk python-mecab-ko unidic-lite
python -m unidic download
```

### 3. config.ini 한국어 모드 설정

`src/config.ini` 에서:

```ini
[STT]
wake_word = 야 타스
stt_processor = faster-whisper
whisper_model = base
stt_language = ko

[LLM]
systemprompt = 너는 친근한 한국어 음성 비서다. 모든 답변은 반드시 한국어로만 짧게 한 두 문장으로 한다. 영어, 이모지, 특수문자는 절대 사용하지 않는다.

[TTS]
ttsoption = melotts_ko
```

### 4. 첫 실행

```bash
cd ~/TARS-AI/src
source ~/tars-venv/bin/activate
python app.py
```

- 첫 응답 시 MeloTTS-Korean 모델 ~250MB 자동 다운로드 (HuggingFace `myshell-ai/MeloTTS-Korean`)
- 이후 캐시되어 다음부터 즉시 로드

### 5. 영어/한국어 모드 토글

config.ini 5줄만 바꾸면 됨:

| 영어 모드 | 한국어 모드 |
|---|---|
| `ttsoption = piper` | `ttsoption = melotts_ko` |
| `stt_language = en` | `stt_language = ko` |
| `wake_word = hey tar` | `wake_word = 야 타스` |
| `whisper_model = tiny` | `whisper_model = base` |
| 영어 systemprompt | 한국어 systemprompt |

## 트러블슈팅

### `ERROR: failed to load MeloTTS Korean: No module named 'melo'`

```bash
source ~/tars-venv/bin/activate
pip install git+https://github.com/myshell-ai/MeloTTS.git
```

### 한국어 인식이 자꾸 영어로 transcribe됨

- `[STT] stt_language = ko` 가 config.ini 에 있는지 확인
- `whisper_model = base` 또는 `small` 로 키우기 (tiny는 한국어 거의 못 알아들음)

### LLM이 영어로 답함

`[LLM] systemprompt` 가 한국어로 답하라는 지시어인지 확인.

### 영어 모드로 돌아가고 싶음

`config.ini` 의 5줄만 원복하면 끝. 코드 수정은 영어 모드에 영향 없도록 짜둠.
