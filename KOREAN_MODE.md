# TARS-AI Korean Mode

ê¸°ì¡´ TARS-AIë¥??œêµ­?´ë¡œ ?™ì‘?œí‚¤???µí•© ê°€?´ë“œ.

## ë¬´ì—‡??ë°”ë€Œì—ˆ??(?´ë? ?ìš©??

| ?Œì¼ | ë³€ê²?|
|---|---|
| `src/modules/module_melotts.py` | **???Œì¼** ??MeloTTS-Korean ?˜í¼, `module_piper.py`?€ ?™ì¼??async-generator ?¸í„°?˜ì´??|
| `src/modules/module_tts.py` | `ttsoption=melotts_ko` ë¶„ê¸° ì¶”ê? |
| `src/modules/module_main.py` | LLM ?‘ë‹µ?ì„œ ?œê? ??ì£½ì´??ASCII-only regex ??Unicode ë³´ì¡´ regex ë¡?êµì²´ |
| `src/modules/module_stt.py` | `language="en"` ?˜ë“œì½”ë”© ??`[STT] stt_language` ë¡??¤ì •??|
| `src/config.ini.template` | `stt_language`, `melotts_ko` ?µì…˜, ?œêµ­??systemprompt ê°€?´ë“œ ì¶”ê? |
| `src/requirements.txt` | MeloTTS ?¤ì¹˜ ëª…ë ¹??ì£¼ì„ ì¶”ê? |

ì½”ë“œ ?˜ì • 4ì¤?+ ???Œì¼ 1ê°? ?ì–´ ëª¨ë“œ??ê·¸ë?ë¡??™ì‘?©ë‹ˆ??

## RPi5 ?‹ì—…

### 1. ì½”ë“œ ë°›ê¸°

```bash
cd ~/TARS`ngit pull origin main`ngit pull
```

### 2. ?œêµ­??TTS ì¶”ê? ?¤ì¹˜ (~3~5ë¶? ~500MB ?”ìŠ¤??

```bash
source ~/tars-venv/bin/activate    # ë³¸ì¸ venv ê²½ë¡œ??ë§ê²Œ

pip install git+https://github.com/myshell-ai/MeloTTS.git
pip install g2pkk python-mecab-ko unidic-lite
python -m unidic download
```

### 3. config.ini ?œêµ­??ëª¨ë“œ ?¤ì •

`src/config.ini` ?ì„œ:

```ini
[STT]
wake_word = ???€??
stt_processor = faster-whisper
whisper_model = base
stt_language = ko

[LLM]
systemprompt = ?ˆëŠ” ì¹œê·¼???œêµ­???Œì„± ë¹„ì„œ?? ëª¨ë“  ?µë??€ ë°˜ë“œ???œêµ­?´ë¡œë§?ì§§ê²Œ ????ë¬¸ì¥?¼ë¡œ ?œë‹¤. ?ì–´, ?´ëª¨ì§€, ?¹ìˆ˜ë¬¸ì???ˆë? ?¬ìš©?˜ì? ?ŠëŠ”??

[TTS]
ttsoption = melotts_ko
```

### 4. ì²??¤í–‰

```bash
cd ~/TARS/src
source ~/tars-venv/bin/activate
python app.py
```

- ì²??‘ë‹µ ??MeloTTS-Korean ëª¨ë¸ ~250MB ?ë™ ?¤ìš´ë¡œë“œ (HuggingFace `myshell-ai/MeloTTS-Korean`)
- ?´í›„ ìºì‹œ?˜ì–´ ?¤ìŒë¶€??ì¦‰ì‹œ ë¡œë“œ

### 5. ?ì–´/?œêµ­??ëª¨ë“œ ? ê?

config.ini 5ì¤„ë§Œ ë°”ê¾¸ë©???

| ?ì–´ ëª¨ë“œ | ?œêµ­??ëª¨ë“œ |
|---|---|
| `ttsoption = piper` | `ttsoption = melotts_ko` |
| `stt_language = en` | `stt_language = ko` |
| `wake_word = hey tar` | `wake_word = ???€?? |
| `whisper_model = tiny` | `whisper_model = base` |
| ?ì–´ systemprompt | ?œêµ­??systemprompt |

## ?¸ëŸ¬ë¸”ìŠˆ??

### `ERROR: failed to load MeloTTS Korean: No module named 'melo'`

```bash
source ~/tars-venv/bin/activate
pip install git+https://github.com/myshell-ai/MeloTTS.git
```

### ?œêµ­???¸ì‹???ê¾¸ ?ì–´ë¡?transcribe??

- `[STT] stt_language = ko` ê°€ config.ini ???ˆëŠ”ì§€ ?•ì¸
- `whisper_model = base` ?ëŠ” `small` ë¡??¤ìš°ê¸?(tiny???œêµ­??ê±°ì˜ ëª??Œì•„?¤ìŒ)

### LLM???ì–´ë¡??µí•¨

`[LLM] systemprompt` ê°€ ?œêµ­?´ë¡œ ?µí•˜?¼ëŠ” ì§€?œì–´?¸ì? ?•ì¸.

### ?ì–´ ëª¨ë“œë¡??Œì•„ê°€ê³??¶ìŒ

`config.ini` ??5ì¤„ë§Œ ?ë³µ?˜ë©´ ?? ì½”ë“œ ?˜ì •?€ ?ì–´ ëª¨ë“œ???í–¥ ?†ë„ë¡?ì§œë‘ .
