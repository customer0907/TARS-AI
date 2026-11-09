import sounddevice as sd
import subprocess
import os
import sys

def list_audio_devices():
    try:
        devices = sd.query_devices()
        print("Available audio devices:\n")
        for i, device in enumerate(devices):
            print(f"{i}: {device['name']} ({'Input' if device['max_input_channels'] > 0 else 'Output'})")
        return devices
    except Exception as e:
        print(f"Error querying audio devices: {e}")
        return None

def check_alsa_devices():
    try:
        result = subprocess.run(["arecord", "-l"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0:
            print("Error running 'arecord':", result.stderr)
            return None
        print("\nDetected devices from ALSA:\n")
        print(result.stdout)
        return result.stdout
    except FileNotFoundError:
        print("'arecord' command not found. Install ALSA utilities.")
        return None

def check_device_usage():
    try:
        result = subprocess.run(["lsof", "/dev/snd/*"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode == 0 and result.stdout:
            print("\nProcesses using audio devices:\n")
            print(result.stdout)
            return result.stdout
        else:
            print("\nNo processes are using audio devices.")
            return None
    except FileNotFoundError:
        print("'lsof' command not found. Install 'lsof' to check device usage.")
        return None

def check_user_permissions():
    try:
        groups = subprocess.check_output(["groups"], text=True).strip()
        if "audio" not in groups:
            print("User is not in the 'audio' group. Run 'sudo usermod -aG audio $USER' and log out and back in.")
            return False
        else:
            print("\nUser is in the 'audio' group.")
            return True
    except Exception as e:
        print(f"Failed to check user groups: {e}")
        return False

def test_audio_stream():
    try:
        with sd.InputStream(samplerate=16000, channels=1, dtype="int16", blocksize=8000, latency="high") as stream:
            print("\nSuccessfully opened audio stream.")
            return True
    except Exception as e:
        print(f"Failed to open audio stream: {e}")
        return False

def main():
    print("\nRunning USB Microphone Diagnostics...\n")

    print("Step 1: Listing audio devices...")
    devices = list_audio_devices()
    if devices is None:
        print("Failed to list audio devices. Exiting.")
        sys.exit(1)

    print("\nStep 2: Checking ALSA devices...")
    alsa_output = check_alsa_devices()
    if alsa_output is None:
        print("Failed to detect ALSA devices. Exiting.")
        sys.exit(1)

    print("\nStep 3: Checking device usage...")
    device_usage = check_device_usage()

    print("\nStep 4: Checking user permissions...")
    permissions_ok = check_user_permissions()
    if not permissions_ok:
        print("User does not have proper permissions. Exiting.")
        sys.exit(1)

    print("\nStep 5: Testing audio stream...")
    stream_ok = test_audio_stream()
    if not stream_ok:
        print("Audio stream test failed. Exiting.")
        sys.exit(1)

    print("\nAll tests completed. Your USB microphone setup appears to be functioning correctly.")

if __name__ == "__main__":
    main()
