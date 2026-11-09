Put ElevenLabs yooni wake clips here (or src/yooni_wakeword/ — both are searched).

Required files (see manifest.json):
  나 불렀어.mp3
  또 왜.mp3
  말해.mp3          (txt line is "말해~")
  어 듣고있어.mp3
  왜불러.mp3        (txt line is "왜불러~")
  웅.mp3            (txt line is "웅?")
  웅 왜.mp3

manifest.json binds each display text to its audio file.
On startup you should see:
  LOAD: wake '웅 왜?' -> 웅 왜.mp3

If print says "웅 왜?" but audio is wrong, rename the mp3 or fix manifest.json.
