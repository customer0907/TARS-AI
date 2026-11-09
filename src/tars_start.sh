#!/bin/bash
cd /home/lch/TARS-AI/src
source .venv/bin/activate
amixer -c 2 sset 'PCM' 80%
python app.py
