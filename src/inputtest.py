import sounddevice as sd

try:
    print("🔍 Available devices:")
    print(sd.query_devices())

    # 사용 가능한 input 장치 자동 선택
    default_input = sd.default.device[0]  # input index
    device_info = sd.query_devices(default_input, 'input')
    samplerate = int(device_info['default_samplerate'])

    print(f"\nO. Testing InputStream with device {default_input} and samplerate {samplerate}")

    with sd.InputStream(device=default_input, samplerate=samplerate, channels=1, dtype='int16'):
        print("InputStream Success!")
except Exception as e:
    print(f"Error: {e}")
