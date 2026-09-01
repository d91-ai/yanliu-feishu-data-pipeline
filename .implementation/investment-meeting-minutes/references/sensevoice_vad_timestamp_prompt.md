# SenseVoice VAD Segment Timestamp Notes

Use this note when audio doubtful-item timestamps need manual replay anchors.

- Run VAD on the complete audio file once. Do not run VAD on pre-cut 20s/60s chunks for final timestamps.
- Slice audio by the global VAD boundaries, then transcribe each VAD segment with SenseVoiceSmall.
- Write timestamp index records with `source=sensevoice_vad_segment`, `precision=segment`, `start`, `end`, `start_ms`, `end_ms`, `duration_ms`, and `text`.
- Treat only sentence/phrase anchors and short `sensevoice_vad_segment` records with `duration_ms <= 10000` as reliable doubtful-item timestamps.
- Do not use chunk/minute-level ranges as final inline doubtful timestamps or final table timestamps.
- Do not make Paraformer the default repair path for this timestamp issue. Keep it as auxiliary proofreading and available evidence only.
