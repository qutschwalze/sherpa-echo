#!/usr/bin/env bash
set -euo pipefail
# Lädt Kroko-DE + Speaker-Modelle in ./models (wie Android Manager)
# Aufruf: ./download_models.sh  (läuft auf Host, nicht auf VM)
MODELS_DIR="$(dirname "$0")/models"
mkdir -p "$MODELS_DIR/kroko-de" "$MODELS_DIR"

echo ">> Kroko DE (58 MB) – csukuangfj/sherpa-onnx-streaming-zipformer-de-kroko-2025-08-06"
for f in encoder.onnx decoder.onnx joiner.onnx tokens.txt; do
  if [[ -f "$MODELS_DIR/kroko-de/$f" && -s "$MODELS_DIR/kroko-de/$f" ]]; then echo "  skip $f"; continue; fi
  echo "  get $f"
  curl -L --progress-bar -o "$MODELS_DIR/kroko-de/$f" "https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-de-kroko-2025-08-06/resolve/main/$f"
done

echo ">> Speaker segmentation (tar.bz2 -> segmentation.onnx, 9 MB)"
if [[ ! -f "$MODELS_DIR/segmentation.onnx" ]]; then
  TMP="$MODELS_DIR/sherpa-onnx-reverb-diarization-v1.tar.bz2"
  curl -L --progress-bar -o "$TMP" "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-reverb-diarization-v1.tar.bz2"
  tar -xjf "$TMP" -C "$MODELS_DIR"
  # enthält Verzeichnis mit model.onnx
  find "$MODELS_DIR" -name "model.onnx" | head -1 | xargs -I{} mv "{}" "$MODELS_DIR/segmentation.onnx"
  rm -f "$TMP"
  rm -rf "$MODELS_DIR/sherpa-onnx-reverb-diarization-v1"
else
  echo "  skip segmentation.onnx"
fi

echo ">> Speaker embedding (nemo titan et small, 38 MB)"
if [[ ! -f "$MODELS_DIR/embedding.onnx" ]]; then
  curl -L --progress-bar -o "$MODELS_DIR/embedding.onnx" "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_small.onnx"
else
  echo "  skip embedding.onnx"
fi

echo ">> EN Zipformer (38 MB) – csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-06-26 (Step 5 DE/EN, Opt-in)"
mkdir -p "$MODELS_DIR/en-zipformer"
for f in encoder-epoch-99-avg-1-chunk-16-left-128.int8.onnx decoder-epoch-99-avg-1-chunk-16-left-128.onnx joiner-epoch-99-avg-1-chunk-16-left-128.int8.onnx tokens.txt; do
  if [[ -f "$MODELS_DIR/en-zipformer/$f" && -s "$MODELS_DIR/en-zipformer/$f" ]]; then echo "  skip $f"; continue; fi
  echo "  get $f"
  curl -L --progress-bar -o "$MODELS_DIR/en-zipformer/$f" "https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-06-26/resolve/main/$f"
done

echo ">> Done – prüfen:"
ls -lh "$MODELS_DIR/kroko-de/" "$MODELS_DIR"/*.onnx
echo "Deploy: rsync -avz ./models/ <server>:/<server-path>/models/ (Zielhost nicht ins Repo schreiben)"
