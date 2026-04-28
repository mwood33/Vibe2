#!/bin/bash
# Load .env if it exists
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

pip install -r requirements.txt -q
python app.py
