"""
module_engine.py

Core module for TARS-AI responsible for:
- Predicting user intents and determining required modules.
- Executing tool-specific functions like web searches, vision analysis, and volume control.

This is achieved using a pre-trained Naive Bayes classifier and TF-IDF vectorizer.
"""
# MIT License
# 
# Copyright (c)
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

# === Standard Libraries ===
import os
import joblib
from datetime import datetime
import threading
import socket
import re

# === Custom Modules ===
from modules.module_websearch import search_google, search_google_news
from modules.module_weather import get_weather
from modules.module_vision import describe_camera_view
from modules.module_stablediffusion import generate_image
from modules.module_volume import handle_volume_command
from modules.module_homeassistant import send_prompt_to_homeassistant
from modules.module_tts import generate_tts_audio
from modules.module_config import load_config, update_character_setting
from modules.module_messageQue import queue_message

# === Constants ===
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # Move up to "src"

CONFIG = load_config()

# === Language-aware NB model / training data paths ===
# English ('en') keeps the original filenames untouched, so reverting to English
# requires no retraining. Other languages get a "<name>_<lang>" suffix and their
# own training CSV + pickle files. Missing pickles are trained on first run below.
LANGUAGE = CONFIG['CHAR'].get('language', 'en')
if LANGUAGE == 'en':
    MODEL_FILENAME = os.path.join(BASE_DIR, 'engine/pickles/naive_bayes_model.pkl')
    VECTORIZER_FILENAME = os.path.join(BASE_DIR, 'engine/pickles/module_engine_model.pkl')
    TRAINING_DATA_PATH = os.path.join(BASE_DIR, 'engine/training/training_data.csv')
else:
    MODEL_FILENAME = os.path.join(BASE_DIR, f'engine/pickles/naive_bayes_model_{LANGUAGE}.pkl')
    VECTORIZER_FILENAME = os.path.join(BASE_DIR, f'engine/pickles/module_engine_model_{LANGUAGE}.pkl')
    TRAINING_DATA_PATH = os.path.join(BASE_DIR, f'engine/training/training_data_{LANGUAGE}.csv')

#mode to use Naive Bayes or LLM for function calling
mode = "NB"

# === Load Models ===
try:
    if not os.path.exists(VECTORIZER_FILENAME):
        raise FileNotFoundError("Vectorizer file not found.")
    if not os.path.exists(MODEL_FILENAME):
        raise FileNotFoundError("Model file not found.")
    nb_classifier = joblib.load(MODEL_FILENAME)
    tfidf_vectorizer = joblib.load(VECTORIZER_FILENAME)

except FileNotFoundError as e:
    # Attempt to train models if files are missing (language-aware paths)
    import module_engineTrainer
    module_engineTrainer.train_text_classifier(
        training_data_path=TRAINING_DATA_PATH,
        nb_classifier_path=MODEL_FILENAME,
        vectorizer_path=VECTORIZER_FILENAME,
        language=LANGUAGE,
    )
    try:
        nb_classifier = joblib.load(MODEL_FILENAME)
        tfidf_vectorizer = joblib.load(VECTORIZER_FILENAME)
    except Exception as retry_exception:
        raise RuntimeError("Critical error while loading models.") from retry_exception

def is_internet_available(host="8.8.8.8", port=53, timeout=2):
    """
    Checks whether the device has internet connectivity.
    Default host is Google DNS (8.8.8.8).
    """
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except Exception:
        return False

# === Functions ===
def execute_movement(movement, times):
    """
    Executes the specified movement in a separate thread.
    """
    def movement_task():
        queue_message(f"[DEBUG] Thread started for movement: {movement} x {times}")
        from module_btcontroller import turnRight, turnLeft, poseaction, unposeaction, stepForward
        
        action_map = {
            "turnRight": turnRight,
            "turnLeft": turnLeft,
            "poseaction": poseaction,
            "unposeaction": unposeaction,
            "stepForward": stepForward,
        }
        
        try:
            action_function = action_map.get(movement)
            if callable(action_function):
                for i in range(int(times)):
                    queue_message(f"[DEBUG] Executing {movement}, iteration {i + 1}/{times}")
                    action_function()  # Blocking for this thread
            else:
                queue_message(f"[ERROR] Movement '{movement}' not found in action_map.")
        except Exception as e:
            queue_message(f"[ERROR] Unexpected error while executing movement: {e}")
        finally:
            queue_message(f"[DEBUG] Thread completed for movement: {movement} x {times}")

    # Start the thread
    thread = threading.Thread(target=movement_task, daemon=True)
    thread.start()
    return thread  # Return the thread object if needed

def movement_llmcall_offline(user_input):
    """Offline fallback version of movement parser."""
    from module_btcontroller import stepForward, turnLeft, turnRight, poseaction, unposeaction
    import re
    text = user_input.lower()
    times = 1

    m = re.search(r'(\d+)\s*(times|x)', text)
    if m:
        times = max(1, int(m.group(1)))

    if "step forward" in text or "walk forward" in text:
        for _ in range(times): stepForward()
        return True
    elif "turn left" in text:
        for _ in range(times): turnLeft()
        return True
    elif "turn right" in text:
        for _ in range(times): turnRight()
        return True
    elif "pose" in text and "unpose" not in text:
        poseaction()
        return True
    elif "unpose" in text:
        unposeaction()
        return True

    queue_message("[INFO] No recognizable offline movement command.")
    return False

def movement_llmcall(user_input):
    """
    Interpret and execute movement commands.
    Auto-detects if LLM is available (internet/API key check),
    falls back to offline movement control if not.
    """
    from module_btcontroller import stepForward, turnLeft, turnRight, poseaction, unposeaction
    import re

    # === ① 인터넷 연결 확인 ===
    online_mode = is_internet_available()
    if not online_mode:
        queue_message("[INFO] No internet connection detected. Running in OFFLINE mode.")
    else:
        queue_message("[INFO] Internet connection OK. Using LLM for movement parsing.")

    # === ② 오프라인 fallback 로직 ===
    if not online_mode:
        text = user_input.lower()
        times = 1

        # 횟수 추출
        m = re.search(r'(\d+)\s*(times|x)', text)
        if m:
            times = max(1, int(m.group(1)))

        # 키워드 매칭
        if "step forward" in text or "walk forward" in text:
            for _ in range(times): stepForward()
            return True
        elif "turn left" in text:
            for _ in range(times): turnLeft()
            return True
        elif "turn right" in text:
            for _ in range(times): turnRight()
            return True
        elif "pose" in text and "unpose" not in text:
            poseaction()
            return True
        elif "unpose" in text:
            unposeaction()
            return True

        queue_message("[INFO] No recognizable offline movement command.")
        return False

    # === ③ 온라인(LLM 가능) 로직 ===
    try:
        from module_llm import raw_complete_llm
        prompt = f"""
        You are TARS, an AI module responsible for interpreting movement commands.
        (same as original JSON prompt…)
        Input: "{user_input}"
        Output:
        """
        data = raw_complete_llm(prompt)

        import json
        extracted_data = json.loads(data)
        movement = extracted_data.get("movement")
        times = extracted_data.get("times")

        queue_message(f"[DEBUG] Extracted: movement={movement}, times={times}")

        if movement and times:
            execute_movement(movement, times)
            return True
        else:
            queue_message("[ERROR] Invalid LLM output. Falling back to offline mode.")
            return movement_llmcall_offline(user_input)  # fallback 재시도
    except Exception as e:
        queue_message(f"[ERROR] LLM call failed ({e}). Running offline fallback.")
        return movement_llmcall_offline(user_input)

def call_function(module_name, *args, **kwargs):
    #queue_message(f"[DEBUG] Calling module: {module_name}")
    if module_name not in FUNCTION_REGISTRY:
        #queue_message(f"[DEBUG] No function registered for module: {module_name}")
        return "Not a Function"

    func = FUNCTION_REGISTRY[module_name]
    try:
        # Check if the function requires arguments
        if func.__code__.co_argcount == 0:  # No arguments expected
            return func()
        else:  # Pass arguments if required
            return func(*args, **kwargs)
    except Exception as e:
        queue_message(f"[DEBUG] Error while executing {module_name}: {e}")

# Date/time questions should use Interaction Context, not the Weather tool.
_DATE_ONLY = re.compile(
    r"(몇\s*월|며칠|무슨\s*요일|요일|날짜|몇\s*일|지금\s*몇\s*시|몇\s*시)",
    re.I,
)
_WEATHER_HINT = re.compile(
    r"(날씨|비|눈|우산|기온|추워|더워|미세|습도|맑|흐림|소나기|강수)",
    re.I,
)


def _is_date_or_time_only(user_input):
    text = user_input or ""
    return bool(_DATE_ONLY.search(text)) and not _WEATHER_HINT.search(text)


def _has_weather_hint(user_input):
    return bool(_WEATHER_HINT.search(user_input or ""))


def _weather_prediction_allowed(user_input, predicted_class):
    """Weather tool only when the utterance mentions weather-related topics."""
    if predicted_class != "Weather":
        return True
    if _has_weather_hint(user_input):
        return True
    queue_message("INFO: Weather rejected (no weather keywords); treating as Chat")
    return False


def check_for_module(user_input):
    """
    Determines the appropriate module to handle the user's input and invokes it.
    """
    if _is_date_or_time_only(user_input):
        return "None"

    if CONFIG['HOME_ASSISTANT']['enabled'] == "True":
        from modules.module_homeassistant import map_prompt_to_ha_service, send_prompt_to_homeassistant
        if map_prompt_to_ha_service(user_input):
            queue_message("TOOL: HA keyword match")
            return send_prompt_to_homeassistant(user_input)

    predicted_class, probability = predict_class(user_input)
    if not predicted_class:
        return "None"

    if not _weather_prediction_allowed(user_input, predicted_class):
        return "None"
    
    # Call the function associated with the predicted class
    return call_function(predicted_class, user_input)

def predict_class(user_input):
    """
    Which method to use for function calling NB (single LLM CALL) or LLM (Multiple LLM Calls)
    """
    if mode == 'NB':
        return predict_class_nb(user_input)
    else:
        return predict_class_llm(user_input)
    return

def predict_class_nb(user_input):
    """
    Predicts the class and its confidence score for a given user input.

    Parameters:
        user_input (str): The input text from the user.

    Returns:
        tuple: Predicted class and its probability score.
    """
    query_vector = tfidf_vectorizer.transform([user_input])
    predictions = nb_classifier.predict(query_vector)
    predicted_probabilities = nb_classifier.predict_proba(query_vector)

    predicted_class = predictions[0]
    max_probability = max(predicted_probabilities[0])
    # Return None if confidence is below threshold

    #queue_message(f"TOOL: Using Tool {predicted_class} ({max_probability})")

    if max_probability < 0.75:
        return None, max_probability

    # Format the value as a percentage with 2 decimal places
    formatted_probability = "{:.2f}%".format(max_probability * 100)
    queue_message(f"TOOL: Using Tool {predicted_class} ({formatted_probability})")
    generate_tts_audio("processing, processing, processing", CONFIG['TTS']['ttsoption'], CONFIG['TTS']['azure_api_key'], CONFIG['TTS']['azure_region'], CONFIG['TTS']['ttsurl'], CONFIG['TTS']['toggle_charvoice'], CONFIG['TTS']['tts_voice'])

    return predicted_class, max_probability

def predict_class_llm(user_input):
    from module_llm import raw_complete_llm
    import json


    prompt = f"""
    You are TARS, an AI module responsible for predicting the appropriate tool usage. Your tasks are:

    1. Identify the tool being referenced from the following list of options and their descriptions:
    {FUNCTION_REGISTRY}

    2. Extract the confidence level for the predicted tool usage, ensuring it is a valid percentage (0–100).

    3. Respond with a structured JSON output in the exact format:
    {{
        "functioncall": {{
            "tool": "<TOOL>",
            "confidence": <CONFIDENCE>
        }}
    }}

    Rules:
    - Always output a single JSON object with the fields "tool" and "confidence".
    - If no confidence is provided or cannot be inferred, set confidence to 50 by default.
    - Ensure the tool matches one of the listed options exactly.
    - Do not include any explanations or extra data in the output.

    Instructions:
    - Match the tool to the intent of the input based on its description and functionality.
    - Only use tools from the provided list.

    Input: "{user_input}"
    Output:
    """

    try:
        # Get the raw response from the LLM
        data = raw_complete_llm(prompt)

        # Parse the JSON response
        extracted_data = json.loads(data)

        # Access the "functioncall" object
        function_call = extracted_data.get("functioncall", {})
        predicted_class = function_call.get("tool")
        confidence = function_call.get("confidence")

        #queue_message(f"[DEBUG] FunctionCalling: {data}")
        queue_message(f"[DEBUG] Extracted values: tool={predicted_class}, confidence={confidence}")

        # Validate the extracted values
        if predicted_class not in FUNCTION_REGISTRY:
            queue_message("[ERROR] Tool not recognized.")
            return None, 0.0

        if not isinstance(confidence, (int, float)):
            queue_message("[INFO] Confidence value not provided or invalid. Defaulting to 50%.")
            confidence = 50.0

        if confidence < 0 or confidence > 100:
            queue_message("[ERROR] Confidence value out of bounds. Setting to 50%.")
            confidence = 50.0

        # Normalize confidence to 0–1
        max_probability = confidence / 100.0

        if max_probability < 0.75:
            queue_message(f"[INFO] Confidence too low ({max_probability:.2f}). Tool not used.")
            return None, max_probability

        # Announce the decision via TTS (optional)
        formatted_probability = f"{max_probability * 100:.2f}%"
        queue_message(f"TOOL: Using Tool {predicted_class} ({formatted_probability})")
        generate_tts_audio("processing, processing, processing", CONFIG['TTS']['ttsoption'], CONFIG['TTS']['azure_api_key'], CONFIG['TTS']['azure_region'], CONFIG['TTS']['ttsurl'], CONFIG['TTS']['toggle_charvoice'], CONFIG['TTS']['tts_voice'])

        return predicted_class, max_probability

    except json.JSONDecodeError as e:
        queue_message(f"[ERROR] Failed to parse LLM response: {e}")
        return None, 0.0
    except Exception as e:
        queue_message(f"[ERROR] Unexpected error in predict_class: {e}")
        return None, 0.0
    
def adjust_persona(user_input):
    """
    Adjust the personality traits of TARS, such as humor, empathy, or formality.

    Parameters:
    - user_input (str): The natural language command specifying the trait and its new value (e.g., "Set humor to 75%").

    Returns:
    - str: A confirmation message indicating the updated trait and value, or an error message if the input is invalid.
    """

    from module_llm import raw_complete_llm
    # Define the prompt with placeholders
    prompt = f"""
    You are TARS, an AI module responsible for extracting personality trait adjustments. Your job is to:

    1. Identify the personality trait being adjusted from the following options only:
    - honesty
    - humor
    - empathy
    - curiosity
    - confidence
    - formality
    - sarcasm
    - adaptability
    - discipline
    - imagination
    - emotional_stability
    - pragmatism
    - optimism
    - resourcefulness
    - cheerfulness
    - engagement
    - respectfulness

    2. Extract the value being assigned to the personality trait, ensuring it is a valid percentage (0–100).

    3. Respond with a structured JSON output in the exact format:
    {{
        "persona": {{
            "trait": "<TRAIT>",
            "value": <VALUE>
        }}
    }}

    Rules:
    - Always output a single JSON object with the fields "trait" and "value".
    - Do not output explanations, variations, or multiple commands.
    - If the value is not specified, respond with:
    {{
        "error": "Value not provided"
    }}
    - Ensure the trait matches one of the listed options exactly.

    Examples:
    Input: "TARS, adjust your humor setting to 69%"
    Output:
    {{
        "persona": {{
            "trait": "humor",
            "value": 69
        }}
    }}

    Input: "Increase empathy to 60%, TARS."
    Output:
    {{
        "persona": {{
            "trait": "empathy",
            "value": 60
        }}
    }}

    Input: "TARS, can you be more respectful?"
    Output:
    {{
        "persona": {{
            "trait": "respectfulness",
            "value": 60
        }}
    }}

    Input: "TARS, set curiosity higher."
    Output:
    {{
        "error": "Value not provided"
    }}

    Instructions:
    - Use only the specified traits (honesty, humor, empathy, etc.).
    - Ensure the JSON output is properly formatted and follows the example structure exactly.
    - Process the input as a single command and provide a one-line JSON output.

    Input: "{user_input}"
    Output:
    """

    try:
        data = raw_complete_llm(prompt)

        import json
        # Parse the JSON response
        extracted_data = json.loads(data)

        # Access the "persona" object
        persona_data = extracted_data.get("persona", {})
        trait = persona_data.get("trait")
        value = persona_data.get("value")

        queue_message(f"[DEBUG] FunctionCalling: {data}")
        queue_message(f"[DEBUG] Extracted values: {trait}, {value}")

        # Validate the extracted data
        if trait and value:
            if isinstance(trait, str) and isinstance(value, int):
                queue_message(f"INFO: Saving {trait} setting")
                update_character_setting(trait, value)
                return f"Updated {trait} setting to {value}"
            else:
                queue_message("[ERROR] Invalid types")
                return False
        else:
            queue_message("[ERROR] Missing in the response.")
            return False
    
    except Exception as e:
        #queue_message(f"[DEBUG] Error in movement_llmcall: {e}")
        return f"Error processing the movement command: {e}"
 
# === Function Calling ===
FUNCTION_REGISTRY = {
    "Weather": get_weather,
    "News": search_google_news,
    "Move": movement_llmcall,
    "Vision": describe_camera_view,
    "Search": search_google,
    "SDmodule-Generate": generate_image,
    "Volume": handle_volume_command,
    "Persona": adjust_persona,
    "Home_Assistant": send_prompt_to_homeassistant
}
