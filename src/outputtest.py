import sounddevice as sd
import numpy as np

# 100Hz 짧은 사인파 출력 테스트
samplerate = 44100
duration = 1.0
t = np.linspace(0, duration, int(samplerate * duration), endpoint=False)
mono_tone = 0.1 * np.sin(2 * np.pi * 100 * t).astype(np.float32)

# 스테레오 (2채널)로 복제
stereo_tone = np.column_stack([mono_tone, mono_tone])

try:
    sd.play(mono_tone, samplerate=samplerate, device=0)
    sd.wait()
    print("sound output success!")
except Exception as e:
    print(f"Error occured: {e}")
